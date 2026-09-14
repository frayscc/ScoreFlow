import sqlite3
import time
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from scoreflow.db import Store
from scoreflow.omr.scan import ScanQueue
from scoreflow.omr.template import generate_template


def _roster():
    return [
        {"student_number": index + 1, "name": f"演示学生{index + 1}", "group_number": index // 7 + 1, "is_leader": index % 7 == 0}
        for index in range(49)
    ]


def _prepared(tmp_path):
    store = Store(tmp_path / "scoreflow.sqlite3")
    project = store.create_project("演示班", "2026")
    store.import_students(project, _roster())
    period = store.create_period(project, "第1周", "2026-09-01", "2026-09-07")
    sheet = store.start_period(period)
    data = store.paper_generation_data(period, sheet)
    paper_dir = tmp_path / "projects" / project / "papers"
    paper_dir.mkdir(parents=True)
    pdf = paper_dir / f"paper-01-{sheet}.pdf"
    manifest = pdf.with_suffix(".manifest.json")
    layout = generate_template(pdf, manifest, project_id=project, period_id=period, sheet_id=sheet,
                               class_name="演示班", period_name="第1周", students=data["students"])
    store.record_template_version(sheet, layout, "font")
    return store, project, period, sheet, pdf


def _wait(store, job_id):
    for _ in range(400):
        job = store.get_scan_job(job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(0.03)
    raise AssertionError("scan job timed out")


def _fake_run(store, project, sheet, suffix, side="front", marks=None):
    job = store.create_scan_job(project)
    asset, _ = store.create_scan_asset(project, f"{suffix}.png", f"{suffix}.png", suffix.ljust(64, "0"), 10, job)
    run = store.create_recognition_run(asset, 0)
    observations = [
        {"slot_id":f"{side}-r00-c00-s{index}", "classification":mark, "confidence":.99, "features":{}}
        for index, mark in enumerate(marks or ["blank"])
    ]
    store.save_observations(run, observations)
    store.update_recognition_run(run, status="ready", sheet_id=sheet, side=side,
                                 quality={"counts":{"blank":sum(mark == "blank" for mark in (marks or ["blank"]))}, "predicted_delta":0})
    store.mark_side_candidate(sheet, side)
    return run


def test_blank_pdf_delete_allows_same_file_reimport_and_multi_page_is_independent(tmp_path):
    store, project, _, sheet, pdf = _prepared(tmp_path)
    queue = ScanQueue(store, tmp_path)
    with pdf.open("rb") as source:
        first = queue.ingest(project, "空白表01.pdf", source)
    assert _wait(store, first["job_id"])["status"] == "completed"
    runs = store.list_recognition_runs(project)
    assert len(runs) == len(pdfium.PdfDocument(pdf)) == 2
    assert all(sum(value for key, value in page["counts"].items() if key != "blank") == 0
               for page in (record["quality"] for record in store.list_scan_imports(project)[0]["pages"]))

    front = next(run for run in runs if run["side"] == "front")
    back = next(run for run in runs if run["side"] == "back")
    retried_page = queue.retry(front["id"])
    assert _wait(store, retried_page["job_id"])["status"] == "completed"
    assert store.get_run(front["id"])["status"] == "ready"
    one = store.remove_recognition_runs([front["id"]], "删除正面测试页")
    assert one["hard_deleted"] == 1
    assert store.get_run(back["id"])["side"] == "back"
    assert store.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side='front'", (sheet,)).fetchone()[0] == "missing"
    assert store.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side='back'", (sheet,)).fetchone()[0] == "candidate"

    last = store.remove_recognition_runs([back["id"]], "删除剩余测试页")
    for value in [*one["paths_to_delete"], *last["paths_to_delete"]]:
        Path(value).unlink(missing_ok=True)
    with pdf.open("rb") as source:
        again = queue.ingest(project, "空白表01-重导.pdf", source)
    assert again["duplicate"] is False
    assert _wait(store, again["job_id"])["status"] == "completed"
    assert len(store.list_recognition_runs(project)) == 2
    with pdf.open("rb") as source:
        duplicate = queue.ingest(project, "改名后的重复文件.pdf", source)
    assert duplicate["duplicate"] is True
    newest = next(record for record in store.list_scan_imports(project) if record["import_job_id"] == duplicate["job_id"])
    assert newest["is_duplicate"] == 1 and newest["original_filename"] == "改名后的重复文件.pdf"
    assert newest["pages"] == []
    queue.shutdown()


def test_deleting_old_candidate_does_not_change_adopted_but_current_returns_to_missing(tmp_path):
    store, project, _, sheet, _ = _prepared(tmp_path)
    old = _fake_run(store, project, sheet, "old")
    store.adopt_run(old)
    current = _fake_run(store, project, sheet, "current")
    store.adopt_run(current)
    store.cancel_run_adoption(current)
    assert store.get_run(current)["adopted"] == 0
    assert store.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side='front'", (sheet,)).fetchone()[0] == "candidate"
    store.adopt_run(current)
    store.remove_recognition_runs([old], "删除未采用旧版")
    assert store.get_run(current)["adopted"] == 1
    assert store.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side='front'", (sheet,)).fetchone()[0] == "reviewed"

    spare = _fake_run(store, project, sheet, "spare")
    store.remove_recognition_runs([current], "删除当前采用版")
    assert store.get_run(spare)["adopted"] == 0
    assert store.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side='front'", (sheet,)).fetchone()[0] == "missing"


def test_posted_zero_score_requires_atomic_whole_sheet_reversal_and_retains_history(tmp_path):
    store, project, period, sheet, _ = _prepared(tmp_path)
    run = _fake_run(store, project, sheet, "blank-posted")
    store.adopt_run(run)
    store.confirm_blank_side(sheet, "back")
    store.begin_settlement(period)
    posted = store.post_sheet(sheet, "post-zero")
    assert posted["entry_count"] == 0
    preview = store.scan_removal_preview([run])
    assert preview == {"page_count":1, "requires_reversal":1, "affected_people":0, "score_delta":0}
    with pytest.raises(ValueError, match="撤销整表"):
        store.remove_recognition_runs([run], "不应直接删")

    removed = store.remove_recognition_runs([run], "空白表重新核对", reverse_posted=True, idempotency_prefix="remove-zero")
    assert removed["history_retained"] == 1 and removed["reversed_sheets"] == [sheet]
    assert store.period_results(period)["students"][0]["total_score"] == 100
    historical = store.get_run(run)
    assert historical["removed_at"] and historical["removal_reason"] == "空白表重新核对"
    assert run not in {item["id"] for item in store.list_recognition_runs(project)}
    sides = dict(store.connection.execute("SELECT side,status FROM paper_sides WHERE sheet_id=?", (sheet,)).fetchall())
    assert sides == {"front":"missing", "back":"confirmed_blank"}


def test_reverse_and_remove_rolls_back_as_one_transaction_on_failure(tmp_path):
    store, project, period, sheet, _ = _prepared(tmp_path)
    run = _fake_run(store, project, sheet, "atomic", marks=["slash_forward"])
    store.adopt_run(run)
    store.confirm_blank_side(sheet, "back")
    store.begin_settlement(period)
    original = store.post_sheet(sheet, "post-atomic")
    store.connection.execute(
        "CREATE TRIGGER fail_remove_audit BEFORE INSERT ON audit_events "
        "WHEN NEW.action='remove_recognition_run' BEGIN SELECT RAISE(ABORT,'forced remove failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="forced remove failure"):
        store.remove_recognition_runs([run], "测试事务回滚", reverse_posted=True, idempotency_prefix="atomic")
    assert store.connection.execute("SELECT status FROM paper_sheets WHERE id=?", (sheet,)).fetchone()[0] == "posted"
    assert store.connection.execute("SELECT status FROM posting_batches WHERE id=?", (original["id"],)).fetchone()[0] == "posted"
    assert store.connection.execute("SELECT count(*) FROM posting_batches WHERE reverses_batch_id=?", (original["id"],)).fetchone()[0] == 0
    assert store.get_run(run)["adopted"] == 1 and store.get_run(run)["removed_at"] is None


def test_delete_requested_during_processing_has_no_late_write_and_can_reimport(tmp_path):
    store, project, _, _, pdf = _prepared(tmp_path)
    queue = ScanQueue(store, tmp_path)
    with pdf.open("rb") as source:
        submitted = queue.ingest(project, "cancel-me.pdf", source)
    result = store.request_scan_asset_deletion(submitted["asset_id"], "取消测试导入")
    if result["status"] == "deleted":
        for value in result["paths_to_delete"]:
            Path(value).unlink(missing_ok=True)
    assert _wait(store, submitted["job_id"])["status"] == "cancelled" or result["status"] == "deleted"
    assert store.connection.execute("SELECT 1 FROM recognition_runs WHERE scan_asset_id=?", (submitted["asset_id"],)).fetchone() is None
    assert store.connection.execute("SELECT 1 FROM scan_assets WHERE id=?", (submitted["asset_id"],)).fetchone() is None
    with pdf.open("rb") as source:
        retried = queue.ingest(project, "cancel-me-again.pdf", source)
    assert retried["duplicate"] is False
    assert _wait(store, retried["job_id"])["status"] == "completed"
    queue.shutdown()
