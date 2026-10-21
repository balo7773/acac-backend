-- ============================================================================
-- ACAC 01_SCHEMA.SQL - CORE TABLES AND CONSTRAINTS
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. SETTINGS & COMPETITION STATE
-- ----------------------------------------------------------------------------

CREATE TABLE settings (
    id SERIAL PRIMARY KEY,
    version INT UNIQUE NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    weight_internal_judge NUMERIC NOT NULL DEFAULT 1.0,
    weight_external_judge NUMERIC NOT NULL DEFAULT 2.0,
    min_active_judges INT NOT NULL DEFAULT 3,
    essay_weight_pct NUMERIC NOT NULL DEFAULT 70,
    conclusion_weight_pct NUMERIC NOT NULL DEFAULT 30,
    conclusion_floor_score NUMERIC NOT NULL DEFAULT 5,
    criteria_weights JSONB NOT NULL DEFAULT jsonb_build_object('c1',0.15,'c2',0.25,'c3',0.25,'c4',0.25,'c5',0.10),
    stage2_criteria_weights JSONB NOT NULL DEFAULT jsonb_build_object('c1',0.30,'c2',0.35,'c3',0.20,'c4',0.15),
    stage1_overall_weight NUMERIC NOT NULL DEFAULT 0.40,
    stage2_overall_weight NUMERIC NOT NULL DEFAULT 0.60,
    top_finalists_target INT NOT NULL DEFAULT 5,
    blur_margin NUMERIC NOT NULL DEFAULT 0.5,
    question_word_limit INT NOT NULL DEFAULT 60,
    answer_word_min INT NOT NULL DEFAULT 150,
    answer_word_limit INT NOT NULL DEFAULT 500,
    window_length_seconds INT NOT NULL DEFAULT 600,
    grace_seconds INT NOT NULL DEFAULT 10,
    phase2_window_seconds INT NOT NULL DEFAULT 300,
    template_count INT NOT NULL DEFAULT 12,
    run_gap NUMERIC NOT NULL DEFAULT 0.2,
    model_gap NUMERIC NOT NULL DEFAULT 0.2,
    similarity_threshold NUMERIC NOT NULL DEFAULT 0.15,
    rubric_version TEXT NOT NULL DEFAULT 'rubric_bands_v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Guarantees at most ONE settings row can have is_active = true
CREATE UNIQUE INDEX one_active_settings ON settings (is_active) WHERE is_active;

CREATE TABLE competition_state (
    id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    current_phase TEXT NOT NULL DEFAULT 'pre_launch' CHECK (
        current_phase IN (
            'pre_launch',
            'stage_1_submitting',
            'stage_1_grading',
            'stage_1_review',
            'finalist_check',
            'results_published',
            'stage_2_live',
            'completed'
        )
    ),
    stage_1_submission_deadline TIMESTAMPTZ,
    stage_1_conclusion_closing_time TIMESTAMPTZ,
    stage_2_started_at TIMESTAMPTZ,
    results_published_at TIMESTAMPTZ
);

-- ----------------------------------------------------------------------------
-- 2. PEOPLE & IDENTITIES
-- ----------------------------------------------------------------------------

CREATE SEQUENCE participant_seq;

CREATE TABLE candidates (
    id SERIAL PRIMARY KEY,
    participant_number INT UNIQUE NOT NULL DEFAULT nextval('participant_seq'),
    full_name TEXT NOT NULL,
    matric_number TEXT UNIQUE NOT NULL,
    whatsapp_number TEXT UNIQUE NOT NULL,
    linkedin_url TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_disqualified BOOLEAN NOT NULL DEFAULT FALSE,
    disqualification_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE judges (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    judge_type TEXT NOT NULL CHECK (judge_type IN ('internal', 'external')),
    mask_avatar TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMPTZ,
    deactivation_reason TEXT
);

CREATE TABLE admins (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

-- ----------------------------------------------------------------------------
-- 3. SUBMISSIONS & CONCLUSION DEFENSE WINDOWS
-- ----------------------------------------------------------------------------

CREATE SEQUENCE job_a_template_seq;

CREATE TABLE submissions (
    id SERIAL PRIMARY KEY,
    candidate_id INT UNIQUE NOT NULL REFERENCES candidates(id),
    participant_number INT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'auto_locked')),
    pdf_path TEXT,
    page_count INT,
    word_count INT,
    canonical_text TEXT,
    rules_version TEXT,
    rules_accepted_at TIMESTAMPTZ,
    final_submitted_at TIMESTAMPTZ,
    locked_at TIMESTAMPTZ
);

CREATE TABLE conclusion_windows (
    id SERIAL PRIMARY KEY,
    submission_id INT UNIQUE NOT NULL REFERENCES submissions(id),
    outcome TEXT NOT NULL DEFAULT 'preparing' CHECK (
        outcome IN ('preparing', 'in_progress', 'answered', 'auto_zero')
    ),
    template_id INT,
    is_fallback_wording BOOLEAN NOT NULL DEFAULT FALSE,
    question_source TEXT CHECK (question_source IN ('deepseek', 'backup')),
    question_text TEXT,
    question_delivered_at TIMESTAMPTZ,
    window_ends_at TIMESTAMPTZ,
    draft_text TEXT,
    draft_saved_at TIMESTAMPTZ,
    answer_text TEXT,
    answer_submitted_at TIMESTAMPTZ,
    CHECK (
        outcome NOT IN ('in_progress', 'answered')
        OR (question_text IS NOT NULL AND window_ends_at IS NOT NULL)
    )
);

-- ----------------------------------------------------------------------------
-- 4. AI WORK QUEUE & EVALUATION RESULTS
-- ----------------------------------------------------------------------------

CREATE TABLE ai_jobs (
    id SERIAL PRIMARY KEY,
    job_type TEXT NOT NULL CHECK (
        job_type IN ('job_a', 'job_b_run', 'job_b_combine', 'job_c_run', 'job_c_combine', 'job_d')
    ),
    submission_id INT REFERENCES submissions(id),
    model_name TEXT NOT NULL DEFAULT 'none',
    run_id INT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued', 'running', 'done', 'failed')
    ),
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    raw_response JSONB,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_ai_job UNIQUE NULLS NOT DISTINCT (submission_id, job_type, model_name, run_id)
);

