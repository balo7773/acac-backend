-- ============================================================================
-- ACAC 02_SECURITY.SQL - ROLES, PERMISSIONS, RLS & IMMUTABILITY TRIGGERS
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. DATABASE ROLES & BASE PRIVILEGE ISOLATION
-- ----------------------------------------------------------------------------

CREATE ROLE candidate_role NOLOGIN;
CREATE ROLE judge_role NOLOGIN;
CREATE ROLE worker_role NOLOGIN;
CREATE ROLE admin_role NOLOGIN;

-- Allow the main database user (acac_admin) to switch into these roles per request
GRANT candidate_role, judge_role, worker_role, admin_role TO CURRENT_USER;

-- Revoke default public permissions so no role gets accidental access
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;

-- Grant schema usage
GRANT USAGE ON SCHEMA public TO candidate_role, judge_role, worker_role, admin_role;

-- Candidate Role Grants (Strict minimum for portal & timed defense)
GRANT SELECT ON settings, competition_state, candidates, submissions, conclusion_windows TO candidate_role;
GRANT UPDATE (pdf_path, page_count, word_count, canonical_text) ON submissions TO candidate_role;
GRANT UPDATE (draft_text, draft_saved_at, outcome, question_text, is_fallback_wording, question_source, question_delivered_at, window_ends_at) ON conclusion_windows TO candidate_role;

-- Judge Role Grants (Notice: ZERO access to 'candidates', 'canonical_text', 'flags', or direct score writes)
GRANT SELECT ON settings, competition_state, judges, conclusion_windows, ai_claims TO judge_role;
GRANT SELECT (id, participant_number, status, page_count, word_count, rules_version, rules_accepted_at, final_submitted_at, locked_at) ON submissions TO judge_role;
GRANT SELECT ON judge_sessions, essay_scores, conclusion_scores, stage2_scores TO judge_role;

-- Worker Role Grants (Background AI pipeline & timer sweeps)
GRANT SELECT ON settings, competition_state, submissions, judge_sessions TO worker_role;
GRANT SELECT, UPDATE ON conclusion_windows TO worker_role;
GRANT SELECT, INSERT, UPDATE ON ai_jobs, ai_evaluations, ai_claims, ai_conclusion_audits, similarity_pairs, flags TO worker_role;
GRANT INSERT ON audit_log TO worker_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO worker_role;

-- Admin Role Grants (Full oversight, audit log append-only)
GRANT SELECT ON ALL TABLES IN SCHEMA public TO admin_role;
GRANT INSERT, UPDATE ON admins, settings, competition_state, candidates, judges, submissions, similarity_pairs, flags, stage1_results, finalist_checks, final_results, ai_jobs TO admin_role;
GRANT INSERT ON audit_log TO admin_role;
GRANT INSERT ON audit_log TO judge_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO admin_role;

-- ----------------------------------------------------------------------------
-- 2. ROW-LEVEL SECURITY (RLS) POLICIES
-- ----------------------------------------------------------------------------

-- Helper function to read the active user's ID injected by FastAPI per transaction
CREATE OR REPLACE FUNCTION current_user_id() RETURNS INT AS $$
    SELECT NULLIF(current_setting('app.current_user_id', true), '')::INT;
$$ LANGUAGE sql STABLE;

GRANT EXECUTE ON FUNCTION current_user_id() TO candidate_role, judge_role, worker_role, admin_role;

-- Enable RLS across all sensitive multi-tenant tables
ALTER TABLE candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE submissions ENABLE ROW LEVEL SECURITY;
ALTER TABLE conclusion_windows ENABLE ROW LEVEL SECURITY;
ALTER TABLE judge_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE essay_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE conclusion_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE stage2_scores ENABLE ROW LEVEL SECURITY;
ALTER TABLE flags ENABLE ROW LEVEL SECURITY;
ALTER TABLE flag_reviews ENABLE ROW LEVEL SECURITY;

-- 2A. CANDIDATE POLICIES (Strict self-isolation)
CREATE POLICY candidate_self_policy ON candidates
    FOR ALL TO candidate_role
    USING (id = current_user_id())
    WITH CHECK (id = current_user_id());

