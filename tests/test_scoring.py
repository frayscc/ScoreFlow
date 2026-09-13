import pytest

from scoreflow.scoring import aggregate_observations, summarize_results


def snapshots():
    students = [
        {"student_id": "s1", "student_number": "1", "name": "甲", "group_number": 1, "row_index": 0},
        {"student_id": "s2", "student_number": "2", "name": "乙", "group_number": 1, "row_index": 1},
    ]
    rules = [
        {"rule_id": "r1", "name": "课堂+", "side": "front", "unit_score": 5, "sort_order": 0},
        {"rule_id": "r2", "name": "迟到-", "side": "back", "unit_score": -5, "sort_order": 0},
    ]
    return students, rules


def test_independent_scoring_engine_counts_slashes_and_ignores_x_and_gaps():
    students, rules = snapshots()
    entries = aggregate_observations(students, rules, {
        "front": [
            {"slot_id": "front-r00-c00-s0", "auto_class": "slash_forward", "manual_class": None},
            {"slot_id": "front-r00-c00-s1", "auto_class": "blank", "manual_class": None},
            {"slot_id": "front-r00-c00-s2", "auto_class": "x", "manual_class": None},
            {"slot_id": "front-r00-c00-s4", "auto_class": "slash_back", "manual_class": None},
        ],
        "back": [{"slot_id": "back-r01-c00-s3", "auto_class": "slash_forward", "manual_class": None}],
    })
    assert [(entry["student_id"], entry["mark_count"], entry["amount"]) for entry in entries] == [
        ("s1", 2, 10), ("s2", 1, -5)
    ]
    summary = summarize_results({"base_score": 100}, students, rules, entries)
    assert [student["total_score"] for student in summary["students"]] == [110, 95]


def test_scoring_engine_rejects_unresolved_observation():
    students, rules = snapshots()
    with pytest.raises(ValueError, match="待复核"):
        aggregate_observations(students, rules, {
            "front": [{"slot_id": "front-r00-c00-s0", "auto_class": "review", "manual_class": None}]
        })
