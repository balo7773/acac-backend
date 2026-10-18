import uuid
import fitz
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import get_rls_session
from app.core.security import get_password_hash
from app.main import app
from worker.runner import process_next_queued_job


def _make_sample_pdf_bytes() -> bytes:
    doc = fitz.open()
    sentences = [
        "Nigeria monetary policy transmission requires coordinated liquidity management across commercial banks.",
        "Fiscal deficits must be anchored by transparent debt sustainability rules and domestic revenue mobilization.",
        "Exchange rate stability depends on export diversification and predictable central bank auction mechanisms.",
        "Institutional reforms in capital markets strengthen long-term infrastructure financing and investor confidence.",
        "Quantitative risk controls prevent systemic banking fragility during external commodity price shocks.",
    ]
    for _ in range(2):
        page = doc.new_page()
        body = " ".join(sentences * 9)
        rect = fitz.Rect(50, 50, 550, 750)
        page.insert_textbox(rect, body, fontsize=9)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def _get_cookie_val(response, name="acac_session"):
    for header in response.headers.get_list("set-cookie"):
        parts = [p.strip() for p in header.split(";")]
        for p in parts:
            if p.startswith(f"{name}="):
                return p.split("=", 1)[1]
    return None


@pytest.mark.asyncio
async def test_full_competition_lifecycle(tmp_path, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    # 1. Seed test admin account directly (username = 'chief_admin')
    admin_uname = "chief_admin"
    admin_pw = "Admin_Pass_2026!"
    async with get_rls_session("admin_role") as session:
        await session.execute(
            text(
                """
                INSERT INTO admins (username, password_hash)
                VALUES (:uname, :pw)
                ON CONFLICT (username) DO NOTHING
                """
            ),
            {"uname": admin_uname, "pw": get_password_hash(admin_pw)},
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_cookies_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_cookies_hook)

        # 2. Admin logs in with username and opens Stage 1 Submissions
        res = await client.post(
            "/api/auth/login",
            json={"identifier": admin_uname, "password": admin_pw, "role": "admin"},
        )
        assert res.status_code == 200
        assert "access_token" not in res.json()
        admin_token = _get_cookie_val(res)
        assert admin_token is not None
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        res = await client.post(
            "/api/admin/phase",
            json={"new_phase": "stage_1_submitting"},
            headers=admin_headers,
        )
        assert res.status_code == 200

        # 3. Admin creates 3 judges (2 internal, 1 external)
        sfx = uuid.uuid4().hex[:6]
        judge_tokens = []
        judge_specs = [
            (f"judge_alpha_{sfx}", "internal", "mask_owl"),
            (f"judge_beta_{sfx}", "internal", "mask_falcon"),
            (f"judge_gamma_{sfx}", "external", "mask_lion"),
        ]
        for uname, jtype, avatar in judge_specs:
            create_j = await client.post(
                "/api/admin/judges",
                json={
                    "username": uname,
                    "password": "Judge_Pass_2026!",
                    "judge_type": jtype,
                    "mask_avatar": avatar,
                },
                headers=admin_headers,
            )
            assert create_j.status_code == 201
            j_login = await client.post(
                "/api/auth/login",
                json={"identifier": uname, "password": "Judge_Pass_2026!", "role": "judge"},
            )
            assert j_login.status_code == 200
            j_tok = _get_cookie_val(j_login)
            assert j_tok is not None
            judge_tokens.append(j_tok)

        # 4. Candidate registers and receives their anonymous participant_number
        reg_res = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Abdulmalik Test Candidate",
                "matric_number": f"AFIT/2026/{sfx}",
                "whatsapp_number": f"+23480{sfx[:4]}99",
                "linkedin_url": "https://linkedin.com/in/abdulmalik-test",
                "password": "Cand_Pass_2026!",
            },
        )
        assert reg_res.status_code == 201
        assert "access_token" not in reg_res.json()
        cand_data = reg_res.json()
        sub_id = cand_data["submission_id"]
        part_no = str(cand_data["participant_number"])

        # 5. Candidate logs in strictly with participant_number + password
        cand_login = await client.post(
            "/api/auth/login",
            json={"identifier": part_no, "password": "Cand_Pass_2026!", "role": "candidate"},
        )
        assert cand_login.status_code == 200
        cand_tok = _get_cookie_val(cand_login)
        assert cand_tok is not None
        cand_headers = {"Authorization": f"Bearer {cand_tok}"}

        # 6. Upload PDF draft and submit final essay
        pdf_bytes = _make_sample_pdf_bytes()
        up_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{part_no}.pdf", pdf_bytes, "application/pdf")},
            headers=cand_headers,
        )
        assert up_res.status_code == 200
        assert up_res.json()["word_count"] >= 50

        fin_res = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=cand_headers,
        )
        assert fin_res.status_code == 200
        assert fin_res.json()["status"] == "submitted"

        # 7. Run worker queue to process Job A and Job B
        while await process_next_queued_job():
            pass

        # 8. Candidate starts 10-min defense window and submits conclusion answer
        start_def = await client.post("/api/candidate/start-defense", headers=cand_headers)
        assert start_def.status_code == 200
        assert start_def.json()["outcome"] == "in_progress"

        sub_def = await client.post(
            "/api/candidate/submit-defense",
            json={
                "answer_text": " ".join(["If liquidity assumptions fail in year one, our contingency triggers a tiered reserve ratio adjustment."] * 14)
            },
            headers=cand_headers,
        )
        assert sub_def.status_code == 200
        assert sub_def.json()["outcome"] == "answered"

        while await process_next_queued_job():
            pass

        # 9. Admin moves phase to stage_1_grading
        await client.post(
            "/api/admin/phase",
            json={"new_phase": "stage_1_grading"},
            headers=admin_headers,
        )
        while await process_next_queued_job():
            pass

        # 10. Judge 1 opens submission in Phase 1 -> Verify AI summaries are visible but marks/reasons are NULL
        j1_headers = {"Authorization": f"Bearer {judge_tokens[0]}"}
        view_p1 = await client.get(f"/api/judge/submissions/{sub_id}", headers=j1_headers)
        assert view_p1.status_code == 200
        bundle_p1 = view_p1.json()["bundle"]
        assert bundle_p1["session_status"] == "phase_1_in_progress"
        assert len(bundle_p1["ai_evaluations"]) == 10
        for ev in bundle_p1["ai_evaluations"]:
            assert ev["description_text"] is not None
            assert ev["mark"] is None
            assert ev["reason_text"] is None
        assert bundle_p1["ai_conclusion_audits"] == []

        # 11. Judge 1 locks Phase 1 scores -> Transitions to Phase 2
        lock_res = await client.post(
            f"/api/judge/submissions/{sub_id}/lock-phase1",
            json={"c1": 80, "c2": 80, "c3": 80, "c4": 80, "c5": 80, "conclusion_score": 25},
            headers=j1_headers,
        )
        assert lock_res.status_code == 200
        assert lock_res.json()["session_status"] == "phase_2_review"

        # Verify AI marks and reasons are now revealed in Phase 2
        view_p2 = await client.get(f"/api/judge/submissions/{sub_id}", headers=j1_headers)
        bundle_p2 = view_p2.json()["bundle"]
        assert bundle_p2["session_status"] == "phase_2_review"
        for ev in bundle_p2["ai_evaluations"]:
            assert ev["mark"] is not None
            assert ev["reason_text"] is not None
        assert len(bundle_p2["ai_conclusion_audits"]) == 2

        # Attempting to revise a score in Phase 2 WITHOUT revision_reason MUST fail
        bad_rev = await client.post(
            f"/api/judge/submissions/{sub_id}/submit-phase2",
            json={"c1": 85},
            headers=j1_headers,
        )
        assert bad_rev.status_code == 400
        assert "revision_reason is required" in bad_rev.json()["detail"]

        # Submitting Phase 2 revision WITH written justification succeeds
        good_rev = await client.post(
            f"/api/judge/submissions/{sub_id}/submit-phase2",
            json={"c1": 90, "revision_reason": "AI quote highlighted stronger fiscal evidence in C1."},
            headers=j1_headers,
        )
        assert good_rev.status_code == 200
        assert good_rev.json()["session_status"] == "locked_final"

        # 12. Judges 2 and 3 open submission, then complete Phase 1 and Phase 2
        for j_tok in judge_tokens[1:]:
            jh = {"Authorization": f"Bearer {j_tok}"}
            open_j = await client.get(f"/api/judge/submissions/{sub_id}", headers=jh)
            assert open_j.status_code == 200
            l_res = await client.post(
                f"/api/judge/submissions/{sub_id}/lock-phase1",
                json={"c1": 80, "c2": 80, "c3": 80, "c4": 80, "c5": 80, "conclusion_score": 25},
                headers=jh,
            )
            assert l_res.status_code == 200
            s_res = await client.post(
                f"/api/judge/submissions/{sub_id}/submit-phase2",
                json={},
                headers=jh,
            )
            assert s_res.status_code == 200

        # 13. Admin computes Stage 1 Leaderboard
        comp_res = await client.post("/api/admin/compute-stage1", headers=admin_headers)
        assert comp_res.status_code == 200
        board = comp_res.json()["leaderboard"]
        assert len(board) >= 1
        target_row = next((r for r in board if r["submission_id"] == sub_id), None)
        assert target_row is not None, f"Submission {sub_id} not found on leaderboard"
        assert float(target_row["composite_score"]) > 80.0
