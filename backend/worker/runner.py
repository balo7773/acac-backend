import asyncio
import json
import logging
import re
from typing import Dict, List, Set, Tuple

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_rls_session
from app.routers.candidates import FALLBACK_QUESTIONS
from app.core.config import settings
from app.services.ai.client import call_deepseek, call_gemma, AIClientError
from app.services.ai.prompts.job_a_templates import (
    JOB_A_TEMPLATES, JOB_A_PERSONALIZE_PROMPT, choose_template, plain_template
)
from app.services.ai.prompts.job_c_templates import (
    JOB_C_AUDIT_PROMPT, validate_audit_response
)
from app.services.pdf_service import locate_verbatim_quote

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("acac.worker")

CRITERIA_KEYS = ("c1", "c2", "c3", "c4", "c5")


def extract_8word_shingles(canonical_text: str) -> Tuple[List[str], Set[str]]:
    words = re.findall(r"\b\w+\b", canonical_text.lower())
    if len(words) < 8:
        return words, set()
    shingles = {" ".join(words[i : i + 8]) for i in range(len(words) - 7)}
    return words, shingles


async def sweep_expired_timers(session: AsyncSession) -> Tuple[int, int]:
    """
    Sweeps expired candidate conclusion windows -> auto_zero
    and expired judge Phase 2 review sessions -> locked_final.
    """
    cfg_res = await session.execute(
        text("SELECT grace_seconds FROM settings WHERE is_active = TRUE")
    )
    g_val = cfg_res.scalar_one_or_none()
    grace = 10 if g_val is None else g_val

    # 1. Expired candidate conclusion windows (both in_progress and preparing past closing time)
    win_res = await session.execute(
        text(
            """
            UPDATE conclusion_windows cw
            SET outcome = 'auto_zero'
            WHERE (
                cw.outcome = 'in_progress'
                AND NOW() > (cw.window_ends_at + make_interval(secs => :grace))
            ) OR (
                cw.outcome = 'preparing'
                AND EXISTS (
                    SELECT 1 FROM competition_state cs
                    WHERE cs.id = 1
                      AND cs.stage_1_conclusion_closing_time IS NOT NULL
                      AND NOW() > (cs.stage_1_conclusion_closing_time + make_interval(secs => :grace))
                )
            )
            RETURNING cw.submission_id
            """
        ),
        {"grace": grace},
    )
    expired_subs = [r[0] for r in win_res.all()]
    for sid in expired_subs:
        await session.execute(
            text(
                """
                INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id)
                VALUES ('worker', NULL, 'conclusion_expired_auto_zero', 'submission', :sid)
                """
            ),
            {"sid": sid},
        )

    # 2. Expired judge Phase 2 review windows
    js_res = await session.execute(
        text(
            """
            UPDATE judge_sessions
            SET status = 'locked_final'
            WHERE status = 'phase_2_review'
              AND NOW() > (phase_2_expires_at + make_interval(secs => :grace))
            RETURNING id, submission_id, judge_id
            """
        ),
        {"grace": grace},
    )
    expired_sessions = js_res.all()
    for row in expired_sessions:
        await session.execute(
            text(
                """
                INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
                VALUES ('worker', :jid, 'phase_2_expired_locked', 'submission', :sid,
                        jsonb_build_object('judge_session_id', CAST(:jsid AS integer)))
                """
            ),
            {"jid": row.judge_id, "sid": row.submission_id, "jsid": row.id},
        )

    return len(expired_subs), len(expired_sessions)


