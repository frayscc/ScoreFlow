import pytest

from scoreflow.db import Store
from scoreflow.roster import parse_roster_text


def test_mixed_roster_text_normalizes_numbers_and_preserves_names():
    text = """学号 姓名
1 张三
０２ 李四
25号 王五
26，赵六
27,钱七
28、孙八
29\t阿依努尔·买买提
"""
    result = parse_roster_text(text)
    assert result.valid
    assert [row["student_number"] for row in result.rows] == ["1", "2", "25", "26", "27", "28", "29"]
    assert result.rows[-1]["name"] == "阿依努尔·买买提"
    assert result.skipped_headers == [1]


def test_roster_text_reports_duplicate_missing_and_allows_duplicate_names():
    duplicate = parse_roster_text("01 张三\n1 李四\n2\n")
    assert not duplicate.valid
    assert any("重复" in error["message"] and error["line"] == 2 for error in duplicate.errors)
    assert any(error["line"] == 3 and error["message"] == "缺少姓名" for error in duplicate.errors)
    same_name = parse_roster_text("1 张三\n2 张三")
    assert same_name.valid and any("允许重名" in warning for warning in same_name.warnings)


def test_incomplete_reimport_preserves_existing_students_names_and_groups(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    project = store.create_project("测试班", "2026", 2)
    store.import_students(project, [
        {"student_number": 1, "name": "张三", "group_number": 1, "is_leader": True},
        {"student_number": 2, "name": "李四", "group_number": 2, "is_leader": True},
    ])
    parsed = parse_roster_text("01 张三改名\n3 王五")
    preview = store.roster_import_preview(project, parsed.rows)
    assert [row["status"] for row in preview] == ["name_difference", "new"]
    result = store.import_roster_students(project, parsed.rows)
    assert result == {"inserted": 1, "unchanged": 0, "name_differences": 1}
    students = {row["student_number"]: row for row in store.list_students(project)}
    assert set(students) == {"1", "2", "3"}
    assert students["1"]["name"] == "张三" and students["1"]["group_number"] == 1
    assert students["2"]["group_number"] == 2
    assert students["3"]["group_number"] is None
    assert store.import_roster_students(project, parsed.rows)["inserted"] == 0


def _new_49_student_draft(path):
    store = Store(path)
    project = store.create_project("二四一五班", "2026", 7)
    text = "\n".join(f"{number} 学生{number}" for number in range(1, 50))
    parsed = parse_roster_text(text)
    store.import_roster_students(project, parsed.rows)
    period = store.create_period(project, "第1周", "2026-09-01", "2026-09-07")
    return store, project, period


def _complete_grouping(draft):
    members = []
    leaders = {}
    for index, member in enumerate(draft["members"]):
        group = index // 7 + 1
        members.append({"student_id": member["student_id"], "group_number": group})
        if index % 7 == 0:
            leaders[str(group)] = member["student_id"]
    return members, leaders


def test_grouping_draft_persists_rejects_stale_save_and_freezes_history(tmp_path):
    path = tmp_path / "db.sqlite"
    store, project, period = _new_49_student_draft(path)
    initial = store.grouping_draft(period)
    assert sum(member["group_number"] is None for member in initial["members"]) == 49
    members, leaders = _complete_grouping(initial)
    revision = store.save_grouping_draft(period, initial["revision"], members, leaders)
    with pytest.raises(ValueError, match="刷新"):
        store.save_grouping_draft(period, initial["revision"], members, leaders)
    store.close()

    reopened = Store(path)
    saved = reopened.grouping_draft(period)
    assert saved["revision"] == revision
    assert [sum(member["group_number"] == group for member in saved["members"]) for group in range(1, 8)] == [7] * 7
    sheet = reopened.start_period(period)
    assert sheet
    first_student = saved["members"][0]["student_id"]
    assert reopened.connection.execute(
        "SELECT group_number FROM period_students WHERE period_id=? AND student_id=?", (period, first_student)
    ).fetchone()[0] == 1

    next_period = reopened.create_period(project, "第2周", "2026-09-08", "2026-09-14")
    next_draft = reopened.grouping_draft(next_period)
    changed = [{"student_id": row["student_id"], "group_number": (2 if row["student_id"] == first_student else row["group_number"])} for row in next_draft["members"]]
    next_leaders = dict(next_draft["leaders"])
    if next_leaders.get("1") == first_student:
        next_leaders["1"] = next(row["student_id"] for row in next_draft["members"] if row["group_number"] == 1 and row["student_id"] != first_student)
    reopened.save_grouping_draft(next_period, next_draft["revision"], changed, next_leaders)
    assert reopened.connection.execute(
        "SELECT group_number FROM period_students WHERE period_id=? AND student_id=?", (period, first_student)
    ).fetchone()[0] == 1
    with pytest.raises(ValueError, match="恰好7人"):
        reopened.start_period(next_period)


def test_invalid_leader_save_rolls_back_and_incomplete_draft_cannot_start(tmp_path):
    store, _, period = _new_49_student_draft(tmp_path / "db.sqlite")
    draft = store.grouping_draft(period)
    members, leaders = _complete_grouping(draft)
    leaders["1"] = members[7]["student_id"]
    with pytest.raises(ValueError, match="本组"):
        store.save_grouping_draft(period, draft["revision"], members, leaders)
    assert store.grouping_draft(period)["revision"] == draft["revision"]
    with pytest.raises(ValueError, match="未分组"):
        store.start_period(period)
