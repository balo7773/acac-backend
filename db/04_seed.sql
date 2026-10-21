-- ============================================================================
-- ACAC 04_SEED.SQL: Initial Bootstrapping State
-- ============================================================================

-- 1. Initial Competition State
INSERT INTO competition_state (id, current_phase)
VALUES (1, 'pre_launch')
ON CONFLICT (id) DO NOTHING;

-- 2. Default Active Settings Configuration
INSERT INTO settings (
    id,
    version,
    is_active,
    weight_internal_judge,
    weight_external_judge,
    min_active_judges,
    essay_weight_pct,
    conclusion_weight_pct,
    conclusion_floor_score,
    criteria_weights,
    stage2_criteria_weights,
    stage1_overall_weight,
    stage2_overall_weight,
    top_finalists_target,
    blur_margin,
    question_word_limit,
    answer_word_min,
    answer_word_limit,
    window_length_seconds,
    grace_seconds,
    phase2_window_seconds,
    template_count,
    run_gap,
    model_gap,
    similarity_threshold,
    rubric_version
) VALUES (
    1,
    1,
    TRUE,
    1.0,
    2.0,
    3,
    70,
    30,
    5,
    '{"c1": 0.15, "c2": 0.25, "c3": 0.25, "c4": 0.25, "c5": 0.10}',
    '{"c1": 0.30, "c2": 0.35, "c3": 0.20, "c4": 0.15}',
    0.40,
    0.60,
    5,
    0.5,
    60,
    150,
    500,
    600,
    10,
    300,
    12,
    0.2,
    0.2,
    0.15,
    'rubric_bands_v1'
)
ON CONFLICT (id) DO NOTHING;

-- 3. Default Root Admin (username: admin, password: Admin_Pass_2026!)
INSERT INTO admins (username, password_hash)
VALUES (
    'admin',
    '$argon2id$v=19$m=65536,t=3,p=4$06Hc2eM+ySMDwzyRxyIVrg$o/wTjIIN+KQx6/zi8Z6u6rnXVHqOln/3nVkrmSY20V0'
)
ON CONFLICT (username) DO NOTHING;
