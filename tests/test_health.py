from fastapi.testclient import TestClient
import time

from scoreflow.main import SESSION_TOKEN, app
import scoreflow.main as main_module


def test_health_endpoint():
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_write_requires_local_session_and_csrf():
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/app/quit")
    assert response.status_code == 403


def test_project_and_custom_week_period_api():
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get(f"/session/bootstrap?token={SESSION_TOKEN}", follow_redirects=False)
        assert response.status_code == 303
        csrf = client.get("/api/session").json()["csrf_token"]
        headers = {"x-scoreflow-csrf": csrf, "origin": "http://127.0.0.1:8765"}
        project = client.post("/api/projects", json={"class_name": "二四一五班", "school_year": "2026—2027"}, headers=headers)
        assert project.status_code == 201
        period = client.post(
            f"/api/projects/{project.json()['id']}/periods",
            json={"name": "第1—2周", "start_date": "2026-09-01", "expected_end_date": "2026-09-14"},
            headers=headers,
        )
        assert period.status_code == 201
        assert period.json()["name"] == "第1—2周"


def test_full_phase2_flow_generates_downloadable_real_roster_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(main_module, "data_dir", lambda: tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        client.get(f"/session/bootstrap?token={SESSION_TOKEN}", follow_redirects=False)
        csrf = client.get("/api/session").json()["csrf_token"]
        headers = {"x-scoreflow-csrf": csrf, "origin": "http://127.0.0.1:8765"}
        project = client.post("/api/projects", json={"class_name":"八组班","school_year":"2026","group_count":8}, headers=headers).json()
        lines = ["学号,姓名,组号,是否组长"]
        for number in range(1, 50):
            group = 8 if number > 42 else (number - 1) // 6 + 1
            leader = "是" if number in {1,7,13,19,25,31,37,43} else "否"
            lines.append(f"{number},学生{number},{group},{leader}")
        preview = client.post(f"/api/projects/{project['id']}/students/preview", json={"csv_text":"\n".join(lines)}, headers=headers)
        assert preview.json()["count"] == 49
        assert client.post(f"/api/projects/{project['id']}/students/import", json={"csv_text":"\n".join(lines)}, headers=headers).status_code == 201
        period = client.post(f"/api/projects/{project['id']}/periods", json={"name":"第3—4周","start_date":"2026-09-15","expected_end_date":"2026-09-28"}, headers=headers).json()
        started = client.post(f"/api/periods/{period['id']}/start", json={}, headers=headers)
        assert started.status_code == 201
        pdf = client.get(started.json()["download_url"])
        assert pdf.status_code == 200
        assert pdf.content.startswith(b"%PDF")
        assert client.get(started.json()["download_url"]).content == pdf.content
        supplemental = client.post(f"/api/periods/{period['id']}/papers", json={}, headers=headers)
        assert supplemental.status_code == 201
        assert supplemental.json()["sheet_id"] != started.json()["sheet_id"]
        assert supplemental.json()["sheet_number"] == 2
        voided = client.post(f"/api/papers/{supplemental.json()['sheet_id']}/void-unused", json={}, headers=headers)
        assert voided.json()["status"] == "void_unused"
        manifest_files = list((tmp_path / "projects" / project["id"] / "papers").glob("*.manifest.json"))
        assert len(manifest_files) == 2
        import json
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))
        assert manifest["row_count"] == 49
        assert manifest["students"][0]["name"] == "学生1"
        uploaded = client.post(f"/api/projects/{project['id']}/scans", files=[("files", ("scan.pdf", pdf.content, "application/pdf"))], headers=headers)
        assert uploaded.status_code == 202
        job_id = uploaded.json()["items"][0]["job_id"]
        for _ in range(100):
            job = client.get(f"/api/scan-jobs/{job_id}").json()
            if job["status"] in {"completed","failed","cancelled"}: break
            time.sleep(.05)
        assert job["status"] == "completed"
        runs = client.get(f"/api/projects/{project['id']}/recognition-runs").json()
        assert {run["side"] for run in runs} == {"front","back"}
        detail = client.get(f"/api/recognition-runs/{runs[0]['id']}").json()
        assert detail["effective_counts"]["review"] == 0
        assert client.post(f"/api/recognition-runs/{runs[0]['id']}/notes", json={"note_text":"纸面备注核对完成"}, headers=headers).status_code == 201
        assert client.get(f"/api/recognition-runs/{runs[0]['id']}/notes-image").headers["content-type"] == "image/jpeg"
        corrected = client.get(f"/api/recognition-runs/{runs[0]['id']}/corrected")
        assert corrected.status_code == 200
        assert corrected.headers["content-type"] == "image/png"
        for run in runs:
            assert client.post(f"/api/recognition-runs/{run['id']}/adopt", json={}, headers=headers).status_code == 200
        duplicate = client.post(f"/api/projects/{project['id']}/scans", files=[("files", ("renamed.pdf", pdf.content, "application/pdf"))], headers=headers)
        assert duplicate.json()["items"][0]["duplicate"] is True
        blank_sheet = client.post(f"/api/periods/{period['id']}/papers", json={}, headers=headers).json()["sheet_id"]
        assert client.post(f"/api/papers/{blank_sheet}/confirm-blank", json={"side":"back"}, headers=headers).json()["status"] == "confirmed_blank"
        withdrawn = client.post(f"/api/papers/{blank_sheet}/withdraw-blank", json={"side":"back","reason":"收到补扫页"}, headers=headers)
        assert withdrawn.json()["status"] == "missing"
        assert client.post(f"/api/papers/{blank_sheet}/void-unused", json={}, headers=headers).status_code == 200
        assert client.post(f"/api/periods/{period['id']}/settle", json={}, headers=headers).json()["status"] == "settling"
        posted = client.post(f"/api/papers/{started.json()['sheet_id']}/post", json={"idempotency_key":"api-post"}, headers=headers)
        assert posted.status_code == 200
        assert client.post(f"/api/papers/{started.json()['sheet_id']}/post", json={"idempotency_key":"api-post-retry"}, headers=headers).json()["id"] == posted.json()["id"]
        results = client.get(f"/api/periods/{period['id']}/results").json()
        assert len(results["students"]) == 49
        assert all(student["total_score"] == 100 for student in results["students"])
        draft = client.post(f"/api/periods/{period['id']}/reports", json={"variant":"display","draft":True}, headers=headers)
        assert draft.status_code == 201 and draft.json()["is_draft"] is True
        assert client.get(draft.json()["download_url"]).content.startswith(b"%PDF")
        closed = client.post(f"/api/periods/{period['id']}/close", json={}, headers=headers)
        assert closed.json() == {"status":"closed","result_version":1}
        generated = {}
        for variant in ("teacher", "display"):
            report = client.post(f"/api/periods/{period['id']}/reports", json={"variant":variant}, headers=headers)
            assert report.status_code == 201 and report.json()["is_draft"] is False
            assert client.get(report.json()["download_url"]).content.startswith(b"%PDF")
            generated[variant] = report.json()
        repeated = client.post(f"/api/periods/{period['id']}/reports", json={"variant":"teacher"}, headers=headers)
        assert repeated.json()["id"] == generated["teacher"]["id"]
        assert client.get(repeated.json()["download_url"]).content == client.get(generated["teacher"]["download_url"]).content
