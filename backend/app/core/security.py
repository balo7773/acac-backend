from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator, Optional, Tuple
import jwt
from fastapi import Cookie, Depends, Header, HTTPException, status
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_rls_session

password_hash = PasswordHash((Argon2Hasher(),))

ALGORITHM = "HS256"
COOKIE_NAME = "acac_session"

ROLE_TO_DB_ROLE = {
    "candidate": "candidate_role",
    "judge": "judge_role",
    "admin": "admin_role",
}


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return password_hash.hash(password)


def create_access_token(user_id: int, role: str, token_version: int = 0, expires_delta: Optional[timedelta] = None) -> str:
    if role not in ROLE_TO_DB_ROLE:
        raise ValueError(f"Unsupported user role: {role}")

    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": str(user_id),
        "role": role,
        "db_role": ROLE_TO_DB_ROLE[role],
        "tv": token_version,
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub", 0))
        role = payload.get("role")
        db_role = payload.get("db_role")
        if not user_id or role not in ROLE_TO_DB_ROLE or db_role != ROLE_TO_DB_ROLE[role]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication claims.",
            )
        return {"user_id": user_id, "role": role, "db_role": db_role, "tv": payload.get("tv", -1)}
    except (jwt.PyJWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token.",
        )


async def get_current_claims(
    acac_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> dict:
    token = acac_session
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    
    claims = decode_access_token(token)
    
    # Verify token_version (single device login)
    user_id = claims["user_id"]
    role = claims["role"]
    table = {"candidate": "candidates", "judge": "judges", "admin": "admins"}.get(role)
    if not table:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid role.")
        
    async with get_rls_session("admin_role") as session:
        res = await session.execute(
            text(f"SELECT token_version FROM {table} WHERE id = :id"),
            {"id": user_id}
        )
        row = res.first()
        if not row or row.token_version != claims.get("tv", -1):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired due to a login on another device."
            )
    
    return claims


async def get_candidate_context(
    claims: dict = Depends(get_current_claims),
) -> AsyncGenerator[Tuple[int, AsyncSession], None]:
    if claims["role"] != "candidate":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Candidate access required.")
    async with get_rls_session("candidate_role", claims["user_id"]) as session:
        res = await session.execute(
            text("SELECT is_disqualified FROM candidates WHERE id = current_user_id()")
        )
        row = res.first()
        if not row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Candidate account not found.")
        if row.is_disqualified:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Candidate account is disqualified.")
        yield claims["user_id"], session


async def get_judge_context(
    claims: dict = Depends(get_current_claims),
) -> AsyncGenerator[Tuple[int, AsyncSession], None]:
    if claims["role"] != "judge":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Judge access required.")
    async with get_rls_session("judge_role", claims["user_id"]) as session:
        res = await session.execute(
            text("SELECT is_active FROM judges WHERE id = current_user_id()")
        )
        row = res.first()
        if not row:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Judge account not found.")
        if not row.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Judge account is currently inactive.")
        yield claims["user_id"], session


async def get_admin_context(
    claims: dict = Depends(get_current_claims),
) -> AsyncGenerator[Tuple[int, AsyncSession], None]:
    if claims["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    async with get_rls_session("admin_role", claims["user_id"]) as session:
        yield claims["user_id"], session
