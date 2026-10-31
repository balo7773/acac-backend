import json
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.security import get_judge_context

router = APIRouter(prefix="/api/judge", tags=["judge"])


def _validate_scores_by_judge_type(judge_type: str, scores: dict) -> None:
    """Enforce internal 0-100 continuous, external whole 0-3."""
    for key, value in scores.items():
        if value is None:
            continue
        f = float(value)
        if judge_type == "external":
            if not (f.is_integer() and 0.0 <= f <= 3.0):
                raise HTTPException(
                    status_code=422,
                    detail=f"External judge scores must be whole numbers 0, 1, 2, or 3; got {value} for {key}.",
                )
        else:
            if not (0.0 <= f <= 100.0):
                raise HTTPException(
                    status_code=422,
                    detail=f"Internal judge scores must be between 0.00 and 100.00; got {value} for {key}.",
                )


class Phase1ScorePayload(BaseModel):
    c1: float = Field(..., ge=0, le=100)
    c2: float = Field(..., ge=0, le=100)
    c3: float = Field(..., ge=0, le=100)
    c4: float = Field(..., ge=0, le=100)
    c5: float = Field(..., ge=0, le=100)
    conclusion_score: float = Field(..., ge=0, le=30)
    zero_score_reason: Optional[str] = None


class Phase2RevisionPayload(BaseModel):
    c1: Optional[float] = Field(default=None, ge=0, le=100)
    c2: Optional[float] = Field(default=None, ge=0, le=100)
    c3: Optional[float] = Field(default=None, ge=0, le=100)
    c4: Optional[float] = Field(default=None, ge=0, le=100)
    c5: Optional[float] = Field(default=None, ge=0, le=100)
    conclusion_score: Optional[float] = Field(default=None, ge=0, le=30)
    zero_score_reason: Optional[str] = None
    revision_reason: Optional[str] = None


class RaiseFlagPayload(BaseModel):
    submission_id: int
    flag_type: Literal["contradicts_essay", "off_topic"]
    note: str = Field(..., min_length=5, max_length=1000)


class FlagVotePayload(BaseModel):
    decision: Literal["confirm", "clear"]


class Stage2ScorePayload(BaseModel):
    c1: float = Field(..., ge=0, le=100)
    c2: float = Field(..., ge=0, le=100)
    c3: float = Field(..., ge=0, le=100)
    c4: float = Field(..., ge=0, le=100)


@router.get("/submissions")
async def list_submissions_for_judge(ctx=Depends(get_judge_context)):
    judge_id, session = ctx

    res = await session.execute(
        text(
            """
            SELECT
                s.id AS submission_id,
                s.participant_number,
                s.word_count,
                s.page_count,
                cw.outcome AS conclusion_outcome,
                COALESCE(js.status, 'not_started') AS judge_session_status,
                js.phase_2_expires_at,
                EXISTS(
                    SELECT 1 FROM ai_evaluations ae
                    WHERE ae.submission_id = s.id AND 'single_run' = ANY(ae.markers)
                ) AS has_single_run
            FROM submissions s
            LEFT JOIN conclusion_windows cw ON cw.submission_id = s.id
            LEFT JOIN judge_sessions js ON js.submission_id = s.id AND js.judge_id = current_user_id()
            WHERE s.status IN ('submitted', 'auto_locked')
            ORDER BY s.participant_number ASC
            """
        )
    )
    return [dict(r) for r in res.mappings().all()]