CREATE POLICY candidate_submissions_policy ON submissions
    FOR ALL TO candidate_role
    USING (candidate_id = current_user_id())
    WITH CHECK (candidate_id = current_user_id());

CREATE POLICY candidate_windows_policy ON conclusion_windows
    FOR ALL TO candidate_role
    USING (submission_id IN (SELECT id FROM submissions WHERE candidate_id = current_user_id()))
    WITH CHECK (submission_id IN (SELECT id FROM submissions WHERE candidate_id = current_user_id()));

-- 2B. JUDGE POLICIES (Read essays, isolate personal scores & sessions)
CREATE POLICY judge_read_submissions ON submissions
    FOR SELECT TO judge_role
    USING (status IN ('submitted', 'auto_locked'));

CREATE POLICY judge_read_windows ON conclusion_windows
    FOR SELECT TO judge_role
    USING (true);

CREATE POLICY judge_own_sessions ON judge_sessions
    FOR ALL TO judge_role
    USING (judge_id = current_user_id())
    WITH CHECK (judge_id = current_user_id());

CREATE POLICY judge_own_essay_scores ON essay_scores
    FOR ALL TO judge_role
    USING (judge_id = current_user_id())
    WITH CHECK (judge_id = current_user_id());

CREATE POLICY judge_own_conclusion_scores ON conclusion_scores
    FOR ALL TO judge_role
    USING (judge_id = current_user_id())
    WITH CHECK (judge_id = current_user_id());

CREATE POLICY judge_own_stage2_scores ON stage2_scores
    FOR ALL TO judge_role
    USING (judge_id = current_user_id())
    WITH CHECK (judge_id = current_user_id());

CREATE POLICY judge_flags_select ON flags
    FOR SELECT TO judge_role
    USING (true);

CREATE POLICY judge_flags_insert ON flags
    FOR INSERT TO judge_role
    WITH CHECK (raised_by = 'judge');

CREATE POLICY judge_flag_reviews_select ON flag_reviews
    FOR SELECT TO judge_role
    USING (true);

CREATE POLICY judge_flag_reviews_insert ON flag_reviews
    FOR INSERT TO judge_role
    WITH CHECK (judge_id = current_user_id());

-- 2C. WORKER & ADMIN POLICIES (Unrestricted row visibility on granted tables)
CREATE POLICY worker_submissions_all ON submissions FOR ALL TO worker_role USING (true) WITH CHECK (true);
CREATE POLICY worker_windows_all ON conclusion_windows FOR ALL TO worker_role USING (true) WITH CHECK (true);
CREATE POLICY worker_sessions_all ON judge_sessions FOR ALL TO worker_role USING (true) WITH CHECK (true);
CREATE POLICY worker_flags_all ON flags FOR ALL TO worker_role USING (true) WITH CHECK (true);

CREATE POLICY admin_candidates_all ON candidates FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_submissions_all ON submissions FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_windows_all ON conclusion_windows FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_sessions_all ON judge_sessions FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_essay_scores_all ON essay_scores FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_conc_scores_all ON conclusion_scores FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_stage2_scores_all ON stage2_scores FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_flags_all ON flags FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_flag_reviews_all ON flag_reviews FOR ALL TO admin_role USING (true) WITH CHECK (true);

-- ----------------------------------------------------------------------------
-- 3. IMMUTABILITY & STATE INTEGRITY TRIGGERS
-- ----------------------------------------------------------------------------

-- 3A. Audit Log: Strictly Append-Only (No UPDATE or DELETE ever allowed)
CREATE OR REPLACE FUNCTION prevent_audit_log_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Security Violation: audit_log is strictly append-only. Modifications and deletions are forbidden.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_log_immutable
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION prevent_audit_log_modification();