async def _generate_Simulated_or_api_eval(
    model_name: str, run_id: int, canonical_text: str
) -> dict:
    """
    Produces structured per-criterion evaluations (c1..c5) and extracted claims.
    AI_MODE=real  -> real LLM via ai.client
    AI_MODE=stub  -> deterministic grounded extraction (default; used in tests)
    """
    if settings.AI_MODE == "real":
        return await _generate_real_eval(model_name, run_id, canonical_text)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", canonical_text) if len(s.strip()) > 20]
    if not sentences:
        sentences = [canonical_text[:120]]

    criteria_out = {}
    base_offset = 0.02 if model_name == "gemma" else 0.0
    run_offset = 0.01 if run_id == 2 else 0.0

    for idx, ckey in enumerate(CRITERIA_KEYS):
        quote = sentences[idx % len(sentences)]
        score = round(min(0.95, 0.72 + (idx * 0.03) + base_offset + run_offset), 4)
        criteria_out[ckey] = {
            "description_text": f"Objective summary ({model_name.upper()}): The essay addresses {ckey.upper()} by focusing on '{quote[:65]}...'",
            "reason_text": f"Evaluative reasoning ({model_name.upper()}): Criterion {ckey.upper()} demonstrates structured alignment with rubric bands.",
            "mark": score,
            "quote_text": quote,
        }

    claims_out = []
    if run_id == 1 and sentences:
        claims_out.append(
            {
                "claim_type": "policy_outcome",
                "claim_text": f"Core policy assertion identified by {model_name}.",
                "quote_text": sentences[0],
            }
        )

    return {"criteria": criteria_out, "claims": claims_out, "model_version": f"{model_name}-v1"}


