from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.core.config import settings

VALID_DB_ROLES = {"candidate_role", "judge_role", "worker_role", "admin_role"}

engine = create_async_engine(
    settings.async_database_url,
    pool_size=10,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_rls_session(
    db_role: str, user_id: Optional[int] = None
) -> AsyncGenerator[AsyncSession, None]:
    """
    Opens a transaction-scoped session locked to a specific PostgreSQL role
    and user_id for Row-Level Security (RLS) enforcement.
    """
    if db_role not in VALID_DB_ROLES:
        raise ValueError(f"Invalid database role requested: {db_role}")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            uid_str = str(user_id) if user_id is not None else ""
            await session.execute(
                text(
                    "SELECT set_config('role', :role, true), "
                    "set_config('app.current_user_id', :uid, true)"
                ),
                {"role": db_role, "uid": uid_str},
            )
            yield session
