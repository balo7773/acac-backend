from datetime import datetime, timezone
from pathlib import Path
import re
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.database import get_rls_session
from app.core.security import get_candidate_context, get_current_claims
from app.services.pdf_service import count_words, normalize_canonical_text, process_and_store_pdf

router = APIRouter(prefix="/api/candidate", tags=["candidate"])

FALLBACK_QUESTIONS = [
    "Identify the single highest-risk assumption in your proposed implementation timeline and explain what concrete contingency mechanism you would trigger if that assumption fails in year one.",
    "Specify which institutional stakeholder stands to lose the most influence under your proposal, and explain how your framework prevents them from blocking execution.",
    "If your available budget or financing capacity were cut by 40% midway through implementation, which specific component of your proposal would you sacrifice first and why?",
]


class ComplaintRequest(BaseModel):
    subject: str = Field(..., min_length=3, max_length=255)
    message: str = Field(..., min_length=10, max_length=2000)


class FinalSubmitRequest(BaseModel):
    accepted_rules: bool = Field(default=False, description="Explicit agreement to competition rules")
    rules_version: str = Field(default="v1.0", min_length=1, max_length=30)


class DefenseTextRequest(BaseModel):
    answer_text: str = Field(..., min_length=1, max_length=4000)


def scan_candidate_identity(
    canonical_text: str,
    full_name: str,
    matric_number: str,
    whatsapp_number: str,
) -> list[str]:
    violations = []

    # 1. Name parts: tokens >= 3 letters, skipping "AFIT"
    name_parts = [
        re.sub(r"[^A-Za-z]", "", part).lower()
        for part in re.split(r"\s+", full_name)
    ]
    name_parts = [p for p in name_parts if len(p) >= 3 and p != "afit"]
    for part in name_parts:
        if re.search(rf"\b{re.escape(part)}\b", canonical_text, flags=re.IGNORECASE):
            violations.append(f"name ('{part}')")
            break

    # 2. Matric number: case-insensitive match
    matric_clean = matric_number.strip()
    if matric_clean and matric_clean.lower() in canonical_text.lower():
        violations.append(f"matric number ('{matric_clean}')")

    # 3. Phone number / WhatsApp: formatted string or significant digits
    phone_clean = whatsapp_number.strip()
    if phone_clean and phone_clean.lower() in canonical_text.lower():
        violations.append(f"phone number ('{phone_clean}')")
    else:
        digits = re.sub(r"\D", "", phone_clean)
        if len(digits) >= 8:
            digits_text = re.sub(r"\D", "", canonical_text)
            if digits[-8:] in digits_text:
                violations.append(f"phone digits ('{digits[-8:]}')")

    return violations


