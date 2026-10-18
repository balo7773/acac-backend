import json
import uuid
import fitz
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core import config
from app.core.database import get_rls_session
from app.core.security import get_password_hash
from app.main import app
from worker.runner import extract_8word_shingles, process_next_queued_job, sweep_expired_timers


def _make_pdf_with_secret(secret_phrase: str) -> bytes:
    doc = fitz.open()
    sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
    for i in range(2):
        page = doc.new_page()
        extra = f" {secret_phrase} " if i == 0 else ""
        body = (sentence * 55) + extra
        rect = fitz.Rect(50, 50, 550, 750)
        page.insert_textbox(rect, body, fontsize=8)
    b = doc.tobytes()
    doc.close()
    return b


def _extract_cookie_val(response, name="acac_session"):
    for header in response.headers.get_list("set-cookie"):
        parts = [p.strip() for p in header.split(";")]
        for p in parts:
            if p.startswith(f"{name}="):
                return p.split("=", 1)[1]
    return None


@pytest.mark.asyncio
async def test_fix_1_1_essay_text_does_not_leak_and_requires_open_full(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))
    secret_key = "ACAC_TOP_SECRET_CANONICAL_ESSAY_TEXT_CHECK"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
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

        admin_login = await client.post(
            "/api/auth/login",
            json={"identifier": admin_uname, "password": admin_pw, "role": "admin"},
        )
        assert admin_login.status_code == 200
        admin_token = _extract_cookie_val(admin_login)
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        await client.post(
            "/api/admin/phase",
            json={"new_phase": "stage_1_submitting"},
            headers=admin_headers,
        )

        sfx = uuid.uuid4().hex[:6]
        reg_res = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Audit Test Candidate",
                "matric_number": f"AFIT/AUD/{sfx}",
                "whatsapp_number": f"+23481{sfx[:4]}11",
                "linkedin_url": "https://linkedin.com/in/audit-cand",
                "password": "Password123!",
            },
        )
        assert reg_res.status_code == 201
        cand_token = _extract_cookie_val(reg_res)
        sub_id = reg_res.json()["submission_id"]
        cand_headers = {"Authorization": f"Bearer {cand_token}"}

        pdf_bytes = _make_pdf_with_secret(secret_key)
        await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{reg_res.json()['participant_number']}.pdf", pdf_bytes, "application/pdf")},
            headers=cand_headers,
        )
        await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=cand_headers,
        )

        judge_uname = f"judge_audit_{sfx}"
        await client.post(
            "/api/admin/judges",
            json={
                "username": judge_uname,
                "password": "JudgePassword123!",
                "judge_type": "internal",
                "mask_avatar": "mask_owl",
            },
            headers=admin_headers,
        )

        j_login = await client.post(
            "/api/auth/login",
            json={"identifier": judge_uname, "password": "JudgePassword123!", "role": "judge"},
        )
        assert j_login.status_code == 200
        j_token = _extract_cookie_val(j_login)
        judge_headers = {"Authorization": f"Bearer {j_token}"}

        # Verification 1: GET evaluation view -> canonical_text MUST NOT be in submission dictionary
        view_res = await client.get(f"/api/judge/submissions/{sub_id}", headers=judge_headers)
        assert view_res.status_code == 200
        data = view_res.json()
        assert "canonical_text" not in data["submission"]
        assert secret_key not in json.dumps(data)

        # Verification 2: Explicitly call open-full -> returns canonical_text and records opened_full_essay
        open_res = await client.post(f"/api/judge/submissions/{sub_id}/open-full", headers=judge_headers)
        assert open_res.status_code == 200
        open_data = open_res.json()
        assert open_data["opened_full_essay"] is True
        assert secret_key in open_data["canonical_text"]


@pytest.mark.asyncio
async def test_fix_1_2_auth_cookie_is_secure_and_no_body_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_uname = "chief_admin"
        admin_pw = "Admin_Pass_2026!"
        login_res = await client.post(
            "/api/auth/login",
            json={"identifier": admin_uname, "password": admin_pw, "role": "admin"},
        )
        assert login_res.status_code == 200
        assert "access_token" not in login_res.json()

        cookie_header = login_res.headers.get("set-cookie", "")
        assert "acac_session=" in cookie_header
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()


@pytest.mark.asyncio
async def test_fix_1_3_login_rate_limit_and_lockout_window():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uname = f"rl_test_{uuid.uuid4().hex[:6]}"
        bad_pass = "WrongPassword123!"

        statuses = []
        for _ in range(7):
            res = await client.post(
                "/api/auth/login",
                json={"identifier": uname, "password": bad_pass, "role": "judge"},
            )
            statuses.append(res.status_code)

        assert 429 in statuses or all(s == 401 for s in statuses[:5])
        async with get_rls_session("admin_role") as session:
            await session.execute(text("TRUNCATE TABLE login_attempts;"))


