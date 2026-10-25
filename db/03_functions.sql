-- ============================================================================
-- ACAC 03_FUNCTIONS.SQL - ATOMIC STORED PROCEDURES & SCORING ENGINE
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. CANDIDATE SUBMISSION & CONCLUSION LIFECYCLE
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION submit_final_essay(
    p_submission_id INT,
    p_pdf_path TEXT,
    p_page_count INT,
    p_word_count INT,
    p_canonical_text TEXT,
    p_rules_version TEXT
) RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_candidate_id INT;
    v_phase TEXT;
    v_template_count INT;
    v_template_id INT;
BEGIN
    -- 1. Verify competition is accepting submissions
    SELECT current_phase INTO v_phase FROM competition_state WHERE id = 1;
    IF v_phase <> 'stage_1_submitting' THEN
        RAISE EXCEPTION 'Competition is not currently accepting Stage 1 submissions (Phase: %).', v_phase;
    END IF;

    -- 2. Verify ownership & draft status
    SELECT candidate_id INTO v_candidate_id
    FROM submissions
    WHERE id = p_submission_id AND status = 'draft'
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Submission % not found or already submitted.', p_submission_id;
    END IF;

    IF current_user_id() IS NOT NULL AND v_candidate_id <> current_user_id() THEN
        RAISE EXCEPTION 'Unauthorized: Submission does not belong to current candidate.';
    END IF;

    -- 3. Lock submission and freeze canonical_text
    UPDATE submissions
    SET status = 'submitted',
        pdf_path = p_pdf_path,
        page_count = p_page_count,
        word_count = p_word_count,
        canonical_text = p_canonical_text,
        rules_version = p_rules_version,
        rules_accepted_at = NOW(),
        final_submitted_at = NOW(),
        locked_at = NOW()
    WHERE id = p_submission_id;

    -- 4. Assign rotating template & initialize conclusion window
    SELECT template_count INTO v_template_count FROM settings WHERE is_active = TRUE;
    v_template_id := ((nextval('job_a_template_seq') - 1) % COALESCE(v_template_count, 12)) + 1;

    INSERT INTO conclusion_windows (submission_id, outcome, template_id)
    VALUES (p_submission_id, 'preparing', v_template_id);

    -- 5. Enqueue Job A (Question Gen) and 4x Job B runs (DeepSeek x2, Gemma x2)
    INSERT INTO ai_jobs (job_type, submission_id, model_name, run_id) VALUES
        ('job_a', p_submission_id, 'deepseek', 1),
        ('job_b_run', p_submission_id, 'deepseek', 1),
        ('job_b_run', p_submission_id, 'deepseek', 2),
        ('job_b_run', p_submission_id, 'gemma', 1),
        ('job_b_run', p_submission_id, 'gemma', 2)
    ON CONFLICT DO NOTHING;

    -- 6. Audit Log
    INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
    VALUES ('candidate', v_candidate_id, 'final_essay_submitted', 'submission', p_submission_id,
            jsonb_build_object('word_count', p_word_count, 'page_count', p_page_count, 'template_id', v_template_id));

    RETURN v_template_id;
END;
$$;

GRANT EXECUTE ON FUNCTION submit_final_essay(INT, TEXT, INT, INT, TEXT, TEXT) TO candidate_role, admin_role;


CREATE OR REPLACE FUNCTION submit_conclusion_answer(
    p_submission_id INT,
    p_answer_text TEXT
) RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_candidate_id INT;
    v_outcome TEXT;
    v_window_ends TIMESTAMPTZ;
    v_grace INT;
BEGIN
    SELECT s.candidate_id, cw.outcome, cw.window_ends_at
    INTO v_candidate_id, v_outcome, v_window_ends
    FROM submissions s
    JOIN conclusion_windows cw ON cw.submission_id = s.id
    WHERE s.id = p_submission_id
    FOR UPDATE OF cw;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Conclusion window for submission % not found.', p_submission_id;
    END IF;

    IF current_user_id() IS NOT NULL AND v_candidate_id <> current_user_id() THEN
        RAISE EXCEPTION 'Unauthorized: Submission does not belong to current candidate.';
    END IF;

    IF v_outcome <> 'in_progress' THEN
        RAISE EXCEPTION 'Conclusion window is not active (current state: %).', v_outcome;
    END IF;

    SELECT grace_seconds INTO v_grace FROM settings WHERE is_active = TRUE;

    -- Enforce strict deadline + network grace buffer
    IF NOW() > (v_window_ends + make_interval(secs => COALESCE(v_grace, 10))) THEN
        UPDATE conclusion_windows
        SET outcome = 'auto_zero'
        WHERE submission_id = p_submission_id;

        INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
        VALUES ('candidate', v_candidate_id, 'conclusion_late_auto_zero', 'submission', p_submission_id,
                jsonb_build_object('window_ends_at', v_window_ends, 'attempted_at', NOW()));

        RETURN 'auto_zero';
    END IF;

    -- Record valid answer
    UPDATE conclusion_windows
    SET outcome = 'answered',
        answer_text = p_answer_text,
        answer_submitted_at = NOW()
    WHERE submission_id = p_submission_id;

    -- Enqueue Job C (Conclusion Consistency Audit)
    INSERT INTO ai_jobs (job_type, submission_id, model_name, run_id) VALUES
        ('job_c_run', p_submission_id, 'deepseek', 1),
        ('job_c_run', p_submission_id, 'gemma', 1)
    ON CONFLICT DO NOTHING;

    INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id)
    VALUES ('candidate', v_candidate_id, 'conclusion_answered', 'submission', p_submission_id);

    RETURN 'answered';