@router.get("/portal")
async def get_candidate_portal(ctx=Depends(get_candidate_context)):
    candidate_id, session = ctx

    res = await session.execute(
        text(
            """
            SELECT
                c.participant_number, c.full_name, c.matric_number, c.whatsapp_number, c.linkedin_url,
                s.id AS submission_id, s.status AS submission_status,
                s.page_count, s.word_count, s.final_submitted_at,
                cw.outcome AS window_outcome,
                CASE WHEN cw.outcome IN ('in_progress', 'answered', 'auto_zero') THEN cw.question_text ELSE NULL END AS question_text,
                cw.question_delivered_at, cw.window_ends_at, cw.draft_text, cw.answer_text, cw.answer_submitted_at,
                cs.current_phase
            FROM candidates c
            JOIN submissions s ON s.candidate_id = c.id
            LEFT JOIN conclusion_windows cw ON cw.submission_id = s.id
            CROSS JOIN competition_state cs
            WHERE cs.id = 1
            """
        )
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate portal record not found.")
    return dict(row)


async def _disqualify(candidate_id: int, reason: str) -> None:
    """Persist disqualification in a fresh admin session, so it survives even
    when the calling handler later raises HTTPException (which rolls back its own session)."""
    from app.core.database import get_rls_session as _get_admin_session
    async with _get_admin_session("admin_role") as s:
        await s.execute(
            text("UPDATE candidates SET is_disqualified = TRUE, "
                 "disqualification_reason = :r WHERE id = :cid"),
            {"r": reason, "cid": candidate_id},
        )


@router.post("/upload-draft")


async def upload_essay_draft(
    file: UploadFile = File(...),
    ctx=Depends(get_candidate_context),
):
    candidate_id, session = ctx

    sub_res = await session.execute(
        text(
            """
            SELECT
                s.id AS submission_id,
                s.participant_number,
                s.status AS submission_status,
                c.full_name,
                c.matric_number,
                c.whatsapp_number,
                cs.current_phase,
                cs.stage_1_submission_deadline
            FROM submissions s
            JOIN candidates c ON c.id = s.candidate_id
            CROSS JOIN competition_state cs
            WHERE s.candidate_id = current_user_id() AND cs.id = 1
            FOR UPDATE OF s
            """
        )
    )
    row = sub_res.mappings().first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission record not found.")

    if row["current_phase"] != "stage_1_submitting":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Essay uploads are only permitted during stage_1_submitting (current phase: {row['current_phase']}).",
        )

    if row["stage_1_submission_deadline"] is not None:
        now_utc = datetime.now(timezone.utc)
        if now_utc > row["stage_1_submission_deadline"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Submission deadline has passed. Uploads are closed.",
            )

    if row["submission_status"] != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Essay is already locked ({row['submission_status']}) and can no longer be replaced.",
        )

    expected_filename = f"{row['participant_number']}.pdf"
    clean_upload_name = Path(file.filename or "").name
    if clean_upload_name != expected_filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid filename '{clean_upload_name}'. Filename must be exactly '{expected_filename}'.",
        )

    pdf_bytes = await file.read()

    try:
        extracted = process_and_store_pdf(pdf_bytes, participant_number=row["participant_number"])
    except HTTPException as e:
        # Word-count violation → disqualify (not soft-retry).
        detail = str(e.detail).lower()
        if "word" in detail and ("minimum" in detail or "fewer" in detail):
            await _disqualify(
                candidate_id,
                f"Essay contains fewer than {settings.ESSAY_MIN_WORDS} words.",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Your essay was below the {settings.ESSAY_MIN_WORDS}-word minimum "
                    f"and you have been disqualified from the competition."
                ),
            )
        # Any other PDF issue (invalid format, too large, corrupted) — plain 400, no DQ.
        raise

    violations = scan_candidate_identity(
        canonical_text=extracted.canonical_text,
        full_name=row["full_name"],
        matric_number=row["matric_number"],
        whatsapp_number=row["whatsapp_number"],
    )
    if violations:
        await _disqualify(
            candidate_id,
            f"Essay contains candidate identity markers: {', '.join(violations)}.",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Your essay contained candidate identity ({', '.join(violations)}) and "
                f"you have been disqualified from the competition. The essay must be anonymous."
            ),
        )

    await session.execute(
        text(
            """
            UPDATE submissions
            SET pdf_path = :pdf_path,
                page_count = :page_count,
                word_count = :word_count,
                canonical_text = :canonical_text
            WHERE id = :sid
            """
        ),
        {
            "pdf_path": extracted.saved_path,
            "page_count": extracted.page_count,
            "word_count": extracted.word_count,
            "canonical_text": extracted.canonical_text,
            "sid": row["submission_id"],
        },
    )

    checks = [
        {"check": "filename", "passed": True, "message": f"Filename matches {expected_filename}"},
        {"check": "readable_text", "passed": True, "message": "Text is readable and selectable"},
        {"check": "pages", "passed": True, "message": f"{extracted.page_count} pages"},
        {"check": "words", "passed": True, "message": f"{extracted.word_count} words (minimum {settings.ESSAY_MIN_WORDS})"},
        {"check": "no_visible_identity", "passed": True, "message": "No identity markers found"},
    ]

    return {
        "submission_id": row["submission_id"],
        "participant_number": row["participant_number"],
        "status": "draft",
        "page_count": extracted.page_count,
        "word_count": extracted.word_count,
        "preview_snippet": extracted.canonical_text[:400],
        "checks": checks,
    }


