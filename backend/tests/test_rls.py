import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from app.core.database import get_rls_session
from app.core.security import create_access_token, decode_access_token, get_password_hash, verify_password


def test_password_hashing_and_jwt_roundtrip():
    raw_pw = "Acac_S3cure_P@ss!"
    hashed = get_password_hash(raw_pw)
    assert verify_password(raw_pw, hashed) is True
    assert verify_password("wrong_password", hashed) is False

    token = create_access_token(user_id=42, role="judge")
    claims = decode_access_token(token)
    assert claims["user_id"] == 42
    assert claims["role"] == "judge"
    assert claims["db_role"] == "judge_role"


@pytest.mark.asyncio
async def test_rls_session_enforces_postgres_role_and_blocks_illegal_access():
    # 1. Verify candidate_role sets current_user and current_user_id() in PostgreSQL
    async with get_rls_session("candidate_role", user_id=77) as session:
        res = await session.execute(text("SELECT current_user, current_user_id()"))
        row = res.one()
        assert row[0] == "candidate_role"
        assert row[1] == 77

    # 2. Verify judge_role is physically blocked from reading 'candidates' table
    with pytest.raises(DBAPIError) as exc_info:
        async with get_rls_session("judge_role", user_id=5) as session:
            await session.execute(text("SELECT * FROM candidates"))
    assert "permission denied for table candidates" in str(exc_info.value)

    # 3. Verify admin_role is blocked from deleting audit_log (via privilege or trigger)
    with pytest.raises(DBAPIError) as exc_info:
        async with get_rls_session("admin_role", user_id=1) as session:
            await session.execute(text("DELETE FROM audit_log WHERE id = 1"))
    err_msg = str(exc_info.value)
    assert "permission denied for table audit_log" in err_msg or "audit_log is strictly append-only" in err_msg