END;
$$;

GRANT EXECUTE ON FUNCTION submit_conclusion_answer(INT, TEXT) TO candidate_role, admin_role;

-- ----------------------------------------------------------------------------
-- 2. BLIND JUDGE EVALUATION GATEKEEPER (PHASE 1 & PHASE 2)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION get_judge_evaluation_bundle(
    p_submission_id INT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_session judge_sessions%ROWTYPE;
    v_reveal_ai BOOLEAN := FALSE;
    v_ai_evals JSONB;
    v_ai_audits JSONB;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    -- 1. Ensure judge session exists for this submission
    INSERT INTO judge_sessions (submission_id, judge_id, status)
    VALUES (p_submission_id, v_judge_id, 'phase_1_in_progress')
    ON CONFLICT (submission_id, judge_id) DO NOTHING;

    SELECT * INTO v_session
    FROM judge_sessions
    WHERE submission_id = p_submission_id AND judge_id = v_judge_id
    FOR UPDATE;

    -- 2. Auto-transition expired Phase 2 window to locked_final
    IF v_session.status = 'phase_2_review' AND NOW() > v_session.phase_2_expires_at THEN
        UPDATE judge_sessions
        SET status = 'locked_final'
        WHERE id = v_session.id
        RETURNING * INTO v_session;
    END IF;

    -- 3. Determine whether AI marks and reasons are unlocked
    v_reveal_ai := (v_session.status IN ('phase_2_review', 'locked_final'));

    -- 4. Build AI Evaluations array (Masking marks & reasons in Phase 1)
    SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
            'model_name', ae.model_name,
            'criterion_key', ae.criterion_key,
            'description_text', ae.description_text,
            'quote_text', ae.quote_text,
            'quote_start', ae.quote_start,
            'quote_end', ae.quote_end,
            'evidence_status', ae.evidence_status,
            'reason_text', CASE WHEN v_reveal_ai THEN ae.reason_text ELSE NULL END,
            'mark', CASE WHEN v_reveal_ai THEN ae.mark ELSE NULL END,
            'mark_run1', CASE WHEN v_reveal_ai THEN ae.mark_run1 ELSE NULL END,
            'mark_run2', CASE WHEN v_reveal_ai THEN ae.mark_run2 ELSE NULL END,
            'markers', CASE WHEN v_reveal_ai THEN to_jsonb(ae.markers) ELSE '[]'::jsonb END
        ) ORDER BY ae.criterion_key, ae.model_name
    ), '[]'::jsonb)
    INTO v_ai_evals
    FROM ai_evaluations ae
    WHERE ae.submission_id = p_submission_id;

    -- 5. Build AI Conclusion Audits (Revealed only in Phase 2 / locked_final)
    IF v_reveal_ai THEN
        SELECT COALESCE(jsonb_agg(
            jsonb_build_object(
                'model_name', aca.model_name,
                'label', aca.label,
                'essay_quote', aca.essay_quote,
                'conclusion_quote', aca.conclusion_quote,
                'quotes_verified', aca.quotes_verified,
                'addresses_question', aca.addresses_question,
                'reference_mark', aca.reference_mark
            ) ORDER BY aca.model_name
        ), '[]'::jsonb)
        INTO v_ai_audits
        FROM ai_conclusion_audits aca
        WHERE aca.submission_id = p_submission_id;
    ELSE
        v_ai_audits := '[]'::jsonb;
    END IF;

    RETURN jsonb_build_object(
        'session_status', v_session.status,
        'opened_full_essay', v_session.opened_full_essay,
        'phase_1_locked_at', v_session.phase_1_locked_at,
        'phase_2_expires_at', v_session.phase_2_expires_at,
        'phase_2_submitted_at', v_session.phase_2_submitted_at,
        'revision_reason', v_session.revision_reason,
        'ai_evaluations', v_ai_evals,
        'ai_conclusion_audits', v_ai_audits
    );
END;
$$;

GRANT EXECUTE ON FUNCTION get_judge_evaluation_bundle(INT) TO judge_role, admin_role;


