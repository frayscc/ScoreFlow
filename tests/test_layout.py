from scoreflow.omr.layout import Layout, build_manifest


def manifest():
    return build_manifest(project_id="p", period_id="period", sheet_id="sheet")


def test_slot_counts_and_student_rows():
    data = manifest()
    assert len(data["students"]) == 49
    assert len(data["sides"]["front"]["slots"]) == 49 * 7 * 5 == 1715
    assert len(data["sides"]["back"]["slots"]) == 49 * 6 * 5 == 1470
    assert {student["group_number"] for student in data["students"]} == set(range(1, 8))


def test_slots_are_inside_page_and_do_not_overlap_with_notes():
    data = manifest()
    page = data["page"]
    for side in data["sides"].values():
        note_top = side["note_roi"]["y_mm"] + side["note_roi"]["height_mm"]
        for slot in side["slots"]:
            roi = slot["roi"]
            assert 0 <= roi["x_mm"] < roi["x_mm"] + roi["width_mm"] <= page["width_mm"]
            assert 0 <= roi["y_mm"] < roi["y_mm"] + roi["height_mm"] <= page["height_mm"]
            assert roi["y_mm"] >= note_top


def test_each_row_rule_has_five_distinct_slots():
    data = manifest()
    for side in data["sides"].values():
        groups = {}
        for slot in side["slots"]:
            key = (slot["row_index"], slot["rule_index"])
            groups.setdefault(key, []).append(slot)
        assert all(len(slots) == 5 for slots in groups.values())
        for slots in groups.values():
            ordered = sorted(slots, key=lambda item: item["roi"]["x_mm"])
            for left, right in zip(ordered, ordered[1:]):
                assert left["roi"]["x_mm"] + left["roi"]["width_mm"] <= right["roi"]["x_mm"]


def test_vertical_budget_fits_safe_area():
    layout = Layout()
    used = layout.note_height_mm + 49 * layout.row_height_mm + layout.header_height_mm + layout.identity_height_mm
    assert used == 275.2
    assert used <= layout.page_height_mm - 2 * layout.margin_mm


def test_qr_identity_distinguishes_side_and_keeps_sheet_identity():
    data = manifest()
    front = data["sides"]["front"]["qr_payload"]
    back = data["sides"]["back"]["qr_payload"]
    assert front["sheet_id"] == back["sheet_id"] == "sheet"
    assert front["side"] == "front"
    assert back["side"] == "back"
    assert front["layout_hash"] == back["layout_hash"]


def test_uneven_eight_groups_are_supported_up_to_49_students():
    students = []
    number = 1
    for group, size in enumerate([5, 7, 4, 8, 6, 5, 7, 7], start=1):
        for member in range(size):
            students.append({
                "student_id": f"s{number}", "student_number": number, "name": f"学生{number}",
                "group_number": group, "is_leader": member == 0,
            })
            number += 1
    data = build_manifest(project_id="p", period_id="period", sheet_id="sheet", students=students)
    assert data["row_count"] == 49
    assert {student["group_number"] for student in data["students"]} == set(range(1, 9))


def test_more_than_49_students_is_an_explicit_capacity_error():
    students = [{"student_id": f"s{i}", "student_number": i, "name": str(i), "group_number": 1, "is_leader": i == 1} for i in range(1, 51)]
    import pytest
    with pytest.raises(ValueError, match="1—49"):
        build_manifest(project_id="p", period_id="period", sheet_id="sheet", students=students)


def test_sheet_identity_does_not_change_reusable_layout_version():
    first = build_manifest(project_id="p1", period_id="one", sheet_id="sheet-1")
    second = build_manifest(project_id="p2", period_id="two", sheet_id="sheet-2")
    assert first["layout_hash"] == second["layout_hash"]
    assert first["sides"]["front"]["qr_payload"]["sheet_id"] != second["sides"]["front"]["qr_payload"]["sheet_id"]
