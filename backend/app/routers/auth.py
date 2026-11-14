from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import get_rls_session
from app.core.security import (
    COOKIE_NAME,
    create_access_token,
    get_current_claims,
    get_password_hash,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class CandidateRegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=120)
    matric_number: str = Field(..., min_length=3, max_length=50)
    whatsapp_number: str = Field(..., min_length=7, max_length=30)
    linkedin_url: str = Field(..., min_length=5, max_length=250)
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    identifier: str = Field(..., description="Participant number for candidates, username for judges/admins")
    password: str
    role: Literal["candidate", "judge", "admin"] = "candidate"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
    )


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_candidate(payload: CandidateRegisterRequest, response: Response):
    hashed_pw = get_password_hash(payload.password)
    matric_clean = payload.matric_number.strip().upper()
    whatsapp_clean = payload.whatsapp_number.strip()
    linkedin_clean = payload.linkedin_url.strip()

    try:
        async with get_rls_session("admin_role") as session:
            phase_res = await session.execute(
                text("SELECT current_phase FROM competition_state WHERE id = 1")
            )
            current_phase = phase_res.scalar_one()
            if current_phase not in ("pre_launch", "stage_1_submitting"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Candidate registration is closed for the current phase.",
                )

            cand_res = await session.execute(
                text(
                    """
                    INSERT INTO candidates (full_name, matric_number, whatsapp_number, linkedin_url, password_hash)
                    VALUES (:full_name, :matric, :whatsapp, :linkedin, :pw)
                    RETURNING id, participant_number
                    """
                ),
                {
                    "full_name": payload.full_name.strip(),
                    "matric": matric_clean,
                    "whatsapp": whatsapp_clean,
                    "linkedin": linkedin_clean,
                    "pw": hashed_pw,
                },
            )
            cand_row = cand_res.one()

            sub_res = await session.execute(
                text(
                    """
                    INSERT INTO submissions (candidate_id, participant_number, status)
                    VALUES (:cid, :pno, 'draft')
                    RETURNING id
                    """
                ),
                {"cid": cand_row.id, "pno": cand_row.participant_number},
            )
            submission_id = sub_res.scalar_one()

    except IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A candidate with this matriculation number or WhatsApp number is already registered.",
        )

    token = create_access_token(user_id=cand_row.id, role="candidate")
    _set_session_cookie(response, token)

    return {
        "user_id": cand_row.id,
        "role": "candidate",
        "submission_id": submission_id,
        "participant_number": cand_row.participant_number,
    }


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response):
    ident = payload.identifier.strip()
    ident_key = f"{payload.role}:{ident.lower()}"
    client_ip = _get_client_ip(request)

    if payload.role == "candidate" and not ident.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Candidates must sign in using their numeric participant_number.",
        )

    auth_error: HTTPException | None = None
    user_id: int | None = None
    display_name: str | None = None

    async with get_rls_session("admin_role") as session:
        # 1. Check lockout window per identifier and per IP
        lock_res = await session.execute(
            text(
                """
                SELECT
                    COUNT(*) FILTER (WHERE identifier = :ident_key) AS ident_Count,
                    COUNT(*) FILTER (WHERE ip_address = :ip) AS ip_count
                FROM login_attempts
                WHERE attempted_at >= NOW() - make_interval(secs => :window_secs)
                  AND (identifier = :ident_key OR ip_address = :ip)
                """
            ),
            {
                "ident_key": ident_key,
                "ip": client_ip,
                "window_secs": settings.LOGIN_LOCKOUT_SECONDS,
            },
        )
        counts = lock_res.one()
        if counts.ident_count >= settings.LOGIN_MAX_ATTEMPTS or counts.ip_count >= settings.LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed login attempts. Account or IP is temporarily locked.",
            )

        # 2. Verify credentials by role
        if payload.role == "candidate":
            res = await session.execute(
                text(
                    """
                    SELECT id, participant_number, full_name, password_hash, is_disqualified, must_change_password FROM candidates
                    WHERE participant_number = :pno
                    """
                ),
                {"pno": int(ident)},
            )
            row = res.first()
            if not row or not verify_password(payload.password, row.password_hash):
                await session.execute(
                    text("INSERT INTO login_attempts (identifier, ip_address) VALUES (:ident_key, :ip)"),
                    {"ident_key": ident_key, "ip": client_ip},
                )
                auth_error = HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid participant number or password.",
                )
            elif row.is_disqualified:
                auth_error = HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Candidate account is disqualified.",
                )
            else:
                display_name = f"Candidate #{row.participant_number}"
                user_id = row.id
                is_temp = bool(row.must_change_password)

        elif payload.role == "judge":
            res = await session.execute(
                text(
                    "SELECT id, username, mask_avatar, judge_type, password_hash, is_active "
                    "FROM judges WHERE lower(username) = :uname"
                ),
                {"uname": ident.lower()},
            )
            row = res.first()
            if not row or not verify_password(payload.password, row.password_hash):
                await session.execute(
                    text("INSERT INTO login_attempts (identifier, ip_address) VALUES (:ident_key, :ip)"),
                    {"ident_key": ident_key, "ip": client_ip},
                )
                auth_error = HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid username or password.",
                )
            elif not row.is_active:
                auth_error = HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Judge account is currently inactive.",
                )
            else:
                mask_map = {
                    "mask_owl": "Judge 1",
                    "mask_falcon": "Judge 2",
                    "mask_lion": "Judge 3",
                    "mask_panther": "Judge 4"
                }
                display_name = mask_map.get(row.mask_avatar, row.username)
                user_id = row.id
                is_temp = False
                judge_type = row.judge_type
                mask_avatar = row.mask_avatar  # Judges NEVER get temp passwords

        else:  # admin
            res = await session.execute(
                text("SELECT id, username, password_hash FROM admins WHERE lower(username) = :uname"),
                {"uname": ident.lower()},
            )
            row = res.first()
            if not row or not verify_password(payload.password, row.password_hash):
                await session.execute(
                    text("INSERT INTO login_attempts (identifier, ip_address) VALUES (:ident_key, :ip)"),
                    {"ident_key": ident_key, "ip": client_ip},
                )
                auth_error = HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid username or password.",
                )
            else:
                display_name = row.username
                user_id = row.id
                is_temp = False

        # 3. Clear failed attempts for this identifier on successful login
        if auth_error is None:
            await session.execute(
                text("DELETE FROM login_attempts WHERE identifier = :ident_key"),
                {"ident_key": ident_key},
            )

    if auth_error is not None:
        raise auth_error

    token = create_access_token(user_id=user_id, role=payload.role)
    _set_session_cookie(response, token)

    return {
        "user_id": user_id,
        "role": payload.role,
        "display_name": display_name,
        "must_change_password": is_temp,
        "judge_type": locals().get("judge_type", None),
        "mask_avatar": locals().get("mask_avatar", None),
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"status": "logged_out"}


