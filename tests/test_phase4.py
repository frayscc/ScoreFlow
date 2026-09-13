import sqlite3

import pytest

from scoreflow.db import Store
from scoreflow.reports import generate_period_report

import pypdfium2 as pdfium


def roster():
    return [
        {"student_number": i + 1, "name": f"学生{i + 1}", "group_number": i // 7 + 1, "is_leader": i % 7 == 0}
        for i in range(49)
    ]


def prepared_period(tmp_path):
    store = Store(tmp_path / "scoreflow.sqlite3")
    project = store.create_project("测试班", "2026")
    store.import_students(project, roster())
    period = store.create_period(project, "第1周", "2026-09-01", "2026-09-07")
    first = store.start_period(period)
    return store, project, period, first


def adopt_front(store, project, sheet, suffix, marks):
    job = store.create_scan_job(project)
    asset, _ = store.create_scan_asset(project, f"{suffix}.png", f"{suffix}.png", suffix.ljust(64, "0"), 10, job)
    run = store.create_recognition_run(asset, 0)
    observations = []
    for slot_index, classification in enumerate(marks):
        observations.append({
            "slot_id": f"front-r00-c00-s{slot_index}",
            "classification": classification,
            "confidence": 0.99,
            "features": {},
        })
    store.save_observations(run, observations)
    store.update_recognition_run(run, status="ready", sheet_id=sheet, side="front")
    store.mark_side_candidate(sheet, "front")
    store.adopt_run(run)
    return run


def complete_sheet(store, project, sheet, suffix, marks):
    adopt_front(store, project, sheet, suffix, marks)
    store.confirm_blank_side(sheet, "back")


def test_two_sheets_accumulate_x_is_zero_and_clicks_are_idempotent(tmp_path):
    store, project, period, first = prepared_period(tmp_path)
    second = store.issue_sheet(period)
    complete_sheet(store, project, first, "first", ["slash_forward", "x", "blank", "slash_back"])
    complete_sheet(store, project, second, "second", ["slash_forward", "blank"])
    store.begin_settlement(period)
    one = store.post_sheet(first, "post-first")
    assert store.post_sheet(first, "post-first")["id"] == one["id"]
    assert store.post_sheet(first, "different-click")["id"] == one["id"]
    store.post_sheet(second, "post-second")
    result = store.period_results(period)
    assert result["students"][0]["total_score"] == 115
    front_rule = next(rule for rule in result["rules"] if rule["side"] == "front" and rule["sort_order"] == 0)
    assert result["students"][0]["rules"][front_rule["rule_id"]]["mark_count"] == 3


def test_posting_failure_rolls_back_everything(tmp_path):
    store, project, period, sheet = prepared_period(tmp_path)
    complete_sheet(store, project, sheet, "rollback", ["slash_forward"])
    store.begin_settlement(period)
    store.connection.execute(
        "CREATE TRIGGER fail_ledger BEFORE INSERT ON ledger_entries BEGIN SELECT RAISE(ABORT,'forced'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="forced"):
        store.post_sheet(sheet, "will-fail")
    assert store.connection.execute("SELECT count(*) FROM posting_batches").fetchone()[0] == 0
    assert store.connection.execute("SELECT status FROM paper_sheets WHERE id=?", (sheet,)).fetchone()[0] == "issued"
    assert store.connection.execute("SELECT status FROM paper_sides WHERE sheet_id=? AND side='front'", (sheet,)).fetchone()[0] == "reviewed"


def test_reversal_creates_linked_negative_ledger_and_allows_repost(tmp_path):
    store, project, period, sheet = prepared_period(tmp_path)
    complete_sheet(store, project, sheet, "reverse", ["slash_forward", "slash_back"])
    store.begin_settlement(period)
    original = store.post_sheet(sheet, "post")
    original_row = store.connection.execute("SELECT front_run_id,back_run_id FROM posting_batches WHERE id=?", (original["id"],)).fetchone()
    assert original_row["front_run_id"] and original_row["back_run_id"] is None
    assert store.period_results(period)["students"][0]["total_score"] == 110
    reversal = store.reverse_posting(sheet, "复核发现录入错误", "reverse")
    assert reversal["reverses_batch_id"] == original["id"]
    assert store.period_results(period)["students"][0]["total_score"] == 100
    linked = store.connection.execute(
        "SELECT count(*) FROM ledger_entries WHERE batch_id=? AND source_entry_id IS NOT NULL", (reversal["id"],)
    ).fetchone()[0]
    assert linked == 1
    assert store.period_results(period)["ledger"][0]["run_id"] == original_row["front_run_id"]
    store.post_sheet(sheet, "repost")
    assert store.period_results(period)["students"][0]["total_score"] == 110


def test_close_requires_every_sheet_and_reopen_increments_next_report_version(tmp_path):
    store, project, period, first = prepared_period(tmp_path)
    unused = store.issue_sheet(period)
    complete_sheet(store, project, first, "close", ["slash_forward"])
    store.begin_settlement(period)
    store.post_sheet(first, "post-close")
    with pytest.raises(ValueError, match="仍有 1 张"):
        store.close_period(period)
    store.void_unused_sheet(unused)
    assert store.close_period(period) == 1
    adopted = store.connection.execute("SELECT id FROM recognition_runs WHERE sheet_id=? AND adopted=1", (first,)).fetchone()[0]
    with pytest.raises(ValueError, match="只读"):
        store.set_manual_observations(adopted, {"front-r00-c00-s0":"blank"})
    with pytest.raises(ValueError, match="只读"):
        store.add_scan_note(adopted, "关闭后不应追加")
    with pytest.raises(ValueError, match="待结算"):
        store.reverse_posting(first, "不能直接改", "closed-reverse")
    store.reopen_period(period, "发现明确录入错误")
    store.reverse_posting(first, "撤销后复核", "after-reopen")
    store.post_sheet(first, "corrected-post")
    assert store.close_period(period) == 2


def test_teacher_and_display_reports_are_readable_multipage_pdfs(tmp_path):
    store, project, period, sheet = prepared_period(tmp_path)
    complete_sheet(store, project, sheet, "report", ["slash_forward", "slash_back", "x"])
    store.begin_settlement(period)
    store.post_sheet(sheet, "post-report")
    store.close_period(period)
    data = store.period_results(period)
    teacher = tmp_path / "teacher.pdf"
    display = tmp_path / "display.pdf"
    generate_period_report(teacher, data, "teacher", False)
    generate_period_report(display, data, "display", False)
    teacher_doc, display_doc = pdfium.PdfDocument(teacher), pdfium.PdfDocument(display)
    assert len(teacher_doc) >= 3
    assert len(display_doc) == 2
    teacher_text = "".join(teacher_doc[i].get_textpage().get_text_bounded() for i in range(len(teacher_doc)))
    display_text = "".join(display_doc[i].get_textpage().get_text_bounded() for i in range(len(display_doc)))
    assert "纸表没有逐事件日期" in teacher_text
    assert "正面识别版本" in teacher_text
    assert "不展示个人扣分或倒数排名" in display_text
