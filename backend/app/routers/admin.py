import json
from decimal import Decimal
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


PHASE_CHAIN = [
    "pre_launch",
    "stage_1_submitting",
    "stage_1_grading",
    "stage_1_review",
    "finalist_check",
    "results_published",
    "stage_2_live",
    "completed",
]
PHASE_INDEX = {name: i for i, name in enumerate(PHASE_CHAIN)}


def _allowed_next_phase(current: str):
    i = PHASE_INDEX.get(current)
    if i is None or i + 1 >= len(PHASE_CHAIN):
        return None
    return PHASE_CHAIN[i + 1]


@router.post("/phase")
async def update_competition_phase(payload: PhaseUpdateRequest, ctx=Depends(get_admin_context)):
    admin_id, session = ctx

    cur_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    current_phase = cur_res.scalar_one()

    if payload.new_phase != current_phase:
        allowed = _allowed_next_phase(current_phase)
        if payload.new_phase != allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Cannot transition from '{current_phase}' to '{payload.new_phase}'. "
                    f"Allowed next: '{allowed or 'none (terminal)'}'."
                ),
            )

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


@router.post("/finalists/select")
async def select_finalists(ctx=Depends(get_admin_context)):
    """Select the top-N finalists + blur-margin candidates from stage1_results.
    Forces a fresh recompute first to guard against stale stage1_results.
    Only callable in 'finalist_check' phase."""
    admin_id, session = ctx

    cur_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    current_phase = cur_res.scalar_one()
    if current_phase != "finalist_check":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Finalist selection only allowed in 'finalist_check' phase "
                f"(current: '{current_phase}')."
            ),
        )

    # Fresh recompute — wipes stale stage1_results (e.g. legacy corrupted rows)
    await session.execute(text("SELECT compute_stage1_results()"))

    # Select
    res = await session.execute(text("SELECT fn_select_finalists()"))
    admitted = res.scalar_one()
    if isinstance(admitted, str):
        admitted = json.loads(admitted)
    if admitted is None:
        admitted = []

    admitted_count = len(admitted)

    await session.execute(
        text(
            "INSERT INTO audit_log (actor_role, actor_id, action, target_type, details) "
            "VALUES ('admin', :aid, 'finalists_selected', 'finalists', CAST(:det AS jsonb))"
        ),
        {
            "aid": admin_id,
            "det": json.dumps({"admitted_count": admitted_count}),
        },
    )

    return {"admitted": admitted, "count": admitted_count}


class DisqualifyCandidateRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1000)


@router.post("/candidates/{participant_number}/disqualify")
async def disqualify_candidate(
    participant_number: int,
    payload: DisqualifyCandidateRequest,
    ctx=Depends(get_admin_context),
):
    """Manually disqualify a candidate. Sets is_disqualified + reason; excludes
    them from stage1_results on next recompute. Guarded recompute mirrors
    judge toggle: recompute runs in grading/review/finalist-check phases only."""
    admin_id, session = ctx

    cand_res = await session.execute(
        text("SELECT id, is_disqualified FROM candidates WHERE participant_number = :pno"),
        {"pno": participant_number},
    )
    cand = cand_res.mappings().first()
    if cand is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Participant {participant_number} not found.",
        )
    if cand["is_disqualified"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Participant {participant_number} is already disqualified.",
        )

    await session.execute(
        text(
            "UPDATE candidates SET is_disqualified = TRUE, "
            "disqualification_reason = :reason WHERE id = :cid"
        ),
        {"cid": cand["id"], "reason": payload.reason.strip()},
    )

    await session.execute(
        text(
            "INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details) "
            "VALUES ('admin', :aid, 'candidate_disqualified', 'candidate', :cid, CAST(:det AS jsonb))"
        ),
        {
            "aid": admin_id,
            "cid": cand["id"],
            "det": json.dumps({
                "participant_number": participant_number,
                "reason": payload.reason.strip(),
            }),
        },
    )

    phase_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    phase = phase_res.scalar_one()

    recompute_phases = ("stage_1_grading", "stage_1_review", "finalist_check")
    recomputed = False
    warning = None
    if phase in recompute_phases:
        await session.execute(text("SELECT compute_stage1_results()"))
        recomputed = True
    elif phase in ("results_published", "stage_2_live", "completed"):
        warning = (
            f"Candidate disqualified; leaderboard NOT recomputed (phase='{phase}'). "
            f"Call /compute-stage1 explicitly if you intend to change published results."
        )

    return {
        "participant_number": participant_number,
        "is_disqualified": True,
        "recomputed": recomputed,
        "warning": warning,
    }


class SettingsUpdateRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    weight_internal_judge: Optional[float] = Field(default=None, gt=0)
    weight_external_judge: Optional[float] = Field(default=None, gt=0)
    min_active_judges: Optional[int] = Field(default=None, ge=1)
    essay_weight_pct: Optional[float] = Field(default=None, gt=0, le=100)
    conclusion_weight_pct: Optional[float] = Field(default=None, gt=0, le=100)
    conclusion_floor_score: Optional[float] = Field(default=None, gt=0, le=30)
    criteria_weights: Optional[dict] = None
    stage2_criteria_weights: Optional[dict] = None
    stage1_overall_weight: Optional[float] = Field(default=None, gt=0, le=1)
    stage2_overall_weight: Optional[float] = Field(default=None, gt=0, le=1)
    top_finalists_target: Optional[int] = Field(default=None, ge=1)
    blur_margin: Optional[float] = Field(default=None, ge=0)
    question_word_limit: Optional[int] = Field(default=None, ge=1)
    answer_word_min: Optional[int] = Field(default=None, ge=1)
    answer_word_limit: Optional[int] = Field(default=None, ge=1)
    window_length_seconds: Optional[int] = Field(default=None, ge=60)
    grace_seconds: Optional[int] = Field(default=None, ge=0)
    phase2_window_seconds: Optional[int] = Field(default=None, ge=60)
    template_count: Optional[int] = Field(default=None, ge=1)
    run_gap: Optional[float] = Field(default=None, ge=0, le=1)
    model_gap: Optional[float] = Field(default=None, ge=0, le=1)
    similarity_threshold: Optional[float] = Field(default=None, ge=0, le=1)
    rubric_version: Optional[str] = Field(default=None, min_length=1, max_length=50)


@router.put("/settings")
async def update_settings(
    payload: SettingsUpdateRequest,
    ctx=Depends(get_admin_context),
):
    admin_id, session = ctx

    # Phase guard: block post-completion (competition frozen)
    cur_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    current_phase = cur_res.scalar_one()
    if current_phase == "completed":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Settings cannot be changed after the competition has completed.",
        )

    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one setting must be provided.",
        )

    # Read current active row (lock for the transaction)
    cur_res = await session.execute(
        text("SELECT * FROM settings WHERE is_active = TRUE FOR UPDATE")
    )
    current = cur_res.mappings().one()
    old_version = current["version"]
    merged = dict(current)
    # NUMERIC columns come back as Decimal; cast to float for arithmetic safety
    for _k, _v in list(merged.items()):
        if isinstance(_v, Decimal):
            merged[_k] = float(_v)

    changed = {}
    for key, new_val in updates.items():
        old_val = merged.get(key)
        if old_val != new_val:
            changed[key] = {"from": old_val, "to": new_val}
            merged[key] = new_val

    if not changed:
        return {
            "old_version": old_version,
            "new_version": old_version,
            "changed_fields": {},
            "message": "No changes: submitted values already match active settings.",
        }

    # Cross-field validation on the MERGED row (not the raw payload)
    if merged["answer_word_min"] >= merged["answer_word_limit"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"answer_word_min ({merged['answer_word_min']}) must be "
                f"less than answer_word_limit ({merged['answer_word_limit']})."
            ),
        )

    try:
        cw_sum = sum(float(v) for v in merged["criteria_weights"].values())
    except (AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="criteria_weights must be a JSON object of numeric values.",
        )
    if abs(cw_sum - 1.0) > 0.001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"criteria_weights must sum to 1.0 (got {cw_sum}).",
        )

    try:
        s2_sum = sum(float(v) for v in merged["stage2_criteria_weights"].values())
    except (AttributeError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stage2_criteria_weights must be a JSON object of numeric values.",
        )
    if abs(s2_sum - 1.0) > 0.001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stage2_criteria_weights must sum to 1.0 (got {s2_sum}).",
        )

    pct_sum = merged["essay_weight_pct"] + merged["conclusion_weight_pct"]
    if abs(pct_sum - 100) > 0.001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"essay_weight_pct + conclusion_weight_pct must equal 100 (got {pct_sum}).",
        )

    overall_sum = merged["stage1_overall_weight"] + merged["stage2_overall_weight"]
    if abs(overall_sum - 1.0) > 0.001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stage1_overall_weight + stage2_overall_weight must equal 1.0 (got {overall_sum}).",
        )

    # Compute new version
    new_version = (await session.execute(
        text("SELECT COALESCE(MAX(version), 0) + 1 FROM settings")
    )).scalar_one()

    # Deactivate old, insert new
    await session.execute(text("UPDATE settings SET is_active = FALSE WHERE is_active = TRUE"))
    await session.execute(
        text("""
            INSERT INTO settings (
                version, is_active,
                weight_internal_judge, weight_external_judge, min_active_judges,
                essay_weight_pct, conclusion_weight_pct, conclusion_floor_score,
                criteria_weights, stage2_criteria_weights,
                stage1_overall_weight, stage2_overall_weight,
                top_finalists_target, blur_margin,
                question_word_limit, answer_word_min, answer_word_limit,
                window_length_seconds, grace_seconds, phase2_window_seconds,
                template_count, run_gap, model_gap, similarity_threshold,
                rubric_version
            ) VALUES (
                :version, TRUE,
                :w_int, :w_ext, :min_judges,
                :essay_pct, :conc_pct, :conc_floor,
                CAST(:cw AS jsonb), CAST(:s2cw AS jsonb),
                :s1_overall, :s2_overall,
                :top_target, :blur,
                :q_limit, :a_min, :a_limit,
                :window_len, :grace, :phase2_win,
                :tmpl_count, :run_gap, :model_gap, :sim_thresh,
                :rubric_ver
            )
        """),
        {
            "version": new_version,
            "w_int": float(merged["weight_internal_judge"]),
            "w_ext": float(merged["weight_external_judge"]),
            "min_judges": int(merged["min_active_judges"]),
            "essay_pct": float(merged["essay_weight_pct"]),
            "conc_pct": float(merged["conclusion_weight_pct"]),
            "conc_floor": float(merged["conclusion_floor_score"]),
            "cw": json.dumps(merged["criteria_weights"]),
            "s2cw": json.dumps(merged["stage2_criteria_weights"]),
            "s1_overall": float(merged["stage1_overall_weight"]),
            "s2_overall": float(merged["stage2_overall_weight"]),
            "top_target": int(merged["top_finalists_target"]),
            "blur": float(merged["blur_margin"]),
            "q_limit": int(merged["question_word_limit"]),
            "a_min": int(merged["answer_word_min"]),
            "a_limit": int(merged["answer_word_limit"]),
            "window_len": int(merged["window_length_seconds"]),
            "grace": int(merged["grace_seconds"]),
            "phase2_win": int(merged["phase2_window_seconds"]),
            "tmpl_count": int(merged["template_count"]),
            "run_gap": float(merged["run_gap"]),
            "model_gap": float(merged["model_gap"]),
            "sim_thresh": float(merged["similarity_threshold"]),
            "rubric_ver": str(merged["rubric_version"]),
        },
    )

    await session.execute(
        text(
            "INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details) "
            "VALUES ('admin', :aid, 'settings_versioned', 'settings', :new_ver, CAST(:det AS jsonb))"
        ),
        {
            "aid": admin_id,
            "new_ver": new_version,
            "det": json.dumps({
                "old_version": old_version,
                "new_version": new_version,
                "changed_fields": changed,
            }),
        },
    )

    return {
        "old_version": old_version,
        "new_version": new_version,
        "changed_fields": changed,
    }


