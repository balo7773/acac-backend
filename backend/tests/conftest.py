import pytest
from sqlalchemy import text
from app.core.database import engine, get_rls_session

@pytest.fixture(autouse=True)
async def dispose_engine_pool():
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def reset_competition_phase():
    """2.9b: phase transitions are forward-only. Every test body starts from
    'pre_launch' so endpoint-driven transitions validate deterministically.
    Tests that need a specific starting phase set it themselves.
    Also truncates login_attempts to prevent rate-limit residue leaking
    between tests (per-IP window is 15 minutes)."""
    async with get_rls_session("admin_role") as session:
        await session.execute(
            text(
                "UPDATE competition_state SET current_phase = 'pre_launch', "
                "stage_1_submission_deadline = NULL, "
                "stage_1_conclusion_closing_time = NULL WHERE id = 1"
            )
        )
        await session.execute(text("DELETE FROM login_attempts"))
        await session.execute(text("DELETE FROM ai_jobs"))
        # Reset settings to baseline version 1 (seed)
        await session.execute(text("UPDATE settings SET is_active = FALSE WHERE is_active = TRUE"))
        await session.execute(text("UPDATE settings SET is_active = TRUE WHERE version = 1"))
    yield