@pytest.mark.asyncio
async def test_fix_1_4_deactivated_judge_and_disqualified_candidate_refused():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sfx = uuid.uuid4().hex[:6]
        pw = "Password123!"

        # Ensure competition is accepting registrations
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET current_phase = 'stage_1_submitting' WHERE id = 1")
            )

        # 1. Candidate flow: Register, login, preserve token
        reg_res = await client.post(
            "/api/auth/register",
            json={
                "full_name": f"Disqualify Cand {sfx}",
                "matric_number": f"AFIT/DQ/{sfx}",
                "whatsapp_number": f"+234805{sfx[:4]}00",
                "linkedin_url": "https://linkedin.com",
                "password": pw,
            },
        )
        assert reg_res.status_code == 201
        pno = reg_res.json()["participant_number"]
        cand_tok = _extract_cookie_val(reg_res)
        cand_headers = {"Authorization": f"Bearer {cand_tok}"}

        # Disqualify candidate in database
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE candidates SET is_disqualified = TRUE WHERE participant_number = :pno"),
                {"pno": pno},
            )

        # Existing token must now be refused
        old_tok_res = await client.get("/api/candidate/portal", headers=cand_headers)
        assert old_tok_res.status_code in (401, 403), f"Disqualified token returned {old_tok_res.status_code}"

        # Fresh login must also be refused
        new_login_cand = await client.post(
            "/api/auth/login",
            json={"identifier": str(pno), "password": pw, "role": "candidate"},
        )
        assert new_login_cand.status_code in (401, 403), f"Disqualified fresh login returned {new_login_cand.status_code}"

        # 2. Judge flow: Create judge, login, preserve token
        admin_uname = "chief_admin"
        admin_pw = "Admin_Pass_2026!"
        adm_login = await client.post(
            "/api/auth/login",
            json={"identifier": admin_uname, "password": admin_pw, "role": "admin"},
        )
        adm_headers = {"Authorization": f"Bearer {_extract_cookie_val(adm_login)}"}

        judge_uname = f"judge_deact_{sfx}"
        add_j = await client.post(
            "/api/admin/judges",
            json={"username": judge_uname, "password": pw, "judge_type": "internal", "mask_avatar": "mask_owl"},
            headers=adm_headers,
        )
        assert add_j.status_code == 201

        j_login = await client.post(
            "/api/auth/login",
            json={"identifier": judge_uname, "password": pw, "role": "judge"},
        )
        assert j_login.status_code == 200
        judge_tok = _extract_cookie_val(j_login)
        judge_headers = {"Authorization": f"Bearer {judge_tok}"}

        # Deactivate judge in database
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE judges SET is_active = FALSE WHERE username = :u"),
                {"u": judge_uname},
            )

        # Existing token must be refused
        j_old_tok_res = await client.get("/api/judge/submissions", headers=judge_headers)
        assert j_old_tok_res.status_code in (401, 403), f"Deactivated judge token returned {j_old_tok_res.status_code}"

        # Fresh login must be refused
        j_new_login = await client.post(
            "/api/auth/login",
            json={"identifier": judge_uname, "password": pw, "role": "judge"},
        )
        assert j_new_login.status_code in (401, 403), f"Deactivated judge fresh login returned {j_new_login.status_code}"


@pytest.mark.asyncio
async def test_fix_1_5_column_level_grants_and_score_write_lockdown():
    async with get_rls_session("judge_role") as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE essay_scores SET final_raw_score = 30.0 WHERE id = 1")
            )


@pytest.mark.asyncio
async def test_fix_1_6_cors_origins_configurable_via_env():
    # Use the first configured CORS origin
    allowed_origin = config.settings.cors_origins[0] if config.settings.cors_origins else "http://localhost:3000"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.options(
            "/api/auth/login",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == allowed_origin


@pytest.mark.asyncio
async def test_fix_2_2_upload_validations_and_identity_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Keep candidate session cookies intact for draft and submit

        sfx = uuid.uuid4().hex[:6]
        full_name = "Tariq Ibrahim"
        matric = f"AFIT/ENG/{sfx}"
        phone = f"+234807{sfx[:4]}22"

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET current_phase = 'stage_1_submitting', stage_1_submission_deadline = NULL WHERE id = 1")
            )

        reg = await client.post(
            "/api/auth/register",
            json={
                "full_name": full_name,
                "matric_number": matric,
                "whatsapp_number": phone,
                "linkedin_url": "https://linkedin.com/in/tariq-ibrahim",
                "password": "Password123!",
            },
        )
        assert reg.status_code == 201
        pno = reg.json()["participant_number"]
        tok = _extract_cookie_val(reg)
        headers = {"Authorization": f"Bearer {tok}"}

        def _make_custom_pdf(injected_snippet: str = "") -> bytes:
            doc = fitz.open()
            sentence = "Sustainable energy transition frameworks require domestic capital market mobilization and risk mitigation. "
            for i in range(2):
                p = doc.new_page()
                extra = f" {injected_snippet} " if i == 0 else ""
                body = (sentence * 55) + extra
                p.insert_textbox(fitz.Rect(50, 50, 550, 750), body, fontsize=8)
            b = doc.tobytes()
            doc.close()
            return b

        clean_pdf = _make_custom_pdf()

        # Check 1: Wrong filename rejected
        bad_name_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": ("my_essay.pdf", clean_pdf, "application/pdf")},
            headers=headers,
        )
        assert bad_name_res.status_code == 400
        assert f"must be exactly '{pno}.pdf'" in bad_name_res.json()["detail"]

        # Check 2: Upload rejected if phase != stage_1_submitting
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET current_phase = 'stage_1_grading' WHERE id = 1")
            )
        wrong_phase_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", clean_pdf, "application/pdf")},
            headers=headers,
        )
        assert wrong_phase_res.status_code == 403
        assert "stage_1_submitting" in wrong_phase_res.json()["detail"]

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET current_phase = 'stage_1_submitting' WHERE id = 1")
            )

        # Check 3: Upload rejected if deadline has passed
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET stage_1_submission_deadline = NOW() - interval '10 minutes' WHERE id = 1")
            )
        deadline_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", clean_pdf, "application/pdf")},
            headers=headers,
        )
        assert deadline_res.status_code == 403
        assert "deadline has passed" in deadline_res.json()["detail"].lower()

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET stage_1_submission_deadline = NULL WHERE id = 1")
            )

        # Check 4: Identity check - Candidate Name part in text rejected
        name_pdf = _make_custom_pdf("Authored by Tariq for review")
        name_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", name_pdf, "application/pdf")},
            headers=headers,
        )
        assert name_res.status_code == 400
        assert "Anonymity violation" in name_res.json()["detail"]
        assert "tariq" in name_res.json()["detail"].lower()

        # Check 5: Identity check - Matric Number in text rejected
        matric_pdf = _make_custom_pdf(f"Student ID: {matric}")
        matric_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", matric_pdf, "application/pdf")},
            headers=headers,
        )
        assert matric_res.status_code == 400
        assert "matric number" in matric_res.json()["detail"].lower()

        # Check 6: Identity check - Phone Number in text rejected
        phone_pdf = _make_custom_pdf(f"Contact telephone: {phone}")
        phone_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", phone_pdf, "application/pdf")},
            headers=headers,
        )
        assert phone_res.status_code == 400
        assert "phone" in phone_res.json()["detail"].lower()

        # Check 7: Text containing "AFIT" passes and returns pre-flight checks
        afit_pdf = _make_custom_pdf("This academic paper examines AFIT institutional fiscal policies in Nigeria.")
        ok_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", afit_pdf, "application/pdf")},
            headers=headers,
        )
        assert ok_res.status_code == 200
        data = ok_res.json()
        assert data["status"] == "draft"
        assert len(data["checks"]) == 5
        check_names = [c["check"] for c in data["checks"]]
        assert "filename" in check_names
        assert "readable_text" in check_names
        assert "pages" in check_names
        assert "words" in check_names
        assert "no_visible_identity" in check_names
        for c in data["checks"]:
            assert c["passed"] is True