class StartStage1Request(BaseModel):
    duration_hours: float = Field(..., gt=0, le=720)


@router.post("/stage-1/start")
async def start_stage_1(
    payload: StartStage1Request,
    ctx=Depends(get_admin_context),
):
    admin_id, session = ctx

    # Phase guard: must be pre_launch
    cur_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    current_phase = cur_res.scalar_one()
    if current_phase != "pre_launch":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Cannot start Stage 1 from '{current_phase}'. "
                f"Stage 1 can only be started from 'pre_launch'."
            ),
        )

    # Read window_length_seconds from the active settings row
    cfg_res = await session.execute(
        text("SELECT window_length_seconds FROM settings WHERE is_active = TRUE")
    )
    window_length_seconds = cfg_res.scalar_one()

    duration_secs = int(payload.duration_hours * 3600)
    closing_secs = duration_secs + window_length_seconds

    update_res = await session.execute(
        text(
            """
            UPDATE competition_state
            SET current_phase = 'stage_1_submitting',
                stage_1_submission_deadline = NOW() + make_interval(secs => :dur_secs),
                stage_1_conclusion_closing_time = NOW() + make_interval(secs => :closing_secs)
            WHERE id = 1
            RETURNING stage_1_submission_deadline, stage_1_conclusion_closing_time
            """
        ),
        {"dur_secs": duration_secs, "closing_secs": closing_secs},
    )
    row = update_res.mappings().one()
    deadline = row["stage_1_submission_deadline"]
    closing_time = row["stage_1_conclusion_closing_time"]

    await session.execute(
        text(
            "INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details) "
            "VALUES ('admin', :aid, 'stage_1_started', 'competition_state', 1, CAST(:det AS jsonb))"
        ),
        {
            "aid": admin_id,
            "det": json.dumps({
                "duration_hours": payload.duration_hours,
                "duration_secs": duration_secs,
                "window_length_seconds": window_length_seconds,
                "deadline": deadline.isoformat() if deadline else None,
                "closing_time": closing_time.isoformat() if closing_time else None,
            }),
        },
    )

    return {
        "current_phase": "stage_1_submitting",
        "duration_hours": payload.duration_hours,
        "deadline": deadline,
        "closing_time": closing_time,
    }


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