async def execute_job(session: AsyncSession, job: dict) -> dict:
    jtype = job["job_type"]
    sid = job["submission_id"]
    model_name = job["model_name"]
    run_id = job["run_id"]

    if jtype == "job_a":
        res = await session.execute(
            text(
                """
                SELECT s.canonical_text, cw.template_id, cw.outcome
                FROM submissions s
                JOIN conclusion_windows cw ON cw.submission_id = s.id
                WHERE s.id = :sid
                """
            ),
            {"sid": sid},
        )
        row = res.first()
        if not row:
            return {"status": "skipped_no_submission"}

        # Choose template deterministically from submission_id
        tid, tstr = choose_template(sid)
        q_text, q_source, is_fb = await _generate_question_a(
            sid, row.canonical_text, tid, tstr
        )
        if row.outcome == "preparing":
            await session.execute(
                text(
                    """
                    UPDATE conclusion_windows cw
                    SET outcome = CASE
                            WHEN cs.stage_1_conclusion_closing_time IS NOT NULL AND NOW() >= cs.stage_1_conclusion_closing_time
                            THEN 'auto_zero'
                            ELSE 'in_progress'
                        END,
                        question_text = :q,
                        question_source = :qsrc,
                        is_fallback_wording = :is_fb,
                        template_id = :tid,
                        question_delivered_at = NOW(),
                        window_ends_at = CASE
                            WHEN cs.stage_1_conclusion_closing_time IS NOT NULL
                            THEN LEAST(NOW() + make_interval(secs => s.window_length_seconds), cs.stage_1_conclusion_closing_time)
                            ELSE NOW() + make_interval(secs => s.window_length_seconds)
                        END
                    FROM settings s
                    CROSS JOIN competition_state cs
                    WHERE s.is_active = TRUE AND cs.id = 1
                      AND cw.submission_id = :sid AND cw.outcome = 'preparing'
                    """
                ),
                {"q": q_text, "qsrc": q_source, "is_fb": is_fb, "tid": tid, "sid": sid},
            )
        return {"question_prepared": q_text}

    elif jtype == "job_b_run":
        sub_res = await session.execute(
            text("SELECT canonical_text FROM submissions WHERE id = :sid"),
            {"sid": sid},
        )
        canonical_text = sub_res.scalar_one()
        payload = await _generate_Simulated_or_api_eval(model_name, run_id, canonical_text)

        # Record extracted claims on run 1
        if run_id == 1:
            for clm in payload.get("claims", []):
                _, q_start, q_end = locate_verbatim_quote(canonical_text, clm["quote_text"])
                await session.execute(
                    text(
                        """
                        INSERT INTO ai_claims (submission_id, model_name, claim_type, claim_text, quote_text, quote_start, quote_end)
                        VALUES (:sid, :mname, :ctype, :ctext, :qtext, :qstart, :qend)
                        """
                    ),
                    {
                        "sid": sid,
                        "mname": model_name,
                        "ctype": clm["claim_type"],
                        "ctext": clm["claim_text"],
                        "qtext": clm["quote_text"],
                        "qstart": q_start,
                        "qend": q_end,
                    },
                )

        # Check if sibling run for this model is already done; if so, enqueue job_b_combine
        await session.execute(
            text(
                """
                INSERT INTO ai_jobs (job_type, submission_id, model_name, run_id)
                VALUES ('job_b_combine', :sid, :mname, 1)
                ON CONFLICT DO NOTHING
                """
            ),
            {"sid": sid, "mname": model_name},
        )
        return payload

    elif jtype == "job_b_combine":
        other_model = "gemma" if model_name == "deepseek" else "deepseek"
        other_res = await session.execute(
            text(
                "SELECT 1 FROM ai_evaluations "
                "WHERE submission_id = :sid AND model_name = :m LIMIT 1"
            ),
            {"sid": sid, "m": other_model},
        )
        other_has_data = other_res.first() is not None

        runs_res = await session.execute(
            text(
                """
                SELECT run_id, raw_response
                FROM ai_jobs
                WHERE submission_id = :sid AND job_type = 'job_b_run'
                  AND model_name = :mname AND status = 'done'
                ORDER BY run_id ASC
                """
            ),
            {"sid": sid, "mname": model_name},
        )
        runs = runs_res.all()
        if len(runs) < 2:
            return {"status": "waiting_for_sibling_run"}

        r1 = runs[0].raw_response if isinstance(runs[0].raw_response, dict) else json.loads(runs[0].raw_response)
        r2 = runs[1].raw_response if isinstance(runs[1].raw_response, dict) else json.loads(runs[1].raw_response)

        sub_res = await session.execute(
            text(
                "SELECT s.canonical_text, st.run_gap, st.model_gap, st.rubric_version "
                "FROM submissions s CROSS JOIN settings st WHERE s.id = :sid AND st.is_active = TRUE"
            ),
            {"sid": sid},
        )
        sub_row = sub_res.one()

        for ckey in CRITERIA_KEYS:
            c1_data = r1["criteria"][ckey]
            c2_data = r2["criteria"][ckey]
            m1 = float(c1_data["mark"])
            m2 = float(c2_data["mark"])
            avg_mark = round((m1 + m2) / 2.0, 4)

            markers = []
            if abs(m1 - m2) > float(sub_row.run_gap):
                markers.append("unstable")

            ev_status, q_start, q_end = locate_verbatim_quote(
                sub_row.canonical_text, c1_data.get("quote_text", "")
            )
            if ev_status != "verified":
                markers.append("unverified_quote")

            if not other_has_data:
                markers.append("single_run")

            await session.execute(
                text(
                    """
                    INSERT INTO ai_evaluations (
                        submission_id, model_name, criterion_key, description_text, reason_text,
                        mark_run1, mark_run2, mark, quote_text, quote_start, quote_end,
                        evidence_status, markers, prompt_version, model_version
                    ) VALUES (
                        :sid, :mname, :ckey, :desc, :reason,
                        :m1, :m2, :mavg, :qtext, :qstart, :qend,
                        :evstatus, :markers, :pver, :mver
                    )
                    ON CONFLICT (submission_id, model_name, criterion_key) DO UPDATE
                    SET mark_run1 = EXCLUDED.mark_run1,
                        mark_run2 = EXCLUDED.mark_run2,
                        mark = EXCLUDED.mark,
                        markers = EXCLUDED.markers
                    """
                ),
                {
                    "sid": sid,
                    "mname": model_name,
                    "ckey": ckey,
                    "desc": c1_data["description_text"],
                    "reason": c1_data["reason_text"],
                    "m1": m1,
                    "m2": m2,
                    "mavg": avg_mark,
                    "qtext": c1_data.get("quote_text"),
                    "qstart": q_start,
                    "qend": q_end,
                    "evstatus": ev_status,
                    "markers": markers,
                    "pver": sub_row.rubric_version,
                    "mver": r1.get("model_version", f"{model_name}-v1"),
                },
            )
                # Check cross-model discrepancy if sibling model evaluations exist
        other_model = "gemma" if model_name == "deepseek" else "deepseek"
        other_evals_res = await session.execute(
            text(
                "SELECT criterion_key, mark, markers FROM ai_evaluations "
                "WHERE submission_id = :sid AND model_name = :mname"
            ),
            {"sid": sid, "mname": other_model},
        )
        other_evals = {r.criterion_key: (float(r.mark), list(r.markers or [])) for r in other_evals_res.all()}

        if other_evals:
            mgap = float(sub_row.model_gap)
            curr_evals_res = await session.execute(
                text(
                    "SELECT criterion_key, mark, markers FROM ai_evaluations "
                    "WHERE submission_id = :sid AND model_name = :mname"
                ),
                {"sid": sid, "mname": model_name},
            )
            for curr in curr_evals_res.all():
                ckey = curr.criterion_key
                if ckey in other_evals:
                    o_mark, o_markers = other_evals[ckey]
                    c_mark = float(curr.mark)
                    c_markers = list(curr.markers or [])

                    if abs(c_mark - o_mark) > mgap:
                        if "discrepant" not in c_markers:
                            c_markers.append("discrepant")
                        if "discrepant" not in o_markers:
                            o_markers.append("discrepant")
                    else:
                        c_markers = [m for m in c_markers if m != "discrepant"]
                        o_markers = [m for m in o_markers if m != "discrepant"]

                    await session.execute(
                        text("UPDATE ai_evaluations SET markers = :m WHERE submission_id = :sid AND model_name = :mn AND criterion_key = :ck"),
                        {"m": c_markers, "sid": sid, "mn": model_name, "ck": ckey},
                    )
                    await session.execute(
                        text("UPDATE ai_evaluations SET markers = :m WHERE submission_id = :sid AND model_name = :mn AND criterion_key = :ck"),
                        {"m": o_markers, "sid": sid, "mn": other_model, "ck": ckey},
                    )

        return {"combined_model": model_name}

    elif jtype == "job_b_summary":
        # One summary per submission. Uses only the essay text (no dependency on
        # the per-criterion evaluations), so it can run independently of job_b_run.
        res = await session.execute(
            text("SELECT canonical_text FROM submissions WHERE id = :sid"),
            {"sid": sid},
        )
        row = res.first()
        if not row or not row.canonical_text:
            return {"status": "skipped_no_essay"}

        if settings.AI_MODE == "real":
            try:
                from app.services.ai.prompts.summary import SUMMARY_PROMPT
                prompt = SUMMARY_PROMPT.replace("{essay}", row.canonical_text)
                result = await call_deepseek(prompt, max_tokens=2000, temperature=0.3)
                summary_text = (result["parsed"].get("summary") or "").strip()[:2000]
                if not summary_text or len(summary_text) < 40:
                    raise AIClientError(
                        f"summary too short (len={len(summary_text)})", category="PARSE"
                    )
                model_used = result.get("model_version", "deepseek-v4-pro")
            except (AIClientError, KeyError) as e:
                logger.warning("Summary generation failed sid=%s: %s", sid, e)
                return {"status": "failed", "error": str(e)}
        else:
            # Stub: first 200 chars of essay, marked so it's obvious in the DB
            summary_text = f"[STUB SUMMARY] {row.canonical_text[:200].strip()}"
            model_used = "stub"

        await session.execute(
            text(
                """
                INSERT INTO ai_summaries (submission_id, summary_text, model_name)
                VALUES (:sid, :st, :m)
                ON CONFLICT (submission_id) DO UPDATE
                SET summary_text = EXCLUDED.summary_text,
                    model_name = EXCLUDED.model_name,
                    created_at = NOW()
                """
            ),
            {"sid": sid, "st": summary_text, "m": model_used},
        )
        return {"status": "done", "length": len(summary_text), "model": model_used}

    elif jtype == "job_c_run":
        res = await session.execute(
            text(
                """
                SELECT s.canonical_text, cw.answer_text, cw.question_text
                FROM submissions s
                JOIN conclusion_windows cw ON cw.submission_id = s.id
                WHERE s.id = :sid
                """
            ),
            {"sid": sid},
        )
        row = res.one()

        if settings.AI_MODE == "real":
            try:
                audit = await _conclusion_audit_real(
                    row.canonical_text,
                    row.question_text,
                    row.answer_text or "",
                    model_name,
                )
            except (AIClientError, ValueError) as e:
                logger.warning("Job C real audit failed sid=%s: %s — falling back to stub", sid, e)
                audit = _conclusion_audit_stub(row.canonical_text, row.answer_text or "")
        else:
            audit = _conclusion_audit_stub(row.canonical_text, row.answer_text or "")

        await session.execute(
            text(
                """
                INSERT INTO ai_conclusion_audits (
                    submission_id, model_name, label, essay_quote, conclusion_quote,
                    quotes_verified, addresses_question, reference_mark
                ) VALUES (
                    :sid, :mname, :label, :eq, :cq, :qv, :addr, :rmark
                )
                ON CONFLICT (submission_id, model_name) DO UPDATE
                SET label = EXCLUDED.label,
                    essay_quote = EXCLUDED.essay_quote,
                    conclusion_quote = EXCLUDED.conclusion_quote,
                    quotes_verified = EXCLUDED.quotes_verified,
                    addresses_question = EXCLUDED.addresses_question,
                    reference_mark = EXCLUDED.reference_mark
                """
            ),
            {
                "sid": sid,
                "mname": model_name,
                "label": audit["label"],
                "eq": audit["essay_quote"],
                "cq": audit["conclusion_quote"],
                "qv": audit["quotes_verified"],
                "addr": audit["addresses_question"],
                "rmark": audit["reference_mark"],
            },
        )

        await session.execute(
            text(
                """
                INSERT INTO ai_jobs (job_type, submission_id, model_name, run_id)
                VALUES ('job_c_combine', :sid, 'none', 1)
                ON CONFLICT DO NOTHING
                """
            ),
            {"sid": sid},
        )
        return {"model": model_name, "label": audit["label"]}

    elif jtype == "job_c_combine":
        audits_res = await session.execute(
            text(
                "SELECT model_name, label, addresses_question FROM ai_conclusion_audits WHERE submission_id = :sid"
            ),
            {"sid": sid},
        )
        audits = audits_res.all()
        if len(audits) == 2:
            if all(a.label == "contradictory" for a in audits):
                await session.execute(
                    text(
                        """
                        INSERT INTO flags (submission_id, flag_type, raised_by, evidence)
                        SELECT :sid, 'contradicts_essay', 'ai', CAST(:ev AS jsonb)
                        WHERE NOT EXISTS (
                            SELECT 1 FROM flags WHERE submission_id = :sid AND flag_type = 'contradicts_essay' AND raised_by = 'ai'
                        )
                        """
                    ),
                    {"sid": sid, "ev": json.dumps({"consensus": "both_models_contradictory"})},
                )
        return {"audits_checked": len(audits)}

    elif jtype == "job_d":
        cfg_res = await session.execute(
            text("SELECT similarity_threshold FROM settings WHERE is_active = TRUE")
        )
        threshold = float(cfg_res.scalar_one() or 0.15)

        subs_res = await session.execute(
            text(
                "SELECT id, canonical_text FROM submissions "
                "WHERE status IN ('submitted', 'auto_locked') AND canonical_text IS NOT NULL "
                "ORDER BY id ASC"
            )
        )
        subs = subs_res.all()
        shingle_map: Dict[int, Set[str]] = {}
        for s in subs:
            _, sh = extract_8word_shingles(s.canonical_text)
            shingle_map[s.id] = sh

        pairs_flagged = 0
        for i in range(len(subs)):
            for j in range(i + 1, len(subs)):
                id_a, id_b = subs[i].id, subs[j].id
                sh_a, sh_b = shingle_map[id_a], shingle_map[id_b]
                if not sh_a or not sh_b:
                    continue
                shared = sh_a.intersection(sh_b)
                score = round(len(shared) / min(len(sh_a), len(sh_b)), 4)
                if score >= threshold:
                    pairs_flagged += 1
                    await session.execute(
                        text(
                            """
                            INSERT INTO similarity_pairs (submission_a_id, submission_b_id, similarity_score, shared_passages)
                            VALUES (:a, :b, :score, CAST(:shared AS jsonb))
                            ON CONFLICT (submission_a_id, submission_b_id) DO UPDATE
                            SET similarity_score = EXCLUDED.similarity_score,
                                shared_passages = EXCLUDED.shared_passages
                            """
                        ),
                        {
                            "a": id_a,
                            "b": id_b,
                            "score": score,
                            "shared": json.dumps(list(shared)[:10]),
                        },
                    )
        return {"pairs_flagged": pairs_flagged}

    return {"status": "unknown_job_type"}