CREATE OR REPLACE FUNCTION lock_phase1_scores(
    p_submission_id INT,
    p_c1 NUMERIC,
    p_c2 NUMERIC,
    p_c3 NUMERIC,
    p_c4 NUMERIC,
    p_c5 NUMERIC,
    p_conclusion_score NUMERIC,
    p_zero_score_reason TEXT DEFAULT NULL
) RETURNS TIMESTAMPTZ
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_session_status TEXT;
    v_window_outcome TEXT;
    v_final_conc_score NUMERIC := p_conclusion_score;
    v_final_zero_reason TEXT := p_zero_score_reason;
    v_phase2_secs INT;
    v_expires_at TIMESTAMPTZ;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    SELECT status INTO v_session_status
    FROM judge_sessions
    WHERE submission_id = p_submission_id AND judge_id = v_judge_id
    FOR UPDATE;

    IF NOT FOUND OR v_session_status <> 'phase_1_in_progress' THEN
        RAISE EXCEPTION 'Session is not in Phase 1 (current status: %).', COALESCE(v_session_status, 'none');
    END IF;

    -- Check if candidate conclusion was auto_zero
    SELECT outcome INTO v_window_outcome
    FROM conclusion_windows
    WHERE submission_id = p_submission_id;

    IF v_window_outcome = 'auto_zero' THEN
        v_final_conc_score := 0;
        v_final_zero_reason := COALESCE(p_zero_score_reason, 'System auto-zero: Conclusion window expired or unanswered.');
    END IF;

    -- Insert initial essay scores
    INSERT INTO essay_scores (submission_id, judge_id, criterion_key, initial_raw_score) VALUES
        (p_submission_id, v_judge_id, 'c1', p_c1),
        (p_submission_id, v_judge_id, 'c2', p_c2),
        (p_submission_id, v_judge_id, 'c3', p_c3),
        (p_submission_id, v_judge_id, 'c4', p_c4),
        (p_submission_id, v_judge_id, 'c5', p_c5);

    -- Insert initial conclusion score
    INSERT INTO conclusion_scores (submission_id, judge_id, initial_raw_score, zero_score_reason)
    VALUES (p_submission_id, v_judge_id, v_final_conc_score, v_final_zero_reason);

    -- Calculate Phase 2 expiry (5 minutes by default)
    SELECT phase2_window_seconds INTO v_phase2_secs FROM settings WHERE is_active = TRUE;
    v_expires_at := NOW() + make_interval(secs => COALESCE(v_phase2_secs, 300));

    -- Transition session to phase_2_review
    UPDATE judge_sessions
    SET status = 'phase_2_review',
        phase_1_locked_at = NOW(),
        phase_2_expires_at = v_expires_at
    WHERE submission_id = p_submission_id AND judge_id = v_judge_id;

    INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
    VALUES ('judge', v_judge_id, 'phase_1_scores_locked', 'submission', p_submission_id,
            jsonb_build_object('c1', p_c1, 'c2', p_c2, 'c3', p_c3, 'c4', p_c4, 'c5', p_c5, 'conclusion', v_final_conc_score));

    RETURN v_expires_at;
END;
$$;

GRANT EXECUTE ON FUNCTION lock_phase1_scores(INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC, TEXT) TO judge_role;