class ToggleJudgeRequest(BaseModel):
    is_active: bool
    reason: Optional[str] = None


@router.post("/judges/{judge_id}/active")
async def toggle_judge_active(
    judge_id: int,
    payload: ToggleJudgeRequest,
    ctx=Depends(get_admin_context),
):
    admin_id, session = ctx

    # Require reason on deactivation
    if not payload.is_active and (not payload.reason or not payload.reason.strip()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A non-empty reason is required when deactivating a judge.",
        )

    exist = await session.execute(
        text("SELECT id FROM judges WHERE id = :jid"),
        {"jid": judge_id},
    )
    if exist.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Judge {judge_id} not found.",
        )

    if payload.is_active:
        await session.execute(
            text(
                "UPDATE judges SET is_active = TRUE, deactivated_at = NULL, "
                "deactivation_reason = NULL WHERE id = :jid"
            ),
            {"jid": judge_id},
        )
        action = "judge_reactivated"
    else:
        await session.execute(
            text(
                "UPDATE judges SET is_active = FALSE, deactivated_at = NOW(), "
                "deactivation_reason = :reason WHERE id = :jid"
            ),
            {"jid": judge_id, "reason": payload.reason.strip()},
        )
        action = "judge_deactivated"

    await session.execute(
        text(
            "INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details) "
            "VALUES ('admin', :aid, :action, 'judge', :jid, CAST(:det AS jsonb))"
        ),
        {
            "aid": admin_id,
            "action": action,
            "jid": judge_id,
            "det": json.dumps({
                "is_active": payload.is_active,
                "reason": payload.reason,
            }),
        },
    )

    phase_res = await session.execute(
        text("SELECT current_phase FROM competition_state WHERE id = 1")
    )
    phase = phase_res.scalar_one()

    recompute_phases = ("stage_1_grading", "stage_1_review", "finalist_check")
    recomputed = False
    warning = None
    if phase in recompute_phases:
        await session.execute(text("SELECT compute_stage1_results()"))
        recomputed = True
    else:
        warning = (
            f"Judge {'reactivated' if payload.is_active else 'deactivated'}; "
            f"leaderboard NOT recomputed (phase='{phase}'). "
            f"Call /compute-stage1 explicitly if you intend to change scores."
        )

    return {
        "judge_id": judge_id,
        "is_active": payload.is_active,
        "recomputed": recomputed,
        "warning": warning,
    }


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
    rows = [dict(r) for r in res.mappings().all()]

    await session.execute(
        text(
            """
            INSERT INTO audit_log
                (actor_role, actor_id, action, target_type, target_id, details)
            VALUES
                ('admin', :aid, 'stage1_leaderboard_viewed_with_identity',
                 'stage1_results', NULL, CAST(:det AS jsonb))
            """
        ),
        {"aid": admin_id, "det": json.dumps({"row_count": len(rows)})},
    )

    return {"leaderboard": rows}


@router.get("/names")
async def list_candidate_names(ctx=Depends(get_admin_context)):
    """Return the participant_number -> real identity mapping. Admin only.
    Every call writes exactly one audit_log row."""
    admin_id, session = ctx
    res = await session.execute(
        text(
            """
            SELECT participant_number, full_name, matric_number, whatsapp_number
            FROM candidates
            ORDER BY participant_number ASC
            """
        )
    )
    rows = [dict(r) for r in res.mappings().all()]

    await session.execute(
        text(
            """
            INSERT INTO audit_log
                (actor_role, actor_id, action, target_type, target_id, details)
            VALUES
                ('admin', :aid, 'identity_mapping_viewed', 'candidates', NULL,
                 CAST(:det AS jsonb))
            """
        ),
        {"aid": admin_id, "det": json.dumps({"row_count": len(rows)})},
    )

    return {"mapping": rows, "count": len(rows)}


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
    rows = [dict(r) for r in res.mappings().all()]

    await session.execute(
        text(
            """
            INSERT INTO audit_log
                (actor_role, actor_id, action, target_type, target_id, details)
            VALUES
                ('admin', :aid, 'final_standings_viewed_with_identity',
                 'final_results', NULL, CAST(:det AS jsonb))
            """
        ),
        {"aid": admin_id, "det": json.dumps({"row_count": len(rows)})},
    )

    return {"final_standings": rows}


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