CREATE TABLE ai_evaluations (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    model_name TEXT NOT NULL CHECK (model_name IN ('deepseek', 'gemma')),
    criterion_key TEXT NOT NULL CHECK (criterion_key IN ('c1', 'c2', 'c3', 'c4', 'c5')),
    description_text TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    mark_run1 NUMERIC,
    mark_run2 NUMERIC,
    mark NUMERIC NOT NULL CHECK (mark BETWEEN 0 AND 1),
    quote_text TEXT,
    quote_start INT,
    quote_end INT,
    evidence_status TEXT NOT NULL CHECK (evidence_status IN ('verified', 'unverified', 'none')),
    markers TEXT[] NOT NULL DEFAULT '{}',
    prompt_version TEXT NOT NULL,
    model_version TEXT NOT NULL,
    UNIQUE (submission_id, model_name, criterion_key)
);

CREATE TABLE ai_claims (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    model_name TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (claim_type IN ('statistic', 'agency', 'date', 'policy_outcome')),
    claim_text TEXT NOT NULL,
    quote_text TEXT NOT NULL,
    quote_start INT,
    quote_end INT
);

CREATE TABLE ai_conclusion_audits (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    model_name TEXT NOT NULL,
    label TEXT NOT NULL CHECK (label IN ('consistent', 'partial', 'contradictory')),
    essay_quote TEXT,
    conclusion_quote TEXT,
    quotes_verified BOOLEAN NOT NULL DEFAULT FALSE,
    addresses_question BOOLEAN NOT NULL,
    reference_mark NUMERIC CHECK (reference_mark BETWEEN 5 AND 30),
    UNIQUE (submission_id, model_name)
);

CREATE TABLE similarity_pairs (
    id SERIAL PRIMARY KEY,
    submission_a_id INT NOT NULL REFERENCES submissions(id),
    submission_b_id INT NOT NULL REFERENCES submissions(id),
    similarity_score NUMERIC NOT NULL,
    shared_passages JSONB NOT NULL,
    admin_decision TEXT NOT NULL DEFAULT 'pending' CHECK (
        admin_decision IN ('pending', 'confirmed_violation', 'cleared')
    ),
    CHECK (submission_a_id < submission_b_id),
    UNIQUE (submission_a_id, submission_b_id)
);

-- ----------------------------------------------------------------------------
-- 5. JUDGING SESSIONS, SCORES & INTEGRITY FLAGS
-- ----------------------------------------------------------------------------

CREATE TABLE judge_sessions (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    judge_id INT NOT NULL REFERENCES judges(id),
    status TEXT NOT NULL DEFAULT 'phase_1_in_progress' CHECK (
        status IN ('phase_1_in_progress', 'phase_2_review', 'locked_final')
    ),
    opened_full_essay BOOLEAN NOT NULL DEFAULT FALSE,
    opened_full_essay_at TIMESTAMPTZ,
    phase_1_locked_at TIMESTAMPTZ,
    phase_2_expires_at TIMESTAMPTZ,
    phase_2_submitted_at TIMESTAMPTZ,
    revision_reason TEXT,
    UNIQUE (submission_id, judge_id)
);

