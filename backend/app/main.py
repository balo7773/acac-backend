from fastapi import FastAPI
from datetime import datetime, timezone
import time as _time
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.database import get_rls_session
from app.routers import admin, auth, candidates, judges, public

app = FastAPI(
    title="ACAC High-Integrity Competition Engine",
    version="1.0.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/time")
def server_time():
    """Unauthenticated server clock. Frontend uses this to compute offset
    between local clock and server clock for all countdown timers."""
    now = datetime.now(timezone.utc)
    return {
        "server_now": now.isoformat(),
        "epoch": _time.time(),
    }


app.include_router(auth.router)
app.include_router(candidates.router)
app.include_router(judges.router)
app.include_router(admin.router)
app.include_router(public.router)


@app.get("/api/health")
async def health_check():
    async with get_rls_session("admin_role") as session:
        res = await session.execute(
            text("SELECT current_phase FROM competition_state WHERE id = 1")
        )
        phase = res.scalar_one()
    return {"status": "healthy", "current_phase": phase}
