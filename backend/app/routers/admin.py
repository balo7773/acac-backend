import json
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.security import get_admin_context, get_password_hash

router = APIRouter(prefix="/api/admin", tags=["admin"])


class PhaseUpdateRequest(BaseModel):
    new_phase: Literal[
        "pre_launch",
        "stage_1_submitting",
        "stage_1_grading",
        "stage_1_review",
        "finalist_check",
        "results_published",
        "stage_2_live",
        "completed",
    ]


class CreateJudgeRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=60)
    password: str = Field(..., min_length=8, max_length=128)
    judge_type: Literal["internal", "external"]
    mask_avatar: str = Field(default="mask_default", min_length=2, max_length=60)


class FinalistCheckRequest(BaseModel):
    submission_id: int
    decision: Literal["approved", "rejected"]
    reason: Optional[str] = None


@router.post("/phase")
async def update_competition_phase(payload: PhaseUpdateRequest, ctx=Depends(get_admin_context)):
    admin_id, session = ctx

    await session.execute(
        text(
            "UPDATE competition_state SET current_phase = :phase WHERE id = 1"
        ),
        {"phase": payload.new_phase},
    )

    if payload.new_phase == "stage_1_grading":
        await session.execute(
            text(
                """
                INSERT INTO ai_jobs (job_type, submission_id, model_name, run_id)
                VALUES ('job_d', NULL, 'none', 1)
                ON CONFLICT DO NOTHING
                """
            )
        )

    await session.execute(
        text(
            """
            INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
            VALUES ('admin', :aid, 'phase_changed', 'competition_state', 1, CAST(:det AS jsonb))
            """
        ),
        {"aid": admin_id, "det": json.dumps({"new_phase": payload.new_phase})},
    )

    return {"current_phase": payload.new_phase}


@router.post("/judges", status_code=status.HTTP_201_CREATED)
async def create_judge(payload: CreateJudgeRequest, ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    hashed = get_password_hash(payload.password)

    try:
        res = await session.execute(
            text(
                """
                INSERT INTO judges (username, password_hash, judge_type, mask_avatar)
                VALUES (:uname, :pw, :jtype, :avatar)
                RETURNING id, username, judge_type, mask_avatar, is_active
                """
            ),
            {
                "uname": payload.username.strip(),
                "pw": hashed,
                "jtype": payload.judge_type,
                "avatar": payload.mask_avatar.strip(),
            },
        )
        row = res.mappings().one()
    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Judge with this username already exists.",
        )

    return dict(row)


@router.post("/compute-stage1")
async def trigger_stage1_computation(ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    await session.execute(text("SELECT compute_stage1_results()"))

    res = await session.execute(
        text(
            """
            SELECT
                r.rank, r.submission_id, s.participant_number,
                c.full_name, c.matric_number, c.whatsapp_number,
                r.essay_score, r.conclusion_score, r.composite_score,
                r.is_in_blur_zone, fc.decision AS finalist_decision
            FROM stage1_results r
            JOIN submissions s ON s.id = r.submission_id
            JOIN candidates c ON c.id = s.candidate_id
            LEFT JOIN finalist_checks fc ON fc.submission_id = r.submission_id
            ORDER BY r.rank ASC, r.submission_id ASC
            """
        )
    )
    return {"leaderboard": [dict(r) for r in res.mappings().all()]}


@router.post("/finalist-check")
async def record_finalist_check(payload: FinalistCheckRequest, ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    if payload.decision == "rejected" and not payload.reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A written reason is required when rejecting a finalist.",
        )

    await session.execute(
        text(
            """
            INSERT INTO finalist_checks (submission_id, admin_id, decision, reason)
            VALUES (:sid, :aid, :dec, :reason)
            ON CONFLICT (submission_id) DO UPDATE
            SET admin_id = EXCLUDED.admin_id,
                decision = EXCLUDED.decision,
                reason = EXCLUDED.reason,
                checked_at = NOW()
            """
        ),
        {
            "sid": payload.submission_id,
            "aid": admin_id,
            "dec": payload.decision,
            "reason": payload.reason,
        },
    )
    return {"submission_id": payload.submission_id, "decision": payload.decision}


@router.post("/compute-final")
async def trigger_final_computation(ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    await session.execute(text("SELECT compute_final_results()"))

    res = await session.execute(
        text(
            """
            SELECT
                fr.final_rank, fr.submission_id, s.participant_number,
                c.full_name, c.matric_number,
                fr.stage1_score, fr.stage2_score, fr.overall_score
            FROM final_results fr
            JOIN submissions s ON s.id = fr.submission_id
            JOIN candidates c ON c.id = s.candidate_id
            ORDER BY fr.final_rank ASC
            """
        )
    )
    return {"final_standings": [dict(r) for r in res.mappings().all()]}


@router.get("/audit-log")
async def get_audit_log(limit: int = 100, ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    res = await session.execute(
        text(
            """
            SELECT id, actor_role, actor_id, action, target_type, target_id, details, created_at
            FROM audit_log
            ORDER BY id DESC
            LIMIT :lim
            """
        ),
        {"lim": min(limit, 500)},
    )
    return [dict(r) for r in res.mappings().all()]


class SimilarityDecisionRequest(BaseModel):
    decision: Literal["confirmed_violation", "cleared"]


@router.get("/similarity-pairs")
async def list_similarity_pairs(ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    res = await session.execute(
        text(
            """
            SELECT
                sp.id, sp.submission_a_id, sp.submission_b_id,
                sa.participant_number AS participant_a,
                sb.participant_number AS participant_b,
                sp.similarity_score, sp.shared_passages, sp.admin_decision
            FROM similarity_pairs sp
            JOIN submissions sa ON sa.id = sp.submission_a_id
            JOIN submissions sb ON sb.id = sp.submission_b_id
            ORDER BY sp.similarity_score DESC
            """
        )
    )
    return [dict(r) for r in res.mappings().all()]


@router.post("/similarity-pairs/{pair_id}/decision")
async def decide_similarity_pair(pair_id: int, payload: SimilarityDecisionRequest, ctx=Depends(get_admin_context)):
    admin_id, session = ctx
    res = await session.execute(
        text(
            """
            UPDATE similarity_pairs
            SET admin_decision = :dec
            WHERE id = :pid
            RETURNING id, submission_a_id, submission_b_id, admin_decision
            """
        ),
        {"dec": payload.decision, "pid": pair_id},
    )
    row = res.mappings().first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Similarity pair not found.")

    await session.execute(
        text(
            """
            INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
            VALUES ('admin', :aid, 'similarity_pair_decided', 'similarity_pair', :pid, CAST(:det AS jsonb))
            """
        ),
        {"aid": admin_id, "pid": pair_id, "det": json.dumps({"decision": payload.decision})},
    )
    return dict(row)