@router.get("/submissions/{submission_id}")
async def get_submission_evaluation_view(submission_id: int, ctx=Depends(get_judge_context)):
    judge_id, session = ctx

    # canonical_text is intentionally NOT selected here to avoid leaking full text prematurely
    sub_res = await session.execute(
        text(
            """
            SELECT
                s.id AS submission_id,
                s.participant_number,
                s.word_count,
                s.page_count,
                cw.outcome AS conclusion_outcome,
                cw.question_text,
                cw.answer_text,
                cw.window_ends_at,
                COALESCE(js.status, 'not_started') AS judge_session_status,
                js.phase_2_expires_at
            FROM submissions s
            LEFT JOIN conclusion_windows cw ON cw.submission_id = s.id
            LEFT JOIN judge_sessions js ON js.submission_id = s.id AND js.judge_id = current_user_id()
            WHERE s.id = :sid
            """
        ),
        {"sid": submission_id},
    )
    sub = sub_res.mappings().first()
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Submission not found.")

    bundle_res = await session.execute(
        text("SELECT get_judge_evaluation_bundle(:sid)"),
        {"sid": submission_id},
    )
    bundle = bundle_res.scalar_one()
    if isinstance(bundle, str):
        bundle = json.loads(bundle)

    claims_res = await session.execute(
        text(
            """
            SELECT id, model_name, claim_type, claim_text, quote_text, quote_start, quote_end
            FROM ai_claims
            WHERE submission_id = :sid
            ORDER BY id ASC
            """
        ),
        {"sid": submission_id},
    )
    claims = [dict(r) for r in claims_res.mappings().all()]

    scores_res = await session.execute(
        text(
            """
            SELECT criterion_key, initial_raw_score, final_raw_score
            FROM essay_scores
            WHERE submission_id = :sid
            ORDER BY criterion_key ASC
            """
        ),
        {"sid": submission_id},
    )
    essay_scores = [dict(r) for r in scores_res.mappings().all()]

    conc_res = await session.execute(
        text(
            """
            SELECT initial_raw_score, final_raw_score, zero_score_reason
            FROM conclusion_scores
            WHERE submission_id = :sid
            """
        ),
        {"sid": submission_id},
    )
    conc_score = conc_res.mappings().first()

    flags_res = await session.execute(
        text(
            """
            SELECT id, flag_type, raised_by, evidence, status, resolved_at, created_at
            FROM flags
            WHERE submission_id = :sid
            ORDER BY id ASC
            """
        ),
        {"sid": submission_id},
    )
    flags = [dict(r) for r in flags_res.mappings().all()]

    summary_res = await session.execute(
        text(
            "SELECT summary_text, model_name, created_at FROM ai_summaries WHERE submission_id = :sid"
        ),
        {"sid": submission_id},
    )
    summary_row = summary_res.mappings().first()

    markers_res = await session.execute(
        text(
            "SELECT DISTINCT unnest(markers) AS marker FROM ai_evaluations WHERE submission_id = :sid"
        ),
        {"sid": submission_id},
    )
    markers = [r["marker"] for r in markers_res.mappings().all() if r["marker"]]

    return {
        "submission": dict(sub),
        "bundle": bundle,
        "ai_claims": claims,
        "ai_summary": dict(summary_row) if summary_row else None,
        "markers": markers,
        "my_essay_scores": essay_scores,
        "my_conclusion_score": dict(conc_score) if conc_score else None,
        "flags": flags,
    }


@router.post("/submissions/{submission_id}/open-full")
@router.post("/submissions/{submission_id}/mark-read")
async def open_full_essay(submission_id: int, ctx=Depends(get_judge_context)):
    judge_id, session = ctx
    try:
        res = await session.execute(
            text("SELECT open_full_essay_for_judge(:sid)"),
            {"sid": submission_id},
        )
        canon = res.scalar_one()
    except DBAPIError as e:
        msg = str(e.orig)
        if "ESSAY_LOCKED|" in msg:
            detail = msg.split("ESSAY_LOCKED|", 1)[1].strip().rstrip(".")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg)

    return {
        "submission_id": submission_id,
        "opened_full_essay": True,
        "canonical_text": canon,
    }


@router.post("/submissions/{submission_id}/lock-phase1")
async def lock_phase1(
    submission_id: int,
    payload: Phase1ScorePayload,
    ctx=Depends(get_judge_context),
):
    judge_id, session = ctx
    jt = await session.execute(
        text("SELECT judge_type FROM judges WHERE id = current_user_id()")
    )
    judge_type = jt.scalar_one()
    _validate_scores_by_judge_type(
        judge_type,
        {"c1": payload.c1, "c2": payload.c2, "c3": payload.c3, "c4": payload.c4, "c5": payload.c5},
    )
    try:
        res = await session.execute(
            text(
                "SELECT lock_phase1_scores(:sid, :c1, :c2, :c3, :c4, :c5, :conc, :zreason)"
            ),
            {
                "sid": submission_id,
                "c1": payload.c1,
                "c2": payload.c2,
                "c3": payload.c3,
                "c4": payload.c4,
                "c5": payload.c5,
                "conc": payload.conclusion_score,
                "zreason": payload.zero_score_reason,
            },
        )
        expires_at = res.scalar_one()
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))

    return {
        "submission_id": submission_id,
        "session_status": "phase_2_review",
        "phase_2_expires_at": expires_at,
    }