@router.post("/submit-final")
async def submit_final(payload: FinalSubmitRequest, ctx=Depends(get_candidate_context)):
    candidate_id, session = ctx

    if not payload.accepted_rules:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You must accept the competition rules before final submission.",
        )

    phase_res = await session.execute(
        text("SELECT current_phase, stage_1_submission_deadline FROM competition_state WHERE id = 1")
    )
    phase_row = phase_res.mappings().first()
    if not phase_row or phase_row["current_phase"] != "stage_1_submitting":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Final submissions are only allowed during stage_1_submitting (current phase: {phase_row['current_phase'] if phase_row else 'none'}).",
        )

    if phase_row["stage_1_submission_deadline"] is not None:
        now_utc = datetime.now(timezone.utc)
        if now_utc > phase_row["stage_1_submission_deadline"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Submission deadline has passed. Final submission rejected.",
            )

    sub_res = await session.execute(
        text(
            "SELECT id, pdf_path, page_count, word_count, canonical_text, status "
            "FROM submissions WHERE candidate_id = current_user_id()"
        )
    )
    sub = sub_res.first()
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")
    if sub.status != "draft":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Essay has already been submitted.")
    if not sub.canonical_text or not sub.pdf_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please upload a valid PDF draft before final submission.",
        )

    try:
        fn_res = await session.execute(
            text(
                "SELECT submit_final_essay(:sid, :pdf, :pages, :words, :canon, :rules)"
            ),
            {
                "sid": sub.id,
                "pdf": sub.pdf_path,
                "pages": sub.page_count,
                "words": sub.word_count,
                "canon": sub.canonical_text,
                "rules": payload.rules_version,
            },
        )
        template_id = fn_res.scalar_one()
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))

    return {
        "submission_id": sub.id,
        "status": "submitted",
        "template_id": template_id,
        "window_outcome": "preparing",
    }


@router.post("/start-defense")
async def start_conclusion_defense(ctx=Depends(get_candidate_context)):
    candidate_id, session = ctx

    res = await session.execute(
        text(
            """
            SELECT cw.submission_id, cw.outcome, cw.template_id, cw.question_text,
                   cw.window_ends_at, s.window_length_seconds,
                   cs.stage_1_conclusion_closing_time
            FROM conclusion_windows cw
            CROSS JOIN settings s
            CROSS JOIN competition_state cs
            WHERE s.is_active = TRUE AND cs.id = 1
            FOR UPDATE OF cw
            """
        )
    )
    row = res.first()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conclusion window not initialized. Submit your final essay first.",
        )

    if row.outcome == "in_progress":
        return {
            "submission_id": row.submission_id,
            "outcome": row.outcome,
            "question_text": row.question_text,
            "window_ends_at": row.window_ends_at,
        }

    if row.outcome != "preparing":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Conclusion window is already closed ({row.outcome}).",
        )

    now_utc = datetime.now(timezone.utc)
    if row.stage_1_conclusion_closing_time and now_utc >= row.stage_1_conclusion_closing_time:
        await session.execute(
            text("UPDATE conclusion_windows SET outcome = 'auto_zero' WHERE submission_id = :sid"),
            {"sid": row.submission_id},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conclusion defense period has closed.",
        )

    q_text = row.question_text
    is_fallback = False
    q_source = "deepseek"

    if not q_text:
        idx = ((row.template_id or 1) - 1) % len(FALLBACK_QUESTIONS)
        q_text = FALLBACK_QUESTIONS[idx]
        is_fallback = True
        q_source = "backup"

    upd = await session.execute(
        text(
            """
            UPDATE conclusion_windows cw
            SET outcome = 'in_progress',
                question_text = :q_text,
                is_fallback_wording = :is_fb,
                question_source = :q_src,
                question_delivered_at = NOW(),
                window_ends_at = CASE
                    WHEN cs.stage_1_conclusion_closing_time IS NOT NULL
                    THEN LEAST(NOW() + make_interval(secs => :win_secs), cs.stage_1_conclusion_closing_time)
                    ELSE NOW() + make_interval(secs => :win_secs)
                END
            FROM competition_state cs
            WHERE cs.id = 1 AND cw.submission_id = :sid
            RETURNING cw.question_delivered_at, cw.window_ends_at
            """
        ),
        {
            "q_text": q_text,
            "is_fb": is_fallback,
            "q_src": q_source,
            "win_secs": row.window_length_seconds,
            "sid": row.submission_id,
        },
    )
    times = upd.one()

    return {
        "submission_id": row.submission_id,
        "outcome": "in_progress",
        "question_text": q_text,
        "question_delivered_at": times.question_delivered_at,
        "window_ends_at": times.window_ends_at,
    }