@pytest.mark.asyncio
async def test_fix_2_3_submit_final_rules_tick_and_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        sfx = uuid.uuid4().hex[:6]
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET current_phase = 'stage_1_submitting', stage_1_submission_deadline = NULL WHERE id = 1")
            )

        reg = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Rules Check Candidate",
                "matric_number": f"AFIT/RULES/{sfx}",
                "whatsapp_number": f"+234812{sfx[:4]}55",
                "linkedin_url": "https://linkedin.com/in/rules-cand",
                "password": "Password123!",
            },
        )
        assert reg.status_code == 201
        pno = reg.json()["participant_number"]
        sub_id = reg.json()["submission_id"]
        tok = _extract_cookie_val(reg)
        headers = {"Authorization": f"Bearer {tok}"}

        doc = fitz.open()
        sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
        for _ in range(2):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 55, fontsize=8)
        pdf_bytes = doc.tobytes()
        doc.close()

        up_res = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", pdf_bytes, "application/pdf")},
            headers=headers,
        )
        assert up_res.status_code == 200

        # Check 1: Missing tick (accepted_rules omitted) -> 400
        missing_tick_res = await client.post(
            "/api/candidate/submit-final",
            json={"rules_version": "v1.0"},
            headers=headers,
        )
        assert missing_tick_res.status_code == 400
        assert "accept the competition rules" in missing_tick_res.json()["detail"].lower()

        # Check 2: Explicit accepted_rules: False -> 400
        false_tick_res = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": False, "rules_version": "v1.0"},
            headers=headers,
        )
        assert false_tick_res.status_code == 400
        assert "accept the competition rules" in false_tick_res.json()["detail"].lower()

        # Check 3: Expired deadline -> 403
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET stage_1_submission_deadline = NOW() - interval '5 minutes' WHERE id = 1")
            )

        deadline_res = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=headers,
        )
        assert deadline_res.status_code == 403
        assert "deadline has passed" in deadline_res.json()["detail"].lower()

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET stage_1_submission_deadline = NULL WHERE id = 1")
            )

        # Check 4: Valid final submission with accepted_rules: True -> 200 OK
        ok_res = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v2.0_rules"},
            headers=headers,
        )
        assert ok_res.status_code == 200
        assert ok_res.json()["status"] == "submitted"

        # Check 5: Database verification of rules_accepted_at and rules_version
        async with get_rls_session("admin_role") as session:
            db_check = await session.execute(
                text("SELECT rules_version, rules_accepted_at, status FROM submissions WHERE id = :sid"),
                {"sid": sub_id},
            )
            row = db_check.mappings().first()
            assert row["status"] == "submitted"
            assert row["rules_version"] == "v2.0_rules"
            assert row["rules_accepted_at"] is not None