CREATE OR REPLACE FUNCTION submit_phase2_revisions(
    p_submission_id INT,
    p_c1 NUMERIC DEFAULT NULL,
    p_c2 NUMERIC DEFAULT NULL,
    p_c3 NUMERIC DEFAULT NULL,
    p_c4 NUMERIC DEFAULT NULL,
    p_c5 NUMERIC DEFAULT NULL,
    p_conclusion_score NUMERIC DEFAULT NULL,
    p_zero_score_reason TEXT DEFAULT NULL,
    p_revision_reason TEXT DEFAULT NULL
) RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_session judge_sessions%ROWTYPE;
    v_grace INT;
    v_any_change BOOLEAN := FALSE;
    v_init_conc NUMERIC;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    SELECT * INTO v_session
    FROM judge_sessions
    WHERE submission_id = p_submission_id AND judge_id = v_judge_id
    FOR UPDATE;

    IF NOT FOUND OR v_session.status <> 'phase_2_review' THEN
        RAISE EXCEPTION 'Session is not in Phase 2 review (status: %).', COALESCE(v_session.status, 'none');
    END IF;

    SELECT grace_seconds INTO v_grace FROM settings WHERE is_active = TRUE;

    -- Check if 5-minute window has already expired
    IF NOW() > (v_session.phase_2_expires_at + make_interval(secs => COALESCE(v_grace, 10))) THEN
        UPDATE judge_sessions
        SET status = 'locked_final'
        WHERE id = v_session.id;

        RETURN 'expired_locked';
    END IF;

    -- Detect if any score was modified from initial_raw_score
    SELECT EXISTS (
        SELECT 1 FROM essay_scores
        WHERE submission_id = p_submission_id AND judge_id = v_judge_id
          AND (
              (criterion_key = 'c1' AND p_c1 IS NOT NULL AND p_c1 IS DISTINCT FROM initial_raw_score) OR
              (criterion_key = 'c2' AND p_c2 IS NOT NULL AND p_c2 IS DISTINCT FROM initial_raw_score) OR
              (criterion_key = 'c3' AND p_c3 IS NOT NULL AND p_c3 IS DISTINCT FROM initial_raw_score) OR
              (criterion_key = 'c4' AND p_c4 IS NOT NULL AND p_c4 IS DISTINCT FROM initial_raw_score) OR
              (criterion_key = 'c5' AND p_c5 IS NOT NULL AND p_c5 IS DISTINCT FROM initial_raw_score)
          )
    ) INTO v_any_change;

    SELECT initial_raw_score INTO v_init_conc
    FROM conclusion_scores
    WHERE submission_id = p_submission_id AND judge_id = v_judge_id;

    IF p_conclusion_score IS NOT NULL AND p_conclusion_score IS DISTINCT FROM v_init_conc THEN
        v_any_change := TRUE;
    END IF;

    IF v_any_change AND (p_revision_reason IS NULL OR btrim(p_revision_reason) = '') THEN
        RAISE EXCEPTION 'A written revision_reason is required when modifying any Phase 1 score.';
    END IF;

    -- Apply revisions where provided
    UPDATE essay_scores SET final_raw_score = p_c1 WHERE submission_id = p_submission_id AND judge_id = v_judge_id AND criterion_key = 'c1' AND p_c1 IS NOT NULL;
    UPDATE essay_scores SET final_raw_score = p_c2 WHERE submission_id = p_submission_id AND judge_id = v_judge_id AND criterion_key = 'c2' AND p_c2 IS NOT NULL;
    UPDATE essay_scores SET final_raw_score = p_c3 WHERE submission_id = p_submission_id AND judge_id = v_judge_id AND criterion_key = 'c3' AND p_c3 IS NOT NULL;
    UPDATE essay_scores SET final_raw_score = p_c4 WHERE submission_id = p_submission_id AND judge_id = v_judge_id AND criterion_key = 'c4' AND p_c4 IS NOT NULL;
    UPDATE essay_scores SET final_raw_score = p_c5 WHERE submission_id = p_submission_id AND judge_id = v_judge_id AND criterion_key = 'c5' AND p_c5 IS NOT NULL;

    IF p_conclusion_score IS NOT NULL THEN
        UPDATE conclusion_scores
        SET final_raw_score = p_conclusion_score,
            zero_score_reason = COALESCE(p_zero_score_reason, zero_score_reason)
        WHERE submission_id = p_submission_id AND judge_id = v_judge_id;
    END IF;

    -- Lock session permanently
    UPDATE judge_sessions
    SET status = 'locked_final',
        phase_2_submitted_at = NOW(),
        revision_reason = p_revision_reason
    WHERE id = v_session.id;

    INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
    VALUES ('judge', v_judge_id, 'phase_2_locked_final', 'submission', p_submission_id,
            jsonb_build_object('revised', v_any_change, 'revision_reason', p_revision_reason));

    RETURN 'locked_final';
END;
$$;

GRANT EXECUTE ON FUNCTION submit_phase2_revisions(INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC, NUMERIC, TEXT, TEXT) TO judge_role;

-- ----------------------------------------------------------------------------
-- 3. TWO-JUDGE FLAG CONSENSUS PROTOCOL
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION submit_flag_review(
    p_flag_id INT,
    p_decision TEXT
) RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_flag_status TEXT;
    v_confirm_count INT;
    v_clear_count INT;
    v_new_status TEXT;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    IF p_decision NOT IN ('confirm', 'clear') THEN
        RAISE EXCEPTION 'Invalid decision: %. Must be confirm or clear.', p_decision;
    END IF;

    SELECT status INTO v_flag_status
    FROM flags
    WHERE id = p_flag_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Flag % not found.', p_flag_id;
    END IF;

    IF v_flag_status <> 'open' THEN
        RAISE EXCEPTION 'Flag % is already resolved (%).', p_flag_id, v_flag_status;
    END IF;

    -- Record this judge's review (Primary key prevents double-voting by same judge)
    INSERT INTO flag_reviews (flag_id, judge_id, decision)
    VALUES (p_flag_id, v_judge_id, p_decision);

    -- Count consensus votes
    SELECT
        COUNT(*) FILTER (WHERE decision = 'confirm'),
        COUNT(*) FILTER (WHERE decision = 'clear')
    INTO v_confirm_count, v_clear_count
    FROM flag_reviews
    WHERE flag_id = p_flag_id;

    v_new_status := v_flag_status;

    IF v_confirm_count >= 2 THEN
        v_new_status := 'confirmed';
    ELSIF v_clear_count >= 2 THEN
        v_new_status := 'cleared';
    END IF;

    IF v_new_status <> 'open' THEN
        UPDATE flags
        SET status = v_new_status,
            resolved_at = NOW()
        WHERE id = p_flag_id;
    END IF;

    INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
    VALUES ('judge', v_judge_id, 'flag_review_submitted', 'flag', p_flag_id,
            jsonb_build_object('decision', p_decision, 'resulting_status', v_new_status));

    RETURN v_new_status;
END;
$$;