@router.post("/save-defense-draft")
async def save_defense_draft(payload: DefenseTextRequest, ctx=Depends(get_candidate_context)):
    candidate_id, session = ctx
    norm_ans = normalize_canonical_text(payload.answer_text)

    res = await session.execute(
        text(
            """
            UPDATE conclusion_windows
            SET draft_text = :draft,
                draft_saved_at = NOW()
            WHERE outcome = 'in_progress'
            RETURNING submission_id, draft_saved_at
            """
        ),
        {"draft": norm_ans},
    )
    row = res.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Defense window is not active.")
    return {"submission_id": row.submission_id, "draft_saved_at": row.draft_saved_at}


@router.post("/submit-defense")
async def submit_defense(payload: DefenseTextRequest, ctx=Depends(get_candidate_context)):
    candidate_id, session = ctx
    norm_ans = normalize_canonical_text(payload.answer_text)
    words = count_words(norm_ans)

    cfg_res = await session.execute(
        text(
            "SELECT s.id AS submission_id, st.answer_word_min, st.answer_word_limit "
            "FROM submissions s CROSS JOIN settings st WHERE st.is_active = TRUE"
        )
    )
    cfg = cfg_res.first()
    if not cfg:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")

    if words < cfg.answer_word_min:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conclusion answer is {words} words, below the {cfg.answer_word_min}-word minimum.",
        )

    if words > cfg.answer_word_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conclusion answer is {words} words, exceeding the {cfg.answer_word_limit}-word limit.",
        )

    try:
        fn_res = await session.execute(
            text("SELECT submit_conclusion_answer(:sid, :ans)"),
            {"sid": cfg.submission_id, "ans": norm_ans},
        )
        outcome = fn_res.scalar_one()
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))

    return {"submission_id": cfg.submission_id, "outcome": outcome, "word_count": words}

@router.get("/verdict")
async def get_verdict(claims: dict = Depends(get_current_claims)):
    """Return the candidate's verdict: qualified / not_selected / disqualified / pending.
    No scores or ranks exposed. Only meaningful once results are published.
    This endpoint deliberately bypasses get_candidate_context so that disqualified
    candidates can still see their status and reason."""
    if claims["role"] != "candidate":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Candidate access required.",
        )
    async with get_rls_session("candidate_role", claims["user_id"]) as session:
        phase_res = await session.execute(
            text("SELECT current_phase FROM competition_state WHERE id = 1")
        )
        phase = phase_res.scalar_one()
        if phase not in ("results_published", "stage_2_live", "completed"):
            return {"status": "pending"}

        res = await session.execute(text("SELECT fn_get_candidate_verdict()"))
        return res.scalar_one()

@router.post("/complaints")
async def submit_complaint(
    payload: ComplaintRequest,
    ctx=Depends(get_candidate_context),
):
    candidate_id, session = ctx
    await session.execute(
        text("""
            INSERT INTO complaints (candidate_id, subject, message)
            VALUES (:cid, :sub, :msg)
        """),
        {"cid": candidate_id, "sub": payload.subject, "msg": payload.message},
    )
    return {"status": "submitted", "message": "Your complaint has been received by the admin."}