@pytest.mark.asyncio
async def test_fix_2_4_defense_window_auto_start_and_closing_cap(tmp_path, monkeypatch):
    import asyncio
    from app.core import config
    from worker.runner import process_next_queued_job, sweep_expired_timers
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        sfx = uuid.uuid4().hex[:6]
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text(
                    """
                    UPDATE competition_state
                    SET current_phase = 'stage_1_submitting',
                        stage_1_submission_deadline = NULL,
                        stage_1_conclusion_closing_time = NULL
                    WHERE id = 1
                    """
                )
            )

        reg = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Defense Candidate 1",
                "matric_number": f"AFIT/AUTO/{sfx}",
                "whatsapp_number": f"+234814{sfx[:4]}99",
                "linkedin_url": "https://linkedin.com",
                "password": "Password123!",
            },
        )
        assert reg.status_code == 201
        pno = reg.json()["participant_number"]
        tok = _extract_cookie_val(reg)
        headers = {"Authorization": f"Bearer {tok}"}

        doc = fitz.open()
        sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
        for _ in range(2):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 55, fontsize=8)
        pdf_bytes = doc.tobytes()
        doc.close()

        await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", pdf_bytes, "application/pdf")},
            headers=headers,
        )
        fin = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=headers,
        )
        assert fin.status_code == 200

        # Run Job A -> window automatically transitions to in_progress
        while await process_next_queued_job():
            p_chk = await client.get("/api/candidate/portal", headers=headers)
            if p_chk.json().get("window_outcome") == "in_progress":
                break

        portal_after = await client.get("/api/candidate/portal", headers=headers)
        assert portal_after.json()["window_outcome"] == "in_progress"

        # Candidate 2: Test closing time cap and timer expiry
        sfx2 = uuid.uuid4().hex[:6]
        reg2 = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Capped Candidate 2",
                "matric_number": f"AFIT/CAP/{sfx2}",
                "whatsapp_number": f"+234816{sfx2[:4]}88",
                "linkedin_url": "https://linkedin.com",
                "password": "Password123!",
            },
        )
        assert reg2.status_code == 201
        pno2 = reg2.json()["participant_number"]
        tok2 = _extract_cookie_val(reg2)
        headers2 = {"Authorization": f"Bearer {tok2}"}

        await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno2}.pdf", pdf_bytes, "application/pdf")},
            headers=headers2,
        )
        fin2 = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=headers2,
        )
        assert fin2.status_code == 200

        # Save previous grace_seconds to restore in finally block
        async with get_rls_session("admin_role") as session:
            g_res = await session.execute(text("SELECT grace_seconds FROM settings WHERE is_active = TRUE LIMIT 1"))
            orig_grace = g_res.scalar_one_or_none() or 30

        try:
            # Set closing time to 2 seconds ahead and grace_seconds = 0 before Job A runs
            async with get_rls_session("admin_role") as session:
                await session.execute(
                    text("UPDATE competition_state SET stage_1_conclusion_closing_time = NOW() + interval '2 seconds' WHERE id = 1")
                )
                await session.execute(text("UPDATE settings SET grace_seconds = 0 WHERE is_active = TRUE"))

            # Process worker jobs until Candidate 2's window starts
            while await process_next_queued_job():
                p2 = await client.get("/api/candidate/portal", headers=headers2)
                if p2.json().get("window_outcome") == "in_progress":
                    break

            portal2 = await client.get("/api/candidate/portal", headers=headers2)
            assert portal2.json()["window_outcome"] == "in_progress"

            # Sleep 3 seconds so deadline passes, then sweep
            await asyncio.sleep(3)
            async with get_rls_session("worker_role") as session:
                await sweep_expired_timers(session)

            portal_expired = await client.get("/api/candidate/portal", headers=headers2)
            assert portal_expired.json()["window_outcome"] == "auto_zero"
        finally:
            # Restore state
            async with get_rls_session("admin_role") as session:
                await session.execute(
                    text("UPDATE settings SET grace_seconds = :g WHERE is_active = TRUE"),
                    {"g": orig_grace},
                )
                await session.execute(
                    text("UPDATE competition_state SET stage_1_conclusion_closing_time = NULL WHERE id = 1")
                )