@router.get("/me")
async def get_me(claims: dict = Depends(get_current_claims)):
    out = dict(claims)
    if claims.get("role") == "judge":
        async with get_rls_session("judge_role", claims["user_id"]) as session:
            res = await session.execute(
                text("SELECT judge_type, mask_avatar FROM judges WHERE id = current_user_id()")
            )
            row = res.mappings().first()
            if row:
                out["judge_type"] = row["judge_type"]
                out["mask_avatar"] = row["mask_avatar"]
    elif claims.get("role") == "candidate":
        async with get_rls_session("candidate_role", claims["user_id"]) as session:
            res = await session.execute(
                text("SELECT must_change_password FROM candidates WHERE id = current_user_id()")
            )
            row = res.first()
            if row:
                out["must_change_password"] = row.must_change_password
    return out

@router.get("/public-phase")
async def get_public_phase():
    async with get_rls_session("admin_role") as session:
        res = await session.execute(
            text("SELECT current_phase FROM competition_state WHERE id = 1")
        )
        phase = res.scalar_one_or_none()
        return {"current_phase": phase}

from pydantic import BaseModel, Field

class ChangePasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8)

@router.post("/change-password")
async def change_password(payload: ChangePasswordRequest, claims: dict = Depends(get_current_claims)):
    from app.core.security import get_password_hash
    user_id = claims["user_id"]
    role = claims["role"]
    table = {"candidate": "candidates", "judge": "judges", "admin": "admins"}.get(role)
    
    async with get_rls_session("admin_role") as session:
        await session.execute(
            text(f"UPDATE {table} SET password_hash = :hash, must_change_password = FALSE WHERE id = :id"),
            {"hash": get_password_hash(payload.new_password), "id": user_id}
        )
    return {"status": "success"}