GRANT EXECUTE ON FUNCTION submit_flag_review(INT, TEXT) TO judge_role, admin_role;


-- ----------------------------------------------------------------------------
-- 4. WEIGHTED CHAMPIONSHIP SCORING ENGINE (STAGE 1 & STAGE 2)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION compute_stage1_results()
RETURNS INT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_cfg settings%ROWTYPE;
    v_cutoff_score NUMERIC;
    v_count INT;
BEGIN
    SELECT * INTO v_cfg FROM settings WHERE is_active = TRUE;
    IF NOT FOUND THEN RAISE EXCEPTION 'No active settings configuration found.'; END IF;
    DELETE FROM stage1_results;
    WITH active_judge_weights AS (
        SELECT id AS judge_id, judge_type,
            CASE WHEN judge_type = 'external' THEN v_cfg.weight_external_judge
                 ELSE v_cfg.weight_internal_judge END AS j_weight
        FROM judges WHERE is_active = TRUE
    ),
    judge_essay_totals AS (
        SELECT es.submission_id, es.judge_id, ajw.j_weight,
            SUM(
                (CASE WHEN ajw.judge_type = 'external'
                      THEN COALESCE(es.final_raw_score, es.initial_raw_score) * (100.0 / 3.0)
                      ELSE COALESCE(es.final_raw_score, es.initial_raw_score) END) *
                COALESCE((v_cfg.criteria_weights ->> es.criterion_key)::NUMERIC, 0)
            ) * (v_cfg.essay_weight_pct / 100.0) AS weighted_essay_70
        FROM essay_scores es
        JOIN judge_sessions js ON js.submission_id = es.submission_id AND js.judge_id = es.judge_id
        JOIN active_judge_weights ajw ON ajw.judge_id = es.judge_id
        WHERE js.status = 'locked_final'
        GROUP BY es.submission_id, es.judge_id, ajw.j_weight, ajw.judge_type
    ),
    judge_conc_totals AS (
        SELECT cs.submission_id, cs.judge_id, ajw.j_weight,
            COALESCE(cs.final_raw_score, cs.initial_raw_score) AS conc_30
        FROM conclusion_scores cs
        JOIN judge_sessions js ON js.submission_id = cs.submission_id AND js.judge_id = cs.judge_id
        JOIN active_judge_weights ajw ON ajw.judge_id = cs.judge_id
        WHERE js.status = 'locked_final'
    ),
    aggregated_scores AS (
        SELECT s.id AS submission_id,
            ROUND(SUM(jet.weighted_essay_70 * jet.j_weight) / NULLIF(SUM(jet.j_weight), 0), 4) AS final_essay_score,
            CASE WHEN cw.outcome = 'auto_zero' OR EXISTS (
                SELECT 1 FROM flags f WHERE f.submission_id = s.id AND f.status = 'confirmed')
                THEN 0
                ELSE ROUND(SUM(jct.conc_30 * jct.j_weight) / NULLIF(SUM(jct.j_weight), 0), 4) END AS final_conc_score
        FROM submissions s
        JOIN candidates c ON c.id = s.candidate_id
        LEFT JOIN conclusion_windows cw ON cw.submission_id = s.id
        JOIN judge_essay_totals jet ON jet.submission_id = s.id
        JOIN judge_conc_totals jct ON jct.submission_id = s.id AND jct.judge_id = jet.judge_id
        WHERE s.status IN ('submitted', 'auto_locked') AND c.is_disqualified = FALSE
        GROUP BY s.id, cw.outcome
        HAVING COUNT(DISTINCT jet.judge_id) >= v_cfg.min_active_judges
    ),
    ranked_submissions AS (
        SELECT submission_id, final_essay_score, final_conc_score,
            (final_essay_score + final_conc_score) AS comp_score,
            RANK() OVER (ORDER BY (final_essay_score + final_conc_score) DESC, final_essay_score DESC) AS rnk
        FROM aggregated_scores
    )
    INSERT INTO stage1_results (submission_id, essay_score, conclusion_score, composite_score, rank, is_in_blur_zone, settings_version)
    SELECT submission_id, final_essay_score, final_conc_score, comp_score, rnk, FALSE, v_cfg.version
    FROM ranked_submissions;
    SELECT composite_score INTO v_cutoff_score FROM stage1_results
    WHERE rank <= v_cfg.top_finalists_target ORDER BY rank DESC LIMIT 1;
    IF v_cutoff_score IS NOT NULL THEN
        UPDATE stage1_results SET is_in_blur_zone = TRUE
        WHERE ABS(composite_score - v_cutoff_score) <= v_cfg.blur_margin;
    END IF;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    INSERT INTO audit_log (actor_role, actor_id, action, target_type, details)
    VALUES ('admin', current_user_id(), 'stage1_results_computed', 'stage1_results',
            jsonb_build_object('settings_version', v_cfg.version, 'cutoff_score', v_cutoff_score));
    RETURN (SELECT COUNT(*) FROM stage1_results);
END;
$$;