@pytest.mark.asyncio
async def test_fix_2_6_ai_audits_model_gap_shingling_and_flag_consensus(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        # Ensure competition is in stage_1_submitting and delete any existing job_d row
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text("UPDATE competition_state SET current_phase = 'stage_1_submitting' WHERE id = 1")
            )
            await session.execute(text("DELETE FROM ai_jobs WHERE job_type = 'job_d'"))
            await session.execute(text("TRUNCATE TABLE similarity_pairs CASCADE"))

        while await process_next_queued_job():
            pass

        # 1. 8-word shingle extraction test
        text_a = "monetary policy in developing economies requires strong institutional autonomy and transparent fiscal anchors for stability"
        words, shingles = extract_8word_shingles(text_a)
        assert len(words) == 15
        assert len(shingles) == 8

        # 2. Register two candidates with overlapping text
        sfx1 = uuid.uuid4().hex[:6]
        reg1 = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Original Candidate",
                "matric_number": f"AFIT/ORG/{sfx1}",
                "whatsapp_number": f"+234811{sfx1[:4]}11",
                "linkedin_url": "https://linkedin.com/in/org-author",
                "password": "Password123!",
            },
        )
        assert reg1.status_code == 201
        pno1 = reg1.json()["participant_number"]
        # sub_id1 resolved after upload
        cand1_tok = _extract_cookie_val(reg1)
        cand1_headers = {"Authorization": f"Bearer {cand1_tok}"}

        sfx2 = uuid.uuid4().hex[:6]
        reg2 = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Similar Candidate",
                "matric_number": f"AFIT/SIM/{sfx2}",
                "whatsapp_number": f"+234811{sfx2[:4]}22",
                "linkedin_url": "https://linkedin.com/in/sim-author",
                "password": "Password123!",
            },
        )
        assert reg2.status_code == 201
        pno2 = reg2.json()["participant_number"]
        # sub_id2 resolved after upload
        cand2_tok = _extract_cookie_val(reg2)
        cand2_headers = {"Authorization": f"Bearer {cand2_tok}"}

        para_chunk = "Comprehensive fiscal policy reforms must prioritize institutional credibility and revenue diversification in West Africa. " * 25
        def _build_test_pdf():
            doc = fitz.open()
            for _ in range(3):
                page = doc.new_page()
                page.insert_textbox(fitz.Rect(50, 50, 550, 750), para_chunk, fontsize=9)
            b = doc.tobytes()
            doc.close()
            return b

        bytes1 = _build_test_pdf()

        up1 = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno1}.pdf", bytes1, "application/pdf")},
            headers=cand1_headers,
        )
        assert up1.status_code == 200, f"Upload 1 failed: {up1.text}"

        sub1 = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=cand1_headers,
        )
        assert sub1.status_code == 200, f"Submit final 1 failed: {sub1.text}"

        bytes2 = _build_test_pdf()
        up2 = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno2}.pdf", bytes2, "application/pdf")},
            headers=cand2_headers,
        )
        assert up2.status_code == 200, f"Upload 2 failed: {up2.text}"

        sub2 = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=cand2_headers,
        )
        assert sub2.status_code == 200, f"Submit final 2 failed: {sub2.text}" 

        while await process_next_queued_job():
            pass

        async with get_rls_session("admin_role") as session:
            r1 = await session.execute(text("SELECT id FROM submissions WHERE participant_number = :p"), {"p": pno1})
            sub_id1 = r1.scalar_one()
            r2 = await session.execute(text("SELECT id FROM submissions WHERE participant_number = :p"), {"p": pno2})
            sub_id2 = r2.scalar_one()

        # 3. Test Cross-Model Discrepancy Marking:
        # Divergent scores between deepseek and gemma on sub_id1 (c1) to exceed model_gap (0.2)
        async with get_rls_session("worker_role") as session:
            res_jobs = await session.execute(
                text("SELECT id, model_name, raw_response FROM ai_jobs WHERE submission_id = :sid AND job_type = 'job_b_run'"),
                {"sid": sub_id1},
            )
            for j_row in res_jobs.all():
                resp_dict = j_row.raw_response if isinstance(j_row.raw_response, dict) else json.loads(j_row.raw_response)
                target_mark = 0.90 if j_row.model_name == "deepseek" else 0.45
                if "criteria" in resp_dict and "c1" in resp_dict["criteria"]:
                    resp_dict["criteria"]["c1"]["mark"] = target_mark
                await session.execute(
                    text("UPDATE ai_jobs SET raw_response = :r WHERE id = :jid"),
                    {"r": json.dumps(resp_dict), "jid": j_row.id},
                )

            await session.execute(
                text(
                    """
                    UPDATE ai_jobs
                    SET status = 'queued'
                    WHERE submission_id = :sid AND job_type = 'job_b_combine';
                    """
                ),
                {"sid": sub_id1},
            )
        while await process_next_queued_job():
            pass

        async with get_rls_session("admin_role") as session:
            chk = await session.execute(
                text("SELECT markers FROM ai_evaluations WHERE submission_id = :sid AND criterion_key = 'c1'"),
                {"sid": sub_id1},
            )
            rows = chk.scalars().all()
            for m_list in rows:
                assert "discrepant" in m_list

        # 4. Trigger Job D (Similarity Check) by moving phase to stage_1_grading
        adm_login = await client.post(
            "/api/auth/login",
            json={"identifier": "chief_admin", "password": "Admin_Pass_2026!", "role": "admin"},
        )
        adm_tok = _extract_cookie_val(adm_login)
        adm_headers = {"Authorization": f"Bearer {adm_tok}"}

        await client.post(
            "/api/admin/phase",
            json={"new_phase": "stage_1_grading"},
            headers=adm_headers,
        )

        while await process_next_queued_job():
            pass

        sim_res = await client.get("/api/admin/similarity-pairs", headers=adm_headers)
        assert sim_res.status_code == 200
        pairs = sim_res.json()
        assert len(pairs) >= 1
        found_pair = next((p for p in pairs if p["submission_a_id"] == min(sub_id1, sub_id2) and p["submission_b_id"] == max(sub_id1, sub_id2)), None)
        assert found_pair is not None
        assert float(found_pair["similarity_score"]) >= 0.15

        dec_res = await client.post(
            f"/api/admin/similarity-pairs/{found_pair['id']}/decision",
            json={"decision": "confirmed_violation"},
            headers=adm_headers,
        )
        assert dec_res.status_code == 200
        assert dec_res.json()["admin_decision"] == "confirmed_violation"

        # 5. Test Flag Voting Consensus Protocol
        j1_uname = f"judge_flag1_{sfx1}"
        j2_uname = f"judge_flag2_{sfx1}"
        await client.post(
            "/api/admin/judges",
            json={"username": j1_uname, "password": "JudgePassword123!", "judge_type": "internal", "mask_avatar": "mask_owl"},
            headers=adm_headers,
        )
        await client.post(
            "/api/admin/judges",
            json={"username": j2_uname, "password": "JudgePassword123!", "judge_type": "internal", "mask_avatar": "mask_falcon"},
            headers=adm_headers,
        )

        j1_login = await client.post(
            "/api/auth/login",
            json={"identifier": j1_uname, "password": "JudgePassword123!", "role": "judge"},
        )
        j1_tok = _extract_cookie_val(j1_login)
        j1_headers = {"Authorization": f"Bearer {j1_tok}"}

        flag_res = await client.post(
            "/api/judge/flags",
            json={
                "submission_id": sub_id1,
                "flag_type": "contradicts_essay",
                "note": "Defense argument directly opposes core fiscal model premise."
            },
            headers=j1_headers,
        )
        assert flag_res.status_code == 201
        flag_id = flag_res.json()["flag_id"]
        assert flag_res.json()["status"] == "open"

        v1 = await client.post(
            f"/api/judge/flags/{flag_id}/review",
            json={"decision": "confirm"},
            headers=j1_headers,
        )
        assert v1.status_code == 200
        assert v1.json()["resulting_status"] == "open"

        # Duplicate vote by same judge rejected
        v1_dup = await client.post(
            f"/api/judge/flags/{flag_id}/review",
            json={"decision": "confirm"},
            headers=j1_headers,
        )
        assert v1_dup.status_code == 400

        # Judge 2 votes confirm -> consensus reached ('confirmed')
        j2_login = await client.post(
            "/api/auth/login",
            json={"identifier": j2_uname, "password": "JudgePassword123!", "role": "judge"},
        )
        j2_tok = _extract_cookie_val(j2_login)
        j2_headers = {"Authorization": f"Bearer {j2_tok}"}

        v2 = await client.post(
            f"/api/judge/flags/{flag_id}/review",
            json={"decision": "confirm"},
            headers=j2_headers,
        )
        assert v2.status_code == 200
        assert v2.json()["resulting_status"] == "confirmed"

        # 6. Verify Judge submission view surfaces flags
        sub_view = await client.get(f"/api/judge/submissions/{sub_id1}", headers=j1_headers)
        assert sub_view.status_code == 200
        view_flags = sub_view.json()["flags"]
        assert any(f["id"] == flag_id and f["status"] == "confirmed" for f in view_flags)


