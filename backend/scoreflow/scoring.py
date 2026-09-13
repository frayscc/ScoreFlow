from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable


SLOT_ID = re.compile(r"^(front|back)-r(\d+)-c(\d+)-s(\d+)$")
VALID_CLASSES = {"blank", "slash_forward", "slash_back", "x", "review"}


def aggregate_observations(
    students: list[dict[str, object]],
    rules: list[dict[str, object]],
    observations_by_side: dict[str, Iterable[dict[str, object]]],
) -> list[dict[str, object]]:
    """Convert adopted slot observations into auditable student/rule entries.

    This function is deliberately independent from SQLite, HTTP and OMR. It only
    accepts frozen period snapshots and adopted observations.
    """
    student_by_row = {int(student["row_index"]): student for student in students}
    rule_by_position = {(str(rule["side"]), int(rule["sort_order"])): rule for rule in rules}
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    seen_slots: set[str] = set()

    for expected_side, observations in observations_by_side.items():
        if expected_side not in {"front", "back"}:
            raise ValueError("识别面别无效")
        for observation in observations:
            slot_id = str(observation["slot_id"])
            if slot_id in seen_slots:
                raise ValueError(f"重复槽位来源：{slot_id}")
            seen_slots.add(slot_id)
            match = SLOT_ID.fullmatch(slot_id)
            if not match or match.group(1) != expected_side:
                raise ValueError(f"槽位身份无效：{slot_id}")
            value = str(observation.get("manual_class") or observation["auto_class"])
            if value not in VALID_CLASSES:
                raise ValueError(f"未知槽位判定：{value}")
            if value == "review":
                raise ValueError("仍有槽位待复核")
            if value in {"blank", "x"}:
                continue
            student = student_by_row.get(int(match.group(2)))
            rule = rule_by_position.get((expected_side, int(match.group(3))))
            if not student or not rule:
                raise ValueError(f"槽位超出周期快照：{slot_id}")
            key = (str(student["student_id"]), str(rule["rule_id"]))
            item = grouped.setdefault(
                key,
                {
                    "student_id": key[0],
                    "rule_id": key[1],
                    "mark_count": 0,
                    "unit_score": int(rule["unit_score"]),
                    "slot_ids": [],
                },
            )
            item["mark_count"] = int(item["mark_count"]) + 1
            item["slot_ids"].append(slot_id)

    entries = []
    for item in grouped.values():
        item["slot_ids"].sort()
        item["amount"] = int(item["mark_count"]) * int(item["unit_score"])
        entries.append(item)
    return sorted(entries, key=lambda item: (str(item["student_id"]), str(item["rule_id"])))


def summarize_results(
    period: dict[str, object],
    students: list[dict[str, object]],
    rules: list[dict[str, object]],
    effective_entries: Iterable[dict[str, object]],
) -> dict[str, object]:
    base_score = int(period["base_score"])
    student_rows = {
        str(student["student_id"]): {
            **student,
            "base_score": base_score,
            "positive_score": 0,
            "negative_score": 0,
            "delta": 0,
            "total_score": base_score,
            "rules": {},
        }
        for student in students
    }
    rule_rows = {
        str(rule["rule_id"]): {**rule, "mark_count": 0, "amount": 0}
        for rule in rules
    }
    for entry in effective_entries:
        student = student_rows[str(entry["student_id"])]
        rule = rule_rows[str(entry["rule_id"])]
        amount, count = int(entry["amount"]), int(entry["mark_count"])
        student["delta"] += amount
        student["total_score"] += amount
        if amount >= 0:
            student["positive_score"] += amount
        else:
            student["negative_score"] += amount
        current = student["rules"].setdefault(str(entry["rule_id"]), {"mark_count": 0, "amount": 0})
        current["mark_count"] += count
        current["amount"] += amount
        rule["mark_count"] += count
        rule["amount"] += amount

    ordered_students = sorted(student_rows.values(), key=lambda item: int(item["row_index"]))
    groups: dict[int, list[dict[str, object]]] = defaultdict(list)
    for student in ordered_students:
        groups[int(student["group_number"])].append(student)
    group_rows = []
    for group_number, members in groups.items():
        total = sum(int(member["total_score"]) for member in members)
        group_rows.append({
            "group_number": group_number,
            "member_count": len(members),
            "total_score": total,
            "average_score": round(total / len(members), 2),
        })
    group_rows.sort(key=lambda item: (-float(item["average_score"]), int(item["group_number"])))
    for rank, group in enumerate(group_rows, start=1):
        group["rank"] = rank
    return {
        "period": period,
        "students": ordered_students,
        "groups": group_rows,
        "rules": [rule_rows[str(rule["rule_id"])] for rule in rules],
    }