GRANT EXECUTE ON FUNCTION compute_stage1_results() TO admin_role;


CREATE OR REPLACE FUNCTION compute_final_results()
RETURNS INT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_cfg settings%ROWTYPE;
BEGIN
    SELECT * INTO v_cfg FROM settings WHERE is_active = TRUE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No active settings configuration found.';
    END IF;

    DELETE FROM final_results;

    WITH active_judge_weights AS (
        SELECT
            id AS judge_id,
            CASE WHEN judge_type = 'external' THEN v_cfg.weight_external_judge
                 ELSE v_cfg.weight_internal_judge
            END AS j_weight
        FROM judges
        WHERE is_active = TRUE
    ),
    judge_stage2_totals AS (
        SELECT
            s2.submission_id,
            s2.judge_id,
            ajw.j_weight,
            SUM(
                s2.raw_score *
                COALESCE((v_cfg.stage2_criteria_weights ->> s2.criterion_key)::NUMERIC, 0)
            ) AS s2_judge_score
        FROM stage2_scores s2
        JOIN active_judge_weights ajw ON ajw.judge_id = s2.judge_id
        GROUP BY s2.submission_id, s2.judge_id, ajw.j_weight
    ),
    aggregated_stage2 AS (
        SELECT
            s1.submission_id,
            s1.composite_score AS stage1_score,
            ROUND(SUM(jst.s2_judge_score * jst.j_weight) / NULLIF(SUM(jst.j_weight), 0), 4) AS stage2_score
        FROM stage1_results s1
        JOIN finalist_checks fc ON fc.submission_id = s1.submission_id AND fc.decision = 'approved'
        JOIN judge_stage2_totals jst ON jst.submission_id = s1.submission_id
        GROUP BY s1.submission_id, s1.composite_score
        HAVING COUNT(DISTINCT jst.judge_id) >= v_cfg.min_active_judges
    ),
    ranked_finals AS (
        SELECT
            submission_id,
            stage1_score,
            stage2_score,
            ROUND((stage1_score * v_cfg.stage1_overall_weight) + (stage2_score * v_cfg.stage2_overall_weight), 4) AS total_score,
            RANK() OVER (
                ORDER BY ((stage1_score * v_cfg.stage1_overall_weight) + (stage2_score * v_cfg.stage2_overall_weight)) DESC,
                         stage2_score DESC
            ) AS f_rank
        FROM aggregated_stage2
    )
    INSERT INTO final_results (
        submission_id, stage1_score, stage2_score, overall_score, final_rank, settings_version
    )
    SELECT
        submission_id,
        stage1_score,
        stage2_score,
        total_score,
        f_rank,
        v_cfg.version
    FROM ranked_finals;

    INSERT INTO audit_log (actor_role, actor_id, action, target_type, details)
    VALUES ('admin', current_user_id(), 'final_results_computed', 'final_results',
            jsonb_build_object('settings_version', v_cfg.version));

    RETURN (SELECT COUNT(*) FROM final_results);
END;
$$;

GRANT EXECUTE ON FUNCTION compute_final_results() TO admin_role;

