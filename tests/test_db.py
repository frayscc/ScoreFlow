import sqlite3

import pytest

from scoreflow.db import Store, parse_student_csv


def roster():
    return [{"student_number": i + 1, "name": f"演示学生{i + 1}", "group_number": i // 7 + 1, "is_leader": i % 7 == 0} for i in range(49)]


def test_import_is_atomic_on_duplicate(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("测试班", "2026")
    rows = roster()
    rows[-1]["student_number"] = 1
    with pytest.raises(ValueError, match="重复"):
        store.import_students(project, rows)
    assert store.connection.execute("SELECT count(*) FROM students").fetchone()[0] == 0


def test_start_freezes_snapshot_and_issues_distinct_sheets(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("测试班", "2026")
    ids = store.import_students(project, roster())
    period = store.create_period(project, "第一周期", "2026-09-01", "2026-09-14")
    sheet1 = store.start_period(period)
    with store.connection:
        store.connection.execute("UPDATE students SET name='改名后' WHERE id=?", (ids[0],))
    snapshot = store.connection.execute("SELECT name FROM period_students WHERE period_id=? AND student_id=?", (period, ids[0])).fetchone()[0]
    sheet2 = store.issue_sheet(period)
    assert snapshot == "演示学生1"
    assert sheet1 != sheet2
    assert [row[0] for row in store.connection.execute("SELECT sheet_number FROM paper_sheets ORDER BY sheet_number")] == [1, 2]
    sheets = store.list_sheets(period)
    assert sheets[1]["front_status"] == sheets[1]["back_status"] == "missing"
    store.void_unused_sheet(sheet2)
    assert store.list_sheets(period)[1]["status"] == "void_unused"


def test_start_rejects_leader_from_wrong_group_without_partial_snapshot(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("测试班", "2026")
    ids = store.import_students(project, roster())
    with store.connection:
        store.connection.execute("UPDATE class_groups SET leader_student_id=? WHERE project_id=? AND group_number=2", (ids[0], project))
    period = store.create_period(project, "第一周期", "2026-09-01", "2026-09-14")
    with pytest.raises(ValueError, match="本组"):
        store.start_period(period)
    assert store.connection.execute("SELECT count(*) FROM period_students").fetchone()[0] == 0
    assert store.connection.execute("SELECT status FROM periods WHERE id=?", (period,)).fetchone()[0] == "draft"


def test_csv_accepts_utf8_bom_and_chinese_headers():
    parsed = parse_student_csv("\ufeff学号,姓名,组号,是否组长\n1,张三,1,是\n2,李四,1,否\n")
    assert parsed[0]["is_leader"] is True
    assert parsed[1]["is_leader"] is False


def test_project_can_define_eight_uneven_groups(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("八组班", "2026", group_count=8)
    rows = [{"student_number": i, "name": f"学生{i}", "group_number": 8 if i > 42 else (i - 1) // 6 + 1, "is_leader": i in {1, 7, 13, 19, 25, 31, 37, 43}} for i in range(1, 50)]
    store.import_students(project, rows)
    assert len(store.list_students(project)) == 49
    assert store.list_projects()[0]["group_count"] == 8


def test_next_period_edits_do_not_change_frozen_snapshot(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("测试班", "2026")
    ids = store.import_students(project, roster())
    period = store.create_period(project, "第1周", "2026-09-01", "2026-09-07")
    sheet = store.start_period(period)
    store.update_student(project, ids[0], name="下一周期姓名", group_number=2, is_leader=False)
    new_rules = [{"name":"新加分","side":"front","unit_score":10},{"name":"新扣分","side":"back","unit_score":-10}]
    store.replace_rules(project, new_rules)
    store.replace_rules(project, new_rules)
    frozen_name = store.connection.execute("SELECT name FROM period_students WHERE period_id=? AND student_id=?", (period, ids[0])).fetchone()[0]
    frozen_rule_count = store.connection.execute("SELECT count(*) FROM period_rules WHERE period_id=?", (period,)).fetchone()[0]
    assert frozen_name == "演示学生1"
    assert frozen_rule_count == 13
    assert len(store.list_rules(project)) == 17  # 13 historical + 2 prior + 2 current definitions
    data = store.paper_generation_data(period, sheet)
    store.record_template_version(sheet, {"layout_hash":"hash1","template_id":"a4-49-v1","page":{},"geometry":{},"row_count":49}, "font-hash")
    assert data["students"][0]["name"] == "演示学生1"
    assert store.connection.execute("SELECT template_id FROM paper_sheets WHERE id=?", (sheet,)).fetchone()[0] == "hash1"


def test_period_dates_can_only_change_while_draft(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("测试班", "2026")
    store.import_students(project, roster())
    period = store.create_period(project, "第1周", "2026-09-01", "2026-09-07")
    store.update_draft_period(period, "第1—2周", "2026-09-01", "2026-09-14")
    assert store.list_periods(project)[0]["name"] == "第1—2周"
    store.start_period(period)
    with pytest.raises(ValueError, match="草稿"):
        store.update_draft_period(period, "不可修改", "2026-09-01", "2026-09-20")


def test_schema_v6_upgrades_to_phase4_ledger_tables(tmp_path):
    path = tmp_path / "db.sqlite"
    store = Store(path)
    with store.connection:
        store.connection.execute("DROP TABLE report_exports")
        store.connection.execute("DROP TABLE ledger_entries")
        store.connection.execute("DROP TABLE posting_batches")
        store.connection.execute("UPDATE schema_version SET version=6")
        store.connection.execute("PRAGMA user_version=6")
    store.close()
    upgraded = Store(path)
    assert upgraded.connection.execute("PRAGMA user_version").fetchone()[0] == 8
    tables = {row[0] for row in upgraded.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"posting_batches", "ledger_entries", "report_exports"}.issubset(tables)
    columns = {row[1] for row in upgraded.connection.execute("PRAGMA table_info(posting_batches)")}
    assert {"front_run_id", "back_run_id"}.issubset(columns)
