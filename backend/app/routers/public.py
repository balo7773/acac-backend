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

from pydantic import BaseModel
from fastapi import HTTPException

class HelpRequestPayload(BaseModel):
    participant_number: str
    message: str

@router.post("/help-request")
async def submit_help_request(payload: HelpRequestPayload):
    async with get_rls_session("admin_role") as session:
        # Look up candidate by matric number
        res = await session.execute(
            text("SELECT id FROM candidates WHERE participant_number = :ident"),
            {"ident": int(payload.participant_number.strip())}
        )
        row = res.first()
        if not row:
            raise HTTPException(404, detail="Candidate not found with that Candidate Number.")
        
        await session.execute(
            text("INSERT INTO help_requests (candidate_id, message) VALUES (:cid, :msg)"),
            {"cid": row.id, "msg": payload.message}
        )
    return {"status": "submitted", "message": "Your request has been sent to the Admin."}

@router.get("/winner")
async def get_public_winner():
    async with get_rls_session("admin_role") as session:
        res = await session.execute(
            text("""
                SELECT c.full_name, c.linkedin_url, s.canonical_text AS essay_text, fr.overall_score, fr.final_rank
                FROM final_results fr
                JOIN submissions s ON s.id = fr.submission_id
                JOIN candidates c ON c.id = s.candidate_id
                WHERE fr.final_rank = 1
                LIMIT 1
            """)
        )
        row = res.mappings().first()
        if not row:
            return None
        return dict(row)