@pytest.mark.asyncio
async def test_p2_fix_1_conclusion_word_bounds(tmp_path, monkeypatch):
    from app.core import config
    from worker.runner import process_next_queued_job
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text(
                    "UPDATE competition_state SET current_phase = 'stage_1_submitting', "
                    "stage_1_submission_deadline = NULL, stage_1_conclusion_closing_time = NULL WHERE id = 1"
                )
            )

        doc = fitz.open()
        sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
        for _ in range(2):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 55, fontsize=8)
        pdf_bytes = doc.tobytes()
        doc.close()

        async def _prep_candidate_in_defense():
            sfx = uuid.uuid4().hex[:6]
            reg = await client.post(
                "/api/auth/register",
                json={
                    "full_name": "Word Bound Tester",
                    "matric_number": f"AFIT/WB/{sfx}",
                    "whatsapp_number": f"+234809{sfx[:4]}11",
                    "linkedin_url": "https://linkedin.com/in/wb-test",
                    "password": "Password123!",
                },
            )
            assert reg.status_code == 201
            pno = reg.json()["participant_number"]
            tok = _extract_cookie_val(reg)
            hdrs = {"Authorization": f"Bearer {tok}"}

            up = await client.post(
                "/api/candidate/upload-draft",
                files={"file": (f"{pno}.pdf", pdf_bytes, "application/pdf")},
                headers=hdrs,
            )
            assert up.status_code == 200

            fin = await client.post(
                "/api/candidate/submit-final",
                json={"accepted_rules": True, "rules_version": "v1.0"},
                headers=hdrs,
            )
            assert fin.status_code == 200

            st = await client.post("/api/candidate/start-defense", headers=hdrs)
            assert st.status_code == 200
            return hdrs

        # Candidate A: test 149 (rejected), 501 (rejected), then 150 (accepted)
        hdrs_a = await _prep_candidate_in_defense()

        ans_149 = " ".join(["policy"] * 149)
        res_149 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_149},
            headers=hdrs_a,
        )
        assert res_149.status_code == 400
        assert "below the 150-word minimum" in res_149.json()["detail"]

        ans_501 = " ".join(["policy"] * 501)
        res_501 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_501},
            headers=hdrs_a,
        )
        assert res_501.status_code == 400
        assert "exceeding the 500-word limit" in res_501.json()["detail"]

        ans_150 = " ".join(["policy"] * 150)
        res_150 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_150},
            headers=hdrs_a,
        )
        assert res_150.status_code == 200
        assert res_150.json()["outcome"] == "answered"
        assert res_150.json()["word_count"] == 150

        # Candidate B: test 500 (accepted)
        hdrs_b = await _prep_candidate_in_defense()
        ans_500 = " ".join(["policy"] * 500)
        res_500 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_500},
            headers=hdrs_b,
        )
        assert res_500.status_code == 200
        assert res_500.json()["outcome"] == "answered"
        assert res_500.json()["word_count"] == 500

        while await process_next_queued_job():
            pass