CREATE TABLE essay_scores (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    judge_id INT NOT NULL REFERENCES judges(id),
    criterion_key TEXT NOT NULL CHECK (criterion_key IN ('c1', 'c2', 'c3', 'c4', 'c5')),
    initial_raw_score NUMERIC NOT NULL CHECK (initial_raw_score >= 0),
    final_raw_score NUMERIC CHECK (final_raw_score >= 0),
    UNIQUE (submission_id, judge_id, criterion_key)
);

CREATE TABLE conclusion_scores (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    judge_id INT NOT NULL REFERENCES judges(id),
    initial_raw_score NUMERIC NOT NULL,
    final_raw_score NUMERIC,
    zero_score_reason TEXT,
    UNIQUE (submission_id, judge_id),
    CHECK (initial_raw_score = 0 OR initial_raw_score BETWEEN 5 AND 30),
    CHECK (final_raw_score IS NULL OR final_raw_score = 0 OR final_raw_score BETWEEN 5 AND 30),
    CHECK (
        (initial_raw_score <> 0 AND COALESCE(final_raw_score, 5) <> 0)
        OR zero_score_reason IS NOT NULL
    )
);

CREATE TABLE flags (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    flag_type TEXT NOT NULL CHECK (flag_type IN ('contradicts_essay', 'off_topic')),
    raised_by TEXT NOT NULL CHECK (raised_by IN ('ai', 'judge')),
    evidence JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'cleared')),
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE flag_reviews (
    flag_id INT NOT NULL REFERENCES flags(id),
    judge_id INT NOT NULL REFERENCES judges(id),
    decision TEXT NOT NULL CHECK (decision IN ('confirm', 'clear')),
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (flag_id, judge_id)
);

-- ----------------------------------------------------------------------------
-- 6. STAGE 1 RESULTS, FINALIST CHECKS, STAGE 2 & AUDIT LOG
-- ----------------------------------------------------------------------------

CREATE TABLE stage1_results (
    submission_id INT PRIMARY KEY REFERENCES submissions(id),
    essay_score NUMERIC NOT NULL,
    conclusion_score NUMERIC NOT NULL,
    composite_score NUMERIC NOT NULL,
    rank INT NOT NULL,
    is_in_blur_zone BOOLEAN NOT NULL DEFAULT FALSE,
    settings_version INT NOT NULL REFERENCES settings(version),
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE finalist_checks (
    submission_id INT PRIMARY KEY REFERENCES submissions(id),
    admin_id INT NOT NULL REFERENCES admins(id),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reason TEXT,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE stage2_scores (
    id SERIAL PRIMARY KEY,
    submission_id INT NOT NULL REFERENCES submissions(id),
    judge_id INT NOT NULL REFERENCES judges(id),
    criterion_key TEXT NOT NULL CHECK (criterion_key IN ('c1', 'c2', 'c3', 'c4')),
    raw_score NUMERIC NOT NULL CHECK (raw_score >= 0),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (submission_id, judge_id, criterion_key)
);

CREATE TABLE final_results (
    submission_id INT PRIMARY KEY REFERENCES submissions(id),
    stage1_score NUMERIC NOT NULL,
    stage2_score NUMERIC NOT NULL,
    overall_score NUMERIC NOT NULL,
    final_rank INT NOT NULL,
    settings_version INT NOT NULL REFERENCES settings(version),
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    actor_role TEXT NOT NULL,
    actor_id INT,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INT,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Login rate limiting & lockout tracking
CREATE TABLE IF NOT EXISTS login_attempts (
    id SERIAL PRIMARY KEY,
    identifier TEXT NOT NULL,
    ip_address TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ident_time ON login_attempts (identifier, attempted_at);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts (ip_address, attempted_at);

-- ----------------------------------------------------------------------------
-- 7. FINALISTS (Top-N + blur margin selections from stage1_results)
-- ----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finalists (
    submission_id INT PRIMARY KEY REFERENCES submissions(id),
    stage1_rank INT NOT NULL,
    composite_score NUMERIC NOT NULL,
    admitted_by TEXT NOT NULL CHECK (admitted_by IN ('rank', 'blur', 'replacement')),
    status TEXT NOT NULL DEFAULT 'pending_check' CHECK (status IN ('pending_check', 'eligible', 'disqualified')),
    check_notes TEXT,
    checked_at TIMESTAMPTZ,
    admitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

GRANT SELECT ON finalists TO admin_role;
