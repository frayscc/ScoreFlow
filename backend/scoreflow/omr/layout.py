from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional


FRONT_RULES = ["作业＋", "课堂＋", "成绩＋", "服务＋", "值日＋", "活动＋", "其他＋"]
BACK_RULES = ["作业－", "课堂－", "值日－", "迟到－", "晚归－", "其他－"]


@dataclass(frozen=True)
class Layout:
    page_width_mm: float = 210.0
    page_height_mm: float = 297.0
    margin_mm: float = 7.0
    note_height_mm: float = 15.0
    row_height_mm: float = 4.8
    header_height_mm: float = 9.0
    identity_height_mm: float = 16.0
    group_width_mm: float = 8.0
    number_width_mm: float = 9.0
    name_width_mm: float = 23.0
    slot_size_mm: float = 3.5
    slot_gap_mm: float = 0.5
    marker_size_mm: float = 3.0

    @property
    def rows_bottom_mm(self) -> float:
        return self.margin_mm + self.note_height_mm

    @property
    def rows_top_mm(self) -> float:
        return self.rows_bottom_mm + 49 * self.row_height_mm

    @property
    def table_left_mm(self) -> float:
        return self.margin_mm

    @property
    def table_right_mm(self) -> float:
        return self.page_width_mm - self.margin_mm

    @property
    def rule_left_mm(self) -> float:
        return self.table_left_mm + self.group_width_mm + self.number_width_mm + self.name_width_mm


def demo_students() -> list[dict[str, Any]]:
    """Clearly synthetic 49-person list used only for engineering verification."""
    surnames = ["赵", "钱", "孙", "李", "周", "吴", "郑"]
    given = ["晨", "宇", "宁", "可", "安", "思远", "嘉禾"]
    students: list[dict[str, Any]] = []
    for index in range(49):
        group = index // 7 + 1
        students.append({
            "student_id": f"demo-student-{index + 1:02d}",
            "student_number": index + 1,
            "name": surnames[group - 1] + given[index % 7],
            "group_number": group,
            "is_leader": index % 7 == 0,
            "row_index": index,
            "demo": True,
        })
    return students


def _rect(x: float, y: float, width: float, height: float) -> dict[str, float]:
    return {"x_mm": round(x, 4), "y_mm": round(y, 4), "width_mm": round(width, 4), "height_mm": round(height, 4)}


def build_manifest(*, project_id: str, period_id: str, sheet_id: str, sheet_number: int = 1,
                   students: Optional[list[dict[str, Any]]] = None,
                   front_rules: Optional[list[str]] = None, back_rules: Optional[list[str]] = None) -> dict[str, Any]:
    layout = Layout()
    students = [dict(student) for student in (students or demo_students())]
    if not 1 <= len(students) <= 49:
        raise ValueError("当前 A4 模板支持 1—49 名学生")
    for row_index, student in enumerate(students):
        student["row_index"] = row_index
    front_rules = front_rules or FRONT_RULES
    back_rules = back_rules or BACK_RULES
    if not 1 <= len(front_rules) <= 7 or not 1 <= len(back_rules) <= 6:
        raise ValueError("当前模板正面最多 7 个项目、背面最多 6 个项目")
    sides: dict[str, Any] = {}
    for side_name, label, rules in (("front", "正面", front_rules), ("back", "背面", back_rules)):
        rule_width = (layout.table_right_mm - layout.rule_left_mm) / len(rules)
        slots: list[dict[str, Any]] = []
        for row_index, student in enumerate(students):
            row_y = layout.rows_top_mm - (row_index + 1) * layout.row_height_mm
            for rule_index, rule_name in enumerate(rules):
                rule_x = layout.rule_left_mm + rule_index * rule_width
                slots_width = 5 * layout.slot_size_mm + 4 * layout.slot_gap_mm
                slot_start = rule_x + (rule_width - slots_width) / 2
                for slot_index in range(5):
                    slot_x = slot_start + slot_index * (layout.slot_size_mm + layout.slot_gap_mm)
                    slot_y = row_y + (layout.row_height_mm - layout.slot_size_mm) / 2
                    slots.append({
                        "slot_id": f"{side_name}-r{row_index:02d}-c{rule_index:02d}-s{slot_index}",
                        "student_id": student["student_id"],
                        "student_number": student["student_number"],
                        "row_index": row_index,
                        "rule_index": rule_index,
                        "rule_name": rule_name,
                        "slot_index": slot_index,
                        "roi": _rect(slot_x, slot_y, layout.slot_size_mm, layout.slot_size_mm),
                    })
        marker_inset = layout.margin_mm + 0.5
        marker_far_x = layout.page_width_mm - marker_inset - layout.marker_size_mm
        marker_far_y = layout.page_height_mm - marker_inset - layout.marker_size_mm
        sides[side_name] = {
            "label": label,
            "rules": [{"index": i, "name": name, "unit_score": 5 if side_name == "front" else -5} for i, name in enumerate(rules)],
            "qr_payload": {
                "format": "scoreflow-paper-v1", "project_id": project_id, "period_id": period_id,
                "sheet_id": sheet_id, "side": side_name, "template_id": "a4-49-v1",
            },
            "markers": [
                _rect(marker_inset, marker_inset, layout.marker_size_mm, layout.marker_size_mm),
                _rect(marker_far_x, marker_inset, layout.marker_size_mm, layout.marker_size_mm),
                _rect(marker_inset, marker_far_y, layout.marker_size_mm, layout.marker_size_mm),
                _rect(marker_far_x, marker_far_y, layout.marker_size_mm, layout.marker_size_mm),
            ],
            "note_roi": _rect(14.0, layout.margin_mm + 1.0, 174.0, layout.note_height_mm - 2.0),
            "slots": slots,
        }
    manifest: dict[str, Any] = {
        "format_version": 1,
        "template_id": "a4-49-v1",
        "template_version": 1,
        "project_id": project_id,
        "period_id": period_id,
        "sheet_id": sheet_id,
        "sheet_number": sheet_number,
        "row_count": len(students),
        "page": {"width_mm": layout.page_width_mm, "height_mm": layout.page_height_mm},
        "geometry": layout.__dict__,
        "students": students,
        "sides": sides,
    }
    # The layout hash identifies reusable printed geometry, not a particular
    # project/sheet identity. Supplemental sheets with the same layout retain
    # the same template version while their QR payloads remain distinct.
    layout_basis = {
        "format_version": manifest["format_version"], "template_id": manifest["template_id"],
        "template_version": manifest["template_version"], "page": manifest["page"],
        "geometry": manifest["geometry"], "row_count": manifest["row_count"],
        "sides": {name: {
            "rules": side["rules"], "markers": side["markers"], "note_roi": side["note_roi"],
            "slots": [{"row_index": slot["row_index"], "rule_index": slot["rule_index"], "slot_index": slot["slot_index"], "roi": slot["roi"]} for slot in side["slots"]],
        } for name, side in sides.items()},
    }
    canonical = json.dumps(layout_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest["layout_hash"] = hashlib.sha256(canonical).hexdigest()
    for side in sides.values():
        side["qr_payload"]["layout_hash"] = manifest["layout_hash"][:16]
    return manifest