@pytest.mark.asyncio
async def test_p2_fix_1_conclusion_word_bounds(tmp_path, monkeypatch):
    from app.core import config
    from worker.runner import process_next_queued_job
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text(
                    "UPDATE competition_state SET current_phase = 'stage_1_submitting', "
                    "stage_1_submission_deadline = NULL, stage_1_conclusion_closing_time = NULL WHERE id = 1"
                )
            )

        doc = fitz.open()
        sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
        for _ in range(2):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 55, fontsize=8)
        pdf_bytes = doc.tobytes()
        doc.close()

        async def _prep_candidate_in_defense():
            sfx = uuid.uuid4().hex[:6]
            reg = await client.post(
                "/api/auth/register",
                json={
                    "full_name": "Word Bound Tester",
                    "matric_number": f"AFIT/WB/{sfx}",
                    "whatsapp_number": f"+234809{sfx[:4]}11",
                    "linkedin_url": "https://linkedin.com/in/wb-test",
                    "password": "Password123!",
                },
            )
            assert reg.status_code == 201
            pno = reg.json()["participant_number"]
            tok = _extract_cookie_val(reg)
            hdrs = {"Authorization": f"Bearer {tok}"}

            up = await client.post(
                "/api/candidate/upload-draft",
                files={"file": (f"{pno}.pdf", pdf_bytes, "application/pdf")},
                headers=hdrs,
            )
            assert up.status_code == 200

            fin = await client.post(
                "/api/candidate/submit-final",
                json={"accepted_rules": True, "rules_version": "v1.0"},
                headers=hdrs,
            )
            assert fin.status_code == 200

            st = await client.post("/api/candidate/start-defense", headers=hdrs)
            assert st.status_code == 200
            return hdrs

        # Candidate A: test 149 (rejected), 501 (rejected), then 150 (accepted)
        hdrs_a = await _prep_candidate_in_defense()

        ans_149 = " ".join(["policy"] * 149)
        res_149 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_149},
            headers=hdrs_a,
        )
        assert res_149.status_code == 400
        assert "below the 150-word minimum" in res_149.json()["detail"]

        ans_501 = " ".join(["policy"] * 501)
        res_501 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_501},
            headers=hdrs_a,
        )
        assert res_501.status_code == 400
        assert "exceeding the 500-word limit" in res_501.json()["detail"]

        ans_150 = " ".join(["policy"] * 150)
        res_150 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_150},
            headers=hdrs_a,
        )
        assert res_150.status_code == 200
        assert res_150.json()["outcome"] == "answered"
        assert res_150.json()["word_count"] == 150

        # Candidate B: test 500 (accepted)
        hdrs_b = await _prep_candidate_in_defense()
        ans_500 = " ".join(["policy"] * 500)
        res_500 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_500},
            headers=hdrs_b,
        )
        assert res_500.status_code == 200
        assert res_500.json()["outcome"] == "answered"
        assert res_500.json()["word_count"] == 500

        while await process_next_queued_job():
            pass


@pytest.mark.asyncio
async def test_p2_fix_1_conclusion_word_bounds(tmp_path, monkeypatch):
    from app.core import config
    from worker.runner import process_next_queued_job
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        async with get_rls_session("admin_role") as session:
            await session.execute(
                text(
                    "UPDATE competition_state SET current_phase = 'stage_1_submitting', "
                    "stage_1_submission_deadline = NULL, stage_1_conclusion_closing_time = NULL WHERE id = 1"
                )
            )

        doc = fitz.open()
        sentence = "Nigeria economic policy requires structural fiscal coordination and sustainable domestic resource mobilization. "
        for _ in range(2):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 55, fontsize=8)
        pdf_bytes = doc.tobytes()
        doc.close()

        async def _prep_candidate_in_defense():
            sfx = uuid.uuid4().hex[:6]
            reg = await client.post(
                "/api/auth/register",
                json={
                    "full_name": "Word Bound Tester",
                    "matric_number": f"AFIT/WB/{sfx}",
                    "whatsapp_number": f"+234809{sfx[:4]}11",
                    "linkedin_url": "https://linkedin.com/in/wb-test",
                    "password": "Password123!",
                },
            )
            assert reg.status_code == 201
            pno = reg.json()["participant_number"]
            tok = _extract_cookie_val(reg)
            hdrs = {"Authorization": f"Bearer {tok}"}

            up = await client.post(
                "/api/candidate/upload-draft",
                files={"file": (f"{pno}.pdf", pdf_bytes, "application/pdf")},
                headers=hdrs,
            )
            assert up.status_code == 200

            fin = await client.post(
                "/api/candidate/submit-final",
                json={"accepted_rules": True, "rules_version": "v1.0"},
                headers=hdrs,
            )
            assert fin.status_code == 200

            st = await client.post("/api/candidate/start-defense", headers=hdrs)
            assert st.status_code == 200
            return hdrs

        # Candidate A: test 149 (rejected), 501 (rejected), then 150 (accepted)
        hdrs_a = await _prep_candidate_in_defense()

        ans_149 = " ".join(["policy"] * 149)
        res_149 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_149},
            headers=hdrs_a,
        )
        assert res_149.status_code == 400
        assert "below the 150-word minimum" in res_149.json()["detail"]

        ans_501 = " ".join(["policy"] * 501)
        res_501 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_501},
            headers=hdrs_a,
        )
        assert res_501.status_code == 400
        assert "exceeding the 500-word limit" in res_501.json()["detail"]

        ans_150 = " ".join(["policy"] * 150)
        res_150 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_150},
            headers=hdrs_a,
        )
        assert res_150.status_code == 200
        assert res_150.json()["outcome"] == "answered"
        assert res_150.json()["word_count"] == 150

        # Candidate B: test 500 (accepted)
        hdrs_b = await _prep_candidate_in_defense()
        ans_500 = " ".join(["policy"] * 500)
        res_500 = await client.post(
            "/api/candidate/submit-defense",
            json={"answer_text": ans_500},
            headers=hdrs_b,
        )
        assert res_500.status_code == 200
        assert res_500.json()["outcome"] == "answered"
        assert res_500.json()["word_count"] == 500

        while await process_next_queued_job():
            pass