async def process_next_queued_job() -> bool:
    """
    Claims a single queued job using FOR UPDATE SKIP LOCKED, executes it,
    and updates its status to 'done' or reschedules with exponential backoff.
    """
    async with get_rls_session("worker_role") as session:
        res = await session.execute(
            text(
                """
                SELECT id, job_type, submission_id, model_name, run_id, attempts
                FROM ai_jobs
                WHERE status = 'queued' AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at ASC, id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
        )
        job_row = res.mappings().first()
        if not job_row:
            return False

        job = dict(job_row)
        await session.execute(
            text("UPDATE ai_jobs SET status = 'running', started_at = NOW() WHERE id = :jid"),
            {"jid": job["id"]},
        )

        try:
            output = await execute_job(session, job)
            await session.execute(
                text(
                    """
                    UPDATE ai_jobs
                    SET status = 'done',
                        raw_response = CAST(:raw AS jsonb),
                        finished_at = NOW()
                    WHERE id = :jid
                    """
                ),
                {"jid": job["id"], "raw": json.dumps(output)},
            )
        except Exception as exc:
            attempts = job["attempts"] + 1
            new_status = "failed" if attempts >= 3 else "queued"
            await session.execute(
                text(
                    """
                    UPDATE ai_jobs
                    SET status = :st,
                        attempts = :att,
                        last_error = :err,
                        next_attempt_at = NOW() + make_interval(secs => :backoff)
                    WHERE id = :jid
                    """
                ),
                {
                    "jid": job["id"],
                    "st": new_status,
                    "att": attempts,
                    "err": f"[{getattr(exc, 'category', 'UNKNOWN')}] {str(exc)[:800]}",
                    "backoff": attempts * 15,
                },
            )
            logger.exception("Job %s failed (attempt %s): %s", job["id"], attempts, exc)

        return True


async def run_worker_forever(poll_interval: float = 2.0):
    logger.info("ACAC Background Worker started (role=worker_role).")
    while True:
        try:
            async with get_rls_session("worker_role") as session:
                await sweep_expired_timers(session)
            while await process_next_queued_job():
                pass
        except Exception as e:
            logger.error("Worker loop error: %s", e)
        await asyncio.sleep(poll_interval)


async def _generate_question_a(sid: int, canonical_text: str, template_id: int, template_str: str) -> tuple[str, str, bool]:
    """Return (question_text, question_source, is_fallback_wording).
    Tries real personalization first; falls back to plain template on any failure."""
    if settings.AI_MODE != "real":
        return plain_template(template_str), "backup", True

    prompt = JOB_A_PERSONALIZE_PROMPT.format(template=template_str, essay=canonical_text)
    try:
        result = await call_deepseek(prompt)
        parsed = result["parsed"]
        essay_quote = (parsed.get("essay_quote") or "").strip()
        question = (parsed.get("question") or "").strip()
        if not essay_quote or not question:
            raise AIClientError("empty essay_quote or question", category="PARSE")
        # Verify quote appears verbatim in essay
        essay_norm = " ".join(canonical_text.split())
        quote_norm = " ".join(essay_quote.split())
        if len(quote_norm) < 40 or quote_norm not in essay_norm:
            raise AIClientError(
                f"quote not verbatim in essay (len={len(quote_norm)})", category="PARSE"
            )
        return question, "deepseek", False
    except AIClientError as e:
        logger.warning("Job A personalization failed for sid=%s: %s", sid, e)
        return plain_template(template_str), "backup", True


def _conclusion_audit_stub(canonical_text: str, answer_text: str) -> dict:
    """Deterministic stub. Preserves exact prior behavior for tests."""
    ans_lower = (answer_text or "").lower()
    is_contra = "contradict" in ans_lower or "opposite" in ans_lower
    label = "contradictory" if is_contra else "consistent"
    return {
        "label": label,
        "essay_quote": canonical_text[:100],
        "conclusion_quote": (answer_text or "")[:100],
        "quotes_verified": True,
        "addresses_question": not is_contra,
        "reference_mark": 6.0 if is_contra else 24.0,
    }


async def _conclusion_audit_real(
    canonical_text: str, question_text: str, answer_text: str, model_name: str
) -> dict:
    """Call DeepSeek; verify quotes; return same shape as stub.
    Raises AIClientError or ValueError on failure so caller can fall back."""
    if model_name != "deepseek":
        raise AIClientError(f"Job C only supports deepseek, got {model_name}")

    prompt = JOB_C_AUDIT_PROMPT.format(
        essay=canonical_text,
        question=question_text or "(not recorded)",
        answer=answer_text or "",
    )
    result = await call_deepseek(prompt)
    return validate_audit_response(result["parsed"], canonical_text, answer_text or "")


async def _generate_real_eval(model_name: str, run_id: int, canonical_text: str) -> dict:
    """Real LLM evaluation. Same shape as the stub."""
    from app.services.ai.schemas import EVAL_PROMPT_TEMPLATE

    prompt = EVAL_PROMPT_TEMPLATE.replace("{{ESSAY}}", canonical_text)

    if model_name == "deepseek":
        result = await call_deepseek(prompt)
    elif model_name == "gemma":
        result = await call_gemma(prompt)
    else:
        raise AIClientError(f"Unknown model_name: {model_name}")

    parsed = result["parsed"]

    criteria_out = {}
    for ckey in CRITERIA_KEYS:
        entry = parsed.get("criteria", {}).get(ckey, {})
        mark = max(0.0, min(1.0, float(entry.get("mark", 0.0))))
        criteria_out[ckey] = {
            "description_text": str(entry.get("description_text", ""))[:2000],
            "reason_text": str(entry.get("reason_text", ""))[:2000],
            "mark": mark,
            "quote_text": str(entry.get("quote_text", ""))[:2000],
        }

    claims_out = []
    for clm in parsed.get("claims", []):
        ct = clm.get("claim_type", "")
        if ct not in ("statistic", "agency", "date", "policy_outcome"):
            continue
        claims_out.append({
            "claim_type": ct,
            "claim_text": str(clm.get("claim_text", ""))[:2000],
            "quote_text": str(clm.get("quote_text", ""))[:2000],
        })

    return {
        "criteria": criteria_out,
        "claims": claims_out if run_id == 1 else [],
        "model_version": result["model_version"],
    }


if __name__ == "__main__":
    asyncio.run(run_worker_forever())
