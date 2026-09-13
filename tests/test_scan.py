import io
import json
import time

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytest

from scoreflow.db import Store
from scoreflow.omr.scan import ScanQueue, decode_identity, quality_metrics
from scoreflow.omr.template import generate_template


def roster():
    return [{"student_number": i + 1, "name": f"学生{i + 1}", "group_number": i // 7 + 1, "is_leader": i % 7 == 0} for i in range(49)]


def prepared_paper(tmp_path):
    store = Store(tmp_path / "scoreflow.sqlite3")
    project = store.create_project("测试班", "2026")
    store.import_students(project, roster())
    period = store.create_period(project, "第1周", "2026-09-01", "2026-09-07")
    sheet = store.start_period(period)
    data = store.paper_generation_data(period, sheet)
    paper_dir = tmp_path / "projects" / project / "papers"; paper_dir.mkdir(parents=True)
    pdf = paper_dir / f"paper-01-{sheet}.pdf"; manifest_path = pdf.with_suffix(".manifest.json")
    manifest = generate_template(pdf, manifest_path, project_id=project, period_id=period, sheet_id=sheet,
                                 class_name="测试班", period_name="第1周", students=data["students"])
    store.record_template_version(sheet, manifest, "font")
    return store, project, period, sheet, pdf


def test_qr_identity_handles_rotated_pages(tmp_path):
    _, _, _, _, pdf = prepared_paper(tmp_path)
    document = pdfium.PdfDocument(pdf)
    image = cv2.cvtColor(np.asarray(document[0].render(scale=300/72).to_pil().convert("RGB")), cv2.COLOR_RGB2BGR)
    identity, oriented, degrees = decode_identity(cv2.rotate(image, cv2.ROTATE_180))
    assert identity["side"] == "front"
    assert degrees == 180
    assert oriented.shape == image.shape


def test_quality_flags_low_resolution():
    metrics = quality_metrics(np.full((600, 400), 240, dtype=np.uint8))
    assert any("分辨率" in problem for problem in metrics["problems"])


def test_review_must_resolve_uncertain_slots_and_adoption_switches_version(tmp_path):
    store, project, _, sheet, _ = prepared_paper(tmp_path)
    job = store.create_scan_job(project)
    asset, _ = store.create_scan_asset(project, "one.png", "one.png", "a" * 64, 1, job)
    run = store.create_recognition_run(asset, 0)
    store.save_observations(run, [{"slot_id":"front-r00-c00-s0","classification":"review","confidence":0.0,"features":{}}])
    store.update_recognition_run(run, status="ready", sheet_id=sheet, side="front")
    store.mark_side_candidate(sheet, "front")
    with pytest.raises(ValueError, match="待复核"):
        store.adopt_run(run)
    store.set_manual_observations(run, {"front-r00-c00-s0":"slash_forward"})
    store.adopt_run(run)
    assert store.get_run(run)["adopted"] == 1
    asset2, _ = store.create_scan_asset(project, "rescan.png", "rescan.png", "c" * 64, 1, job)
    run2 = store.create_recognition_run(asset2, 0)
    store.save_observations(run2, [{"slot_id":"front-r00-c00-s0","classification":"blank","confidence":.99,"features":{}}])
    store.update_recognition_run(run2, status="ready", sheet_id=sheet, side="front")
    store.mark_side_candidate(sheet, "front"); store.adopt_run(run2)
    assert store.get_run(run)["adopted"] == 0
    assert store.get_run(run2)["adopted"] == 1


def test_blank_confirmation_conflict_requires_withdrawal(tmp_path):
    store, project, _, sheet, _ = prepared_paper(tmp_path)
    store.confirm_blank_side(sheet, "back")
    job = store.create_scan_job(project)
    asset, _ = store.create_scan_asset(project, "back.png", "back.png", "b" * 64, 1, job)
    run = store.create_recognition_run(asset, 0)
    store.save_observations(run, [{"slot_id":"back-r00-c00-s0","classification":"slash_back","confidence":.9,"features":{}}])
    store.update_recognition_run(run, status="ready", sheet_id=sheet, side="back")
    with pytest.raises(ValueError, match="撤回"):
        store.adopt_run(run)
    store.withdraw_blank_side(sheet, "back", "发现扫描件有记录")
    store.mark_side_candidate(sheet, "back")
    store.adopt_run(run)
    assert store.get_run(run)["adopted"] == 1
    note = store.add_scan_note(run, "15：主动搬器材", student_number="15", rule_name="其他－")
    assert store.list_scan_notes(run)[0]["id"] == note


def test_queue_processes_two_page_pdf_with_bounded_worker(tmp_path):
    store, project, _, _, pdf = prepared_paper(tmp_path)
    processor = ScanQueue(store, tmp_path, capacity=2)
    with pdf.open("rb") as source:
        submitted = processor.ingest(project, "scan.pdf", source)
    deadline = time.time() + 20
    while time.time() < deadline:
        job = store.get_scan_job(submitted["job_id"])
        if job["status"] in {"completed", "failed", "cancelled"}: break
        time.sleep(.05)
    assert job["status"] == "completed", job
    assert job["total_pages"] == job["completed_pages"] == 2
    runs = store.list_recognition_runs(project)
    assert len(runs) == 2
    assert {run["side"] for run in runs} == {"front", "back"}
    assert all(run["status"] == "ready" for run in runs)
    page_counts = {run["side"]: json.loads(run["quality_json"])["counts"] for run in runs}
    assert page_counts["front"]["blank"] == 1715
    assert page_counts["back"]["blank"] == 1470
    assert sum(len(store.get_run(run["id"], include_observations=True)["observations"]) for run in runs) == 3185
    with pdf.open("rb") as source:
        duplicate = processor.ingest(project, "renamed.pdf", source)
    assert duplicate["duplicate"] is True


def test_interrupted_jobs_are_recovered_as_failed(tmp_path):
    store, project, _, _, _ = prepared_paper(tmp_path)
    job = store.create_scan_job(project)
    store.update_scan_job(job, status="processing", stage="识别页面")
    ScanQueue(store, tmp_path)
    recovered = store.get_scan_job(job)
    assert recovered["status"] == "failed"
    assert "中断" in recovered["stage"]


def wait_for_job(store, job_id):
    for _ in range(200):
        job = store.get_scan_job(job_id)
        if job["status"] in {"completed","failed","cancelled"}: return job
        time.sleep(.03)
    return job


def test_unknown_qr_enters_manual_identity_then_can_be_resolved(tmp_path):
    store, project, _, sheet, pdf = prepared_paper(tmp_path)
    document = pdfium.PdfDocument(pdf)
    image = cv2.cvtColor(np.asarray(document[0].render(scale=300/72).to_pil().convert("RGB")), cv2.COLOR_RGB2BGR)
    image[140:430, 2050:2400] = 255  # retain registration squares; obscure only QR
    ok, encoded = cv2.imencode(".png", image); assert ok
    processor = ScanQueue(store, tmp_path)
    submitted = processor.ingest(project, "no-qr.png", io.BytesIO(encoded.tobytes()))
    assert wait_for_job(store, submitted["job_id"])["status"] == "completed"
    run = store.list_recognition_runs(project)[0]
    assert run["status"] == "needs_identity"
    resolved = store.resolve_sheet_reference(project, sheet[-8:])
    processor.resolve_identity(run["id"], resolved, "front")
    assert store.get_run(run["id"])["status"] == "ready"


def test_page_from_another_project_is_rejected(tmp_path):
    store, _, _, _, pdf = prepared_paper(tmp_path)
    other = store.create_project("另一个班", "2026")
    processor = ScanQueue(store, tmp_path)
    with pdf.open("rb") as source:
        submitted = processor.ingest(other, "wrong-project.pdf", source)
    assert wait_for_job(store, submitted["job_id"])["status"] == "completed"
    runs = store.list_recognition_runs(other)
    assert len(runs) == 2
    assert all(run["status"] == "failed" and "其他项目" in run["error"] for run in runs)


def test_back_page_can_arrive_before_front_page(tmp_path):
    store, project, _, _, pdf = prepared_paper(tmp_path)
    document = pdfium.PdfDocument(pdf)
    rendered = []
    for page_index in (1, 0):
        image = cv2.cvtColor(
            np.asarray(document[page_index].render(scale=300 / 72).to_pil().convert("RGB")),
            cv2.COLOR_RGB2BGR,
        )
        ok, encoded = cv2.imencode(".png", image)
        assert ok
        rendered.append(encoded.tobytes())
    processor = ScanQueue(store, tmp_path)
    first = processor.ingest(project, "back-first.png", io.BytesIO(rendered[0]))
    assert wait_for_job(store, first["job_id"])["status"] == "completed"
    assert store.list_recognition_runs(project)[0]["side"] == "back"
    second = processor.ingest(project, "front-second.png", io.BytesIO(rendered[1]))
    assert wait_for_job(store, second["job_id"])["status"] == "completed"
    assert {run["side"] for run in store.list_recognition_runs(project)} == {"front", "back"}
    processor.shutdown()


def test_severely_cropped_page_is_not_silently_aligned(tmp_path):
    store, project, _, _, pdf = prepared_paper(tmp_path)
    document = pdfium.PdfDocument(pdf)
    image = cv2.cvtColor(
        np.asarray(document[0].render(scale=300 / 72).to_pil().convert("RGB")),
        cv2.COLOR_RGB2BGR,
    )
    cropped = image[:-650, :]  # QR remains, but both lower registration marks are gone.
    ok, encoded = cv2.imencode(".png", cropped)
    assert ok
    processor = ScanQueue(store, tmp_path)
    submitted = processor.ingest(project, "cropped.png", io.BytesIO(encoded.tobytes()))
    assert wait_for_job(store, submitted["job_id"])["status"] == "completed"
    run = store.list_recognition_runs(project)[0]
    assert run["status"] == "failed"
    assert "定位" in run["error"]
    processor.shutdown()