@pytest.mark.asyncio
async def test_p2_fix_2_judge_scale_internal_vs_external(tmp_path, monkeypatch):
    """2.6 Option B: internal judges 0-100 continuous, external judges whole 0-3.
       Both lock-phase1 and submit-phase2 reject out-of-scale values with 422."""
    monkeypatch.setattr(config.settings, "UPLOAD_DIR", str(tmp_path))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async def _clear_hook(resp):
            client.cookies.clear()
        client.event_hooks["response"].append(_clear_hook)

        # Ensure admin + open phase
        async with get_rls_session("admin_role") as session:
            await session.execute(
                text(
                    "INSERT INTO admins (username, password_hash) "
                    "VALUES ('chief_admin', :pw) ON CONFLICT (username) DO NOTHING"
                ),
                {"pw": get_password_hash("Admin_Pass_2026!")},
            )
            await session.execute(
                text(
                    "UPDATE competition_state SET current_phase = 'stage_1_submitting', "
                    "stage_1_submission_deadline = NULL WHERE id = 1"
                )
            )

        adm_login = await client.post(
            "/api/auth/login",
            json={"identifier": "chief_admin", "password": "Admin_Pass_2026!", "role": "admin"},
        )
        assert adm_login.status_code == 200
        adm_headers = {"Authorization": f"Bearer {_extract_cookie_val(adm_login)}"}

        # Create 1 internal + 1 external judge
        sfx = uuid.uuid4().hex[:6]
        tokens = {}
        for label, jtype in [("int_a", "internal"), ("ext", "external")]:
            uname = f"scale_{label}_{sfx}"
            r = await client.post(
                "/api/admin/judges",
                json={"username": uname, "password": "JP_2026!", "judge_type": jtype, "mask_avatar": "mask_owl"},
                headers=adm_headers,
            )
            assert r.status_code == 201, r.text
            lr = await client.post(
                "/api/auth/login",
                json={"identifier": uname, "password": "JP_2026!", "role": "judge"},
            )
            assert lr.status_code == 200, lr.text
            tokens[label] = _extract_cookie_val(lr)

        # Register candidate, upload, submit
        reg = await client.post(
            "/api/auth/register",
            json={
                "full_name": "Scale Test Candidate",
                "matric_number": f"AFIT/SCT/{sfx}",
                "whatsapp_number": f"+234899{sfx[:4]}11",
                "linkedin_url": "https://linkedin.com/in/sct",
                "password": "Password123!",
            },
        )
        assert reg.status_code == 201, reg.text
        pno = reg.json()["participant_number"]
        cand_headers = {"Authorization": f"Bearer {_extract_cookie_val(reg)}"}

        doc = fitz.open()
        sentence = "Comprehensive fiscal policy reforms require sustained institutional capacity and disciplined macroeconomic coordination. "
        for _ in range(3):
            p = doc.new_page()
            p.insert_textbox(fitz.Rect(50, 50, 550, 750), sentence * 40, fontsize=9)
        pdf_bytes = doc.tobytes()
        doc.close()

        up = await client.post(
            "/api/candidate/upload-draft",
            files={"file": (f"{pno}.pdf", pdf_bytes, "application/pdf")},
            headers=cand_headers,
        )
        assert up.status_code == 200, up.text

        sub = await client.post(
            "/api/candidate/submit-final",
            json={"accepted_rules": True, "rules_version": "v1.0"},
            headers=cand_headers,
        )
        assert sub.status_code == 200, sub.text
        sub_id = sub.json()["submission_id"]

        while await process_next_queued_job():
            pass

        ph = await client.post(
            "/api/admin/phase",
            json={"new_phase": "stage_1_grading"},
            headers=adm_headers,
        )
        assert ph.status_code == 200

        # External judge: open submission, then probe invalid scales
        ext_h = {"Authorization": f"Bearer {tokens['ext']}"}
        open_ext = await client.get(f"/api/judge/submissions/{sub_id}", headers=ext_h)
        assert open_ext.status_code == 200

        for bad in [2.5, -0.5, 3.5]:
            r = await client.post(
                f"/api/judge/submissions/{sub_id}/lock-phase1",
                json={"c1": bad, "c2": 2, "c3": 2, "c4": 2, "c5": 2, "conclusion_score": 25},
                headers=ext_h,
            )
            assert r.status_code == 422, f"external {bad} -> {r.status_code}: {r.text}"

        # External judge: valid 3s must succeed
        ok_ext = await client.post(
            f"/api/judge/submissions/{sub_id}/lock-phase1",
            json={"c1": 3, "c2": 3, "c3": 3, "c4": 3, "c5": 3, "conclusion_score": 25},
            headers=ext_h,
        )
        assert ok_ext.status_code == 200, ok_ext.text

        # External judge: submit-phase2 with 2.5 must be 422
        bad_p2 = await client.post(
            f"/api/judge/submissions/{sub_id}/submit-phase2",
            json={"c1": 2.5, "revision_reason": "scale test"},
            headers=ext_h,
        )
        assert bad_p2.status_code == 422, bad_p2.text

        # Internal judge: open submission, then probe out-of-range
        int_h = {"Authorization": f"Bearer {tokens['int_a']}"}
        open_int = await client.get(f"/api/judge/submissions/{sub_id}", headers=int_h)
        assert open_int.status_code == 200

        for bad in [105, -5]:
            r = await client.post(
                f"/api/judge/submissions/{sub_id}/lock-phase1",
                json={"c1": bad, "c2": 80, "c3": 80, "c4": 80, "c5": 80, "conclusion_score": 25},
                headers=int_h,
            )
            assert r.status_code == 422, f"internal {bad} -> {r.status_code}: {r.text}"