@router.post("/submissions/{submission_id}/submit-phase2")
async def submit_phase2(
    submission_id: int,
    payload: Phase2RevisionPayload,
    ctx=Depends(get_judge_context),
):
    judge_id, session = ctx
    jt = await session.execute(
        text("SELECT judge_type FROM judges WHERE id = current_user_id()")
    )
    judge_type = jt.scalar_one()
    _validate_scores_by_judge_type(
        judge_type,
        {"c1": payload.c1, "c2": payload.c2, "c3": payload.c3, "c4": payload.c4, "c5": payload.c5},
    )
    try:
        res = await session.execute(
            text(
                "SELECT submit_phase2_revisions(:sid, :c1, :c2, :c3, :c4, :c5, :conc, :zreason, :revreason)"
            ),
            {
                "sid": submission_id,
                "c1": payload.c1,
                "c2": payload.c2,
                "c3": payload.c3,
                "c4": payload.c4,
                "c5": payload.c5,
                "conc": payload.conclusion_score,
                "zreason": payload.zero_score_reason,
                "revreason": payload.revision_reason,
            },
        )
        new_status = res.scalar_one()
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))

    return {"submission_id": submission_id, "session_status": new_status}


@router.post("/flags", status_code=status.HTTP_201_CREATED)
async def raise_integrity_flag(payload: RaiseFlagPayload, ctx=Depends(get_judge_context)):
    judge_id, session = ctx
    try:
        res = await session.execute(
            text("SELECT raise_judge_flag(:sid, :ftype, :note)"),
            {"sid": payload.submission_id, "ftype": payload.flag_type, "note": payload.note},
        )
        out = res.scalar_one()
        if isinstance(out, str):
            out = json.loads(out)
        return out
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))


@router.post("/flags/{flag_id}/review")
async def review_flag(flag_id: int, payload: FlagVotePayload, ctx=Depends(get_judge_context)):
    judge_id, session = ctx
    try:
        res = await session.execute(
            text("SELECT submit_flag_review(:fid, :dec)"),
            {"fid": flag_id, "dec": payload.decision},
        )
        resulting_status = res.scalar_one()
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))

    return {"flag_id": flag_id, "resulting_status": resulting_status}


@router.get("/stage2/finalists")
async def list_stage2_finalists(ctx=Depends(get_judge_context)):
    """List approved Stage 2 finalists with per-judge has_my_score flag.
    No identity fields, no scores. Phase guard: stage_2_live or completed."""
    judge_id, session = ctx

    phase_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    phase = phase_res.scalar_one()
    if phase not in ("stage_2_live", "completed"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Stage 2 finalist list only available in 'stage_2_live' or 'completed' "
                f"(current: '{phase}')."
            ),
        )

    res = await session.execute(text("SELECT fn_list_stage2_finalists()"))
    finalists = res.scalar_one()
    if finalists is None:
        finalists = []

    return {"finalists": finalists, "count": len(finalists)}


@router.get("/stage2/{submission_id}/peers")
async def get_stage2_peers(
    submission_id: int,
    ctx=Depends(get_judge_context),
):
    """Peer scores for a Stage 2 finalist. Revealed only when (a) quorum is met
    and (b) the requesting judge has submitted their own score. Returns mask
    labels only, never identity."""
    judge_id, session = ctx

    phase_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    phase = phase_res.scalar_one()
    if phase not in ("stage_2_live", "completed"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Stage 2 peer view only available in 'stage_2_live' or 'completed' "
                f"(current: '{phase}')."
            ),
        )

    try:
        res = await session.execute(
            text("SELECT fn_get_stage2_view(:sid)"),
            {"sid": submission_id},
        )
        result = res.scalar_one()
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))

    return result


@router.post("/stage2/{submission_id}/score")
async def submit_stage2_live_score(
    submission_id: int,
    payload: Stage2ScorePayload,
    ctx=Depends(get_judge_context),
):
    judge_id, session = ctx
    try:
        await session.execute(
            text("SELECT submit_stage2_scores(:sid, :c1, :c2, :c3, :c4)"),
            {"sid": submission_id, "c1": payload.c1, "c2": payload.c2, "c3": payload.c3, "c4": payload.c4},
        )
    except DBAPIError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e.orig))
    return {"submission_id": submission_id, "status": "stage2_recorded"}