CREATE OR REPLACE FUNCTION open_full_essay_for_judge(p_submission_id INT)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_canon TEXT;
    v_outcome TEXT;
    v_window_ends_at TIMESTAMPTZ;
    v_now TIMESTAMPTZ := NOW();
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    SELECT canonical_text INTO v_canon
    FROM submissions
    WHERE id = p_submission_id AND status IN ('submitted', 'auto_locked');

    IF v_canon IS NULL THEN
        RAISE EXCEPTION 'Submission text not found.';
    END IF;

    -- Defense gate: essay opens only after the candidate's defense window
    -- has closed, they've submitted, or it timed out.
    SELECT outcome, window_ends_at
    INTO v_outcome, v_window_ends_at
    FROM conclusion_windows
    WHERE submission_id = p_submission_id;

    IF v_outcome = 'preparing' THEN
        RAISE EXCEPTION 'ESSAY_LOCKED|Defense question not yet delivered.'
            USING ERRCODE = '42501';
    END IF;

    IF v_outcome = 'in_progress'
       AND (v_window_ends_at IS NULL OR v_now <= v_window_ends_at) THEN
        RAISE EXCEPTION 'ESSAY_LOCKED|Defense window still open until %.',
            to_char(v_window_ends_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            USING ERRCODE = '42501';
    END IF;

    INSERT INTO judge_sessions (submission_id, judge_id, status, opened_full_essay, opened_full_essay_at)
    VALUES (p_submission_id, v_judge_id, 'phase_1_in_progress', TRUE, v_now)
    ON CONFLICT (submission_id, judge_id) DO UPDATE
    SET opened_full_essay = TRUE,
        opened_full_essay_at = COALESCE(judge_sessions.opened_full_essay_at, v_now);

    INSERT INTO audit_log (actor_role, actor_id, action, target_type, target_id, details)
    VALUES ('judge', v_judge_id, 'judge_opened_full_essay', 'submission', p_submission_id,
            jsonb_build_object('judge_id', v_judge_id));

    RETURN v_canon;
END;
$$;

GRANT EXECUTE ON FUNCTION open_full_essay_for_judge(INT) TO judge_role;

CREATE OR REPLACE FUNCTION raise_judge_flag(p_submission_id INT, p_flag_type TEXT, p_note TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_flag_id INT;
    v_status TEXT;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    INSERT INTO flags (submission_id, flag_type, raised_by, evidence)
    VALUES (p_submission_id, p_flag_type, 'judge', jsonb_build_object('judge_id', v_judge_id, 'note', p_note))
    RETURNING id, status INTO v_flag_id, v_status;

    RETURN jsonb_build_object('flag_id', v_flag_id, 'status', v_status);
END;
$$;

GRANT EXECUTE ON FUNCTION raise_judge_flag(INT, TEXT, TEXT) TO judge_role;

CREATE OR REPLACE FUNCTION submit_stage2_scores(
    p_submission_id INT,
    p_c1 NUMERIC,
    p_c2 NUMERIC,
    p_c3 NUMERIC,
    p_c4 NUMERIC
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM finalists f
        JOIN finalist_checks fc ON fc.submission_id = f.submission_id
        WHERE f.submission_id = p_submission_id AND fc.decision = 'approved'
    ) THEN
        RAISE EXCEPTION 'Submission % is not an approved finalist; cannot record Stage 2 scores.', p_submission_id;
    END IF;

    INSERT INTO stage2_scores (submission_id, judge_id, criterion_key, raw_score)
    VALUES
        (p_submission_id, v_judge_id, 'c1', p_c1),
        (p_submission_id, v_judge_id, 'c2', p_c2),
        (p_submission_id, v_judge_id, 'c3', p_c3),
        (p_submission_id, v_judge_id, 'c4', p_c4)
    ON CONFLICT (submission_id, judge_id, criterion_key) DO UPDATE
    SET raw_score = EXCLUDED.raw_score, submitted_at = NOW();
END;
$$;

GRANT EXECUTE ON FUNCTION submit_stage2_scores(INT, NUMERIC, NUMERIC, NUMERIC, NUMERIC) TO judge_role;


-- ----------------------------------------------------------------------------
-- 5. CANDIDATE VERDICT
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_get_candidate_verdict()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_user_id INT := current_user_id();
    v_is_disq BOOLEAN;
    v_dq_reason TEXT;
    v_submission_id INT;
    v_fc_decision TEXT;
    v_fc_reason TEXT;
BEGIN
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: candidate identity required.';
    END IF;

    SELECT c.is_disqualified, c.disqualification_reason, s.id
    INTO v_is_disq, v_dq_reason, v_submission_id
    FROM candidates c
    JOIN submissions s ON s.candidate_id = c.id
    WHERE c.id = v_user_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Candidate or submission not found.';
    END IF;

    IF v_is_disq THEN
        RETURN jsonb_build_object(
            'status', 'disqualified',
            'reason', COALESCE(v_dq_reason, 'Disqualified by administration.')
        );
    END IF;

    SELECT decision, reason INTO v_fc_decision, v_fc_reason
    FROM finalist_checks WHERE submission_id = v_submission_id;

    IF v_fc_decision IS NULL THEN
        RETURN jsonb_build_object('status', 'not_selected');
    ELSIF v_fc_decision = 'approved' THEN
        RETURN jsonb_build_object('status', 'qualified');
    ELSE
        RETURN jsonb_build_object(
            'status', 'disqualified',
            'reason', COALESCE(v_fc_reason, 'Finalist verification failed.')
        );
    END IF;
END;
$$;

GRANT EXECUTE ON FUNCTION fn_get_candidate_verdict() TO candidate_role, admin_role;

-- ----------------------------------------------------------------------------
-- 6. FINALIST SELECTION (Top-N + blur margin from stage1_results)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_select_finalists()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_cfg settings%ROWTYPE;
    v_N INT;
    v_blur NUMERIC;
    v_sn NUMERIC;
    v_count INT;
    v_result JSONB;
BEGIN
    SELECT * INTO v_cfg FROM settings WHERE is_active = TRUE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'No active settings row';
    END IF;
    v_N := v_cfg.top_finalists_target;
    v_blur := v_cfg.blur_margin;

    SELECT COUNT(*) INTO v_count FROM stage1_results;
    IF v_count = 0 THEN
        RAISE EXCEPTION 'stage1_results is empty; run compute_stage1_results() first.';
    END IF;

    -- Fresh selection each call: wipe prior rank/blur admissions (preserve replacements from promote)
    DELETE FROM finalists WHERE admitted_by IN ('rank', 'blur');

    -- Score at Nth position (submission_id is tie-breaker for the OFFSET)
    SELECT composite_score INTO v_sn
    FROM stage1_results
    ORDER BY rank ASC, submission_id ASC
    LIMIT 1 OFFSET (v_N - 1);

    IF v_sn IS NULL THEN
        -- Fewer than N candidates exist: all within top N by rank are admitted as 'rank'
        INSERT INTO finalists (submission_id, stage1_rank, composite_score, admitted_by, status)
        SELECT submission_id, rank, composite_score, 'rank', 'pending_check'
        FROM stage1_results
        WHERE rank <= v_N;
    ELSE
        INSERT INTO finalists (submission_id, stage1_rank, composite_score, admitted_by, status)
        SELECT submission_id, rank, composite_score,
               CASE WHEN rank <= v_N THEN 'rank' ELSE 'blur' END,
               'pending_check'
        FROM stage1_results
        WHERE rank <= v_N OR composite_score >= v_sn - v_blur;
    END IF;

    SELECT jsonb_agg(jsonb_build_object(
        'submission_id', f.submission_id,
        'stage1_rank', f.stage1_rank,
        'composite_score', f.composite_score,
        'admitted_by', f.admitted_by,
        'status', f.status
    ) ORDER BY f.stage1_rank ASC, f.submission_id ASC)
    INTO v_result
    FROM finalists f;

    RETURN COALESCE(v_result, '[]'::jsonb);
END;
$$;

GRANT EXECUTE ON FUNCTION fn_select_finalists() TO admin_role;

-- ----------------------------------------------------------------------------
-- 7. STAGE 2 FINALIST LIST (per-judge view)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_list_stage2_finalists()
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_result JSONB;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    SELECT jsonb_agg(jsonb_build_object(
        'submission_id', f.submission_id,
        'participant_number', s.participant_number,
        'has_my_score', EXISTS(
            SELECT 1 FROM stage2_scores ss
            WHERE ss.submission_id = f.submission_id
              AND ss.judge_id = v_judge_id
        )
    ) ORDER BY s.participant_number ASC)
    INTO v_result
    FROM finalists f
    JOIN finalist_checks fc ON fc.submission_id = f.submission_id
                           AND fc.decision = 'approved'
    JOIN submissions s ON s.id = f.submission_id;

    RETURN COALESCE(v_result, '[]'::jsonb);
END;
$$;

GRANT EXECUTE ON FUNCTION fn_list_stage2_finalists() TO judge_role;

-- ----------------------------------------------------------------------------
-- 8. STAGE 2 PEER VIEW (per-judge, quorum-gated, requires own score)
-- ----------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_get_stage2_view(p_submission_id INT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_judge_id INT := current_user_id();
    v_quorum INT;
    v_scored_count INT;
    v_my_scored BOOLEAN;
    v_revealed BOOLEAN := FALSE;
    v_peers JSONB;
BEGIN
    IF v_judge_id IS NULL THEN
        RAISE EXCEPTION 'Unauthorized: Judge identity required.';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM judges WHERE id = v_judge_id AND is_active = TRUE) THEN
        RAISE EXCEPTION 'Judge is not active.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM finalists f
        JOIN finalist_checks fc ON fc.submission_id = f.submission_id
        WHERE f.submission_id = p_submission_id AND fc.decision = 'approved'
    ) THEN
        RAISE EXCEPTION 'Submission is not an approved finalist.';
    END IF;

    SELECT min_active_judges INTO v_quorum FROM settings WHERE is_active = TRUE;

    SELECT EXISTS(
        SELECT 1 FROM stage2_scores
        WHERE submission_id = p_submission_id AND judge_id = v_judge_id
    ) INTO v_my_scored;

    SELECT COUNT(DISTINCT judge_id) INTO v_scored_count
    FROM stage2_scores WHERE submission_id = p_submission_id;

    IF v_scored_count >= v_quorum AND v_my_scored THEN
        v_revealed := TRUE;
        SELECT jsonb_agg(jsonb_build_object(
            'mask_avatar', j.mask_avatar,
            'c1', s2.c1,
            'c2', s2.c2,
            'c3', s2.c3,
            'c4', s2.c4
        ) ORDER BY j.mask_avatar)
        INTO v_peers
        FROM (
            SELECT judge_id,
                   MAX(CASE WHEN criterion_key='c1' THEN raw_score END) AS c1,
                   MAX(CASE WHEN criterion_key='c2' THEN raw_score END) AS c2,
                   MAX(CASE WHEN criterion_key='c3' THEN raw_score END) AS c3,
                   MAX(CASE WHEN criterion_key='c4' THEN raw_score END) AS c4
            FROM stage2_scores
            WHERE submission_id = p_submission_id
            GROUP BY judge_id
        ) s2
        JOIN judges j ON j.id = s2.judge_id
        WHERE s2.judge_id <> v_judge_id;
    END IF;

    RETURN jsonb_build_object(
        'submission_id', p_submission_id,
        'revealed', v_revealed,
        'my_score_recorded', v_my_scored,
        'scored_count', v_scored_count,
        'quorum', v_quorum,
        'peers', COALESCE(v_peers, '[]'::jsonb)
    );
END;
$$;

GRANT EXECUTE ON FUNCTION fn_get_stage2_view(INT) TO judge_role;