-- 3B. Submissions: Freeze essay content once status leaves 'draft'
CREATE OR REPLACE FUNCTION enforce_submission_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN ('submitted', 'auto_locked') THEN
        IF NEW.canonical_text IS DISTINCT FROM OLD.canonical_text
           OR NEW.pdf_path IS DISTINCT FROM OLD.pdf_path
           OR NEW.word_count IS DISTINCT FROM OLD.word_count
           OR NEW.page_count IS DISTINCT FROM OLD.page_count
           OR NEW.candidate_id IS DISTINCT FROM OLD.candidate_id
           OR NEW.participant_number IS DISTINCT FROM OLD.participant_number
        THEN
            RAISE EXCEPTION 'Integrity Violation: Cannot alter essay content or metadata after submission is locked.';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_submission_immutable
    BEFORE UPDATE ON submissions
    FOR EACH ROW EXECUTE FUNCTION enforce_submission_immutability();

-- 3C. Conclusion Windows: Freeze question once delivered, freeze row once closed
CREATE OR REPLACE FUNCTION enforce_window_immutability()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.outcome IN ('answered', 'auto_zero') THEN
        RAISE EXCEPTION 'Integrity Violation: Conclusion window is already closed (%) and cannot be modified.', OLD.outcome;
    END IF;

    IF OLD.question_delivered_at IS NOT NULL AND (
        NEW.question_text IS DISTINCT FROM OLD.question_text
        OR NEW.window_ends_at IS DISTINCT FROM OLD.window_ends_at
        OR NEW.question_delivered_at IS DISTINCT FROM OLD.question_delivered_at
    ) THEN
        RAISE EXCEPTION 'Integrity Violation: Delivered question and window deadline cannot be altered.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_window_immutable
    BEFORE UPDATE ON conclusion_windows
    FOR EACH ROW EXECUTE FUNCTION enforce_window_immutability();

-- 3D. Judge Scores: Never alter initial_raw_score; freeze all edits once session is locked_final
CREATE OR REPLACE FUNCTION enforce_score_immutability()
RETURNS TRIGGER AS $$
DECLARE
    v_session_status TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Integrity Violation: Judge scores cannot be deleted.';
    END IF;

    IF NEW.initial_raw_score IS DISTINCT FROM OLD.initial_raw_score THEN
        RAISE EXCEPTION 'Integrity Violation: initial_raw_score is permanently locked after Phase 1.';
    END IF;

    SELECT status INTO v_session_status
    FROM judge_sessions
    WHERE submission_id = OLD.submission_id AND judge_id = OLD.judge_id;

    IF v_session_status = 'locked_final' THEN
        RAISE EXCEPTION 'Integrity Violation: Judge session is locked_final. Scores can no longer be revised.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_essay_scores_immutable
    BEFORE UPDATE OR DELETE ON essay_scores
    FOR EACH ROW EXECUTE FUNCTION enforce_score_immutability();

CREATE TRIGGER trg_conclusion_scores_immutable
    BEFORE UPDATE OR DELETE ON conclusion_scores
    FOR EACH ROW EXECUTE FUNCTION enforce_score_immutability();

-- Results policies
CREATE POLICY admin_stage1_results_all ON stage1_results FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY admin_final_results_all ON final_results FOR ALL TO admin_role USING (true) WITH CHECK (true);
CREATE POLICY public_read_stage1_published ON stage1_results FOR SELECT TO candidate_role, judge_role
    USING (EXISTS (SELECT 1 FROM competition_state WHERE current_phase IN ('results_published', 'stage_2_live', 'completed')));
CREATE POLICY public_read_final_published ON final_results FOR SELECT TO candidate_role, judge_role
    USING (EXISTS (SELECT 1 FROM competition_state WHERE current_phase = 'completed'));

-- Login attempts RLS and grants
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE, DELETE ON login_attempts TO admin_role;
GRANT USAGE, SELECT ON SEQUENCE login_attempts_id_seq TO admin_role;
CREATE POLICY admin_login_attempts_all ON login_attempts FOR ALL TO admin_role USING (true) WITH CHECK (true);

-- Allow judge_role to read own judge record
GRANT SELECT ON judges TO judge_role;
CREATE POLICY judge_self_policy ON judges FOR SELECT TO judge_role USING (id = current_user_id());
