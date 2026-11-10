from fastapi import APIRouter
from sqlalchemy import text
from app.core.database import get_rls_session

router = APIRouter(prefix="/api/public", tags=["public"])

@router.get("/status")
async def get_public_status():
    """
    Public, unauthenticated endpoint for the landing page.
    Exposes ONLY: current phase, deadline, and total registered candidates.
    No names, no scores, no identities.
    """
    async with get_rls_session("admin_role") as session:
        # Get phase and deadline
        state_res = await session.execute(
            text("SELECT current_phase, stage_1_submission_deadline FROM competition_state WHERE id = 1")
        )
        state = state_res.mappings().first()
        
        # Get total registered candidates count
        count_res = await session.execute(
            text("SELECT COUNT(*) AS total FROM candidates")
        )
        count = count_res.scalar_one()
        
        return {
            "current_phase": state["current_phase"],
            "stage_1_submission_deadline": state["stage_1_submission_deadline"],
            "registered_count": count
        }
