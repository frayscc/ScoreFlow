from __future__ import annotations

import io
import json
import base64
import uuid
from pathlib import Path
from typing import Any, Optional

import qrcode
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.utils import ImageReader

from .layout import Layout, build_manifest
from ..config import resource_root


FONT_NAME = "NotoSansSC"
FONT_PATH = resource_root() / "assets" / "fonts" / "NotoSansSC-Regular.ttf"


def _register_font() -> None:
    try:
        pdfmetrics.getFont(FONT_NAME)
    except KeyError:
        if not FONT_PATH.exists():
            raise RuntimeError(f"缺少随包中文字体：{FONT_PATH}")
        pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))


def _qr_image(payload: dict[str, Any]) -> ImageReader:
    code = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=5, border=4)
    def compact_id(value: str) -> str:
        try: return base64.urlsafe_b64encode(uuid.UUID(value).bytes).decode("ascii").rstrip("=")
        except ValueError: return value
    compact = "|".join(("SF1", compact_id(payload["project_id"]), compact_id(payload["period_id"]),
                        compact_id(payload["sheet_id"]), "F" if payload["side"] == "front" else "B",
                        payload["template_id"], payload["layout_hash"]))
    code.add_data(compact)
    code.make(fit=True)
    image = code.make_image(fill_color="black", back_color="white")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    return ImageReader(stream)


def _draw_centered(canvas: Canvas, text: str, x: float, y: float, size: float) -> None:
    canvas.setFont(FONT_NAME, size)
    canvas.drawCentredString(x * mm, y * mm, text)


def _draw_side(canvas: Canvas, manifest: dict[str, Any], side_name: str, class_name: str, period_name: str) -> None:
    layout = Layout()
    side = manifest["sides"][side_name]
    students = manifest["students"]
    rules = side["rules"]
    canvas.setLineWidth(0.25)
    for marker in side["markers"]:
        canvas.rect(marker["x_mm"] * mm, marker["y_mm"] * mm, marker["width_mm"] * mm, marker["height_mm"] * mm, fill=1, stroke=0)

    canvas.setFont(FONT_NAME, 12)
    canvas.drawString(14 * mm, 279 * mm, f"{class_name}　{period_name}　积分登记纸表{manifest['sheet_number']:02d}")
    canvas.setFont(FONT_NAME, 7.5)
    canvas.drawString(14 * mm, 273.8 * mm, f"{side['label']}｜演示名单｜模板 {manifest['template_id']}｜短编号 {manifest['sheet_id'][-8:]}")
    canvas.drawString(14 * mm, 269.6 * mm, "每格：单斜线=1次；X=作废；可跳格。请勿擦改，疑难笔迹由教师复核。")
    canvas.drawImage(_qr_image(side["qr_payload"]), 184 * mm, 267 * mm, 15 * mm, 15 * mm, preserveAspectRatio=True, mask="auto")

    left, right = layout.table_left_mm, layout.table_right_mm
    rows_top = layout.rows_top_mm
    rows_bottom = rows_top - len(students) * layout.row_height_mm
    header_top = rows_top + layout.header_height_mm
    canvas.rect(left * mm, rows_bottom * mm, (right - left) * mm, (header_top - rows_bottom) * mm, fill=0, stroke=1)
    info_edges = [left, left + layout.group_width_mm, left + layout.group_width_mm + layout.number_width_mm, layout.rule_left_mm]
    for x in info_edges[1:]:
        canvas.line(x * mm, rows_bottom * mm, x * mm, header_top * mm)
    rule_width = (right - layout.rule_left_mm) / len(rules)
    for index in range(1, len(rules)):
        x = layout.rule_left_mm + index * rule_width
        canvas.line(x * mm, rows_bottom * mm, x * mm, header_top * mm)
    canvas.line(left * mm, rows_top * mm, right * mm, rows_top * mm)
    for row in range(1, len(students)):
        y = rows_top - row * layout.row_height_mm
        if students[row - 1]["group_number"] != students[row]["group_number"]:
            # Group boundaries cross the merged group column and are heavier,
            # so a recorder can find the next group at a glance.
            canvas.setLineWidth(0.7)
            canvas.line(left * mm, y * mm, right * mm, y * mm)
            canvas.setLineWidth(0.25)
        else:
            canvas.line((left + layout.group_width_mm) * mm, y * mm, right * mm, y * mm)

    header_y = rows_top + 3.0
    _draw_centered(canvas, "小组", left + layout.group_width_mm / 2, header_y, 7.2)
    _draw_centered(canvas, "学号", left + layout.group_width_mm + layout.number_width_mm / 2, header_y, 7.2)
    _draw_centered(canvas, "姓名", left + layout.group_width_mm + layout.number_width_mm + layout.name_width_mm / 2, header_y, 7.2)
    for i, rule in enumerate(rules):
        _draw_centered(canvas, rule["name"], layout.rule_left_mm + (i + 0.5) * rule_width, header_y, 6.8)

    for row_index, student in enumerate(students):
        row_y = rows_top - (row_index + 1) * layout.row_height_mm
        text_y = row_y + 1.35
        _draw_centered(canvas, str(student["student_number"]), left + layout.group_width_mm + layout.number_width_mm / 2, text_y, 7.2)
        _draw_centered(canvas, student["name"] + ("*" if student["is_leader"] else ""), left + layout.group_width_mm + layout.number_width_mm + layout.name_width_mm / 2, text_y, 7.2)

    run_start = 0
    while run_start < len(students):
        group_number = students[run_start]["group_number"]
        run_end = run_start + 1
        while run_end < len(students) and students[run_end]["group_number"] == group_number:
            run_end += 1
        run_top = rows_top - run_start * layout.row_height_mm
        run_bottom = rows_top - run_end * layout.row_height_mm
        _draw_centered(canvas, f"{group_number}组", left + layout.group_width_mm / 2, (run_top + run_bottom) / 2 - 0.9, 7.5)
        run_start = run_end

    for slot in side["slots"]:
        roi = slot["roi"]
        canvas.rect(roi["x_mm"] * mm, roi["y_mm"] * mm, roi["width_mm"] * mm, roi["height_mm"] * mm, fill=0, stroke=1)

    note = side["note_roi"]
    canvas.rect(note["x_mm"] * mm, note["y_mm"] * mm, note["width_mm"] * mm, note["height_mm"] * mm, fill=0, stroke=1)
    canvas.setFont(FONT_NAME, 6.8)
    canvas.drawString((note["x_mm"] + 1.5) * mm, (note["y_mm"] + note["height_mm"] - 3.2) * mm, "其他事项备注（学号：原因；不进入自动识别槽位）")
    canvas.setFont(FONT_NAME, 5.5)
    canvas.drawRightString(199 * mm, 4.5 * mm, f"{manifest['layout_hash'][:16]} · {side['label']}")


def generate_template(output_pdf: Path, output_manifest: Path, *, project_id: str, period_id: str, sheet_id: str,
                      sheet_number: int = 1, class_name: str = "七年级演示班", period_name: str = "演示周期",
                      students: Optional[list[dict[str, Any]]] = None,
                      front_rules: Optional[list[str]] = None, back_rules: Optional[list[str]] = None) -> dict[str, Any]:
    _register_font()
    manifest = build_manifest(project_id=project_id, period_id=period_id, sheet_id=sheet_id, sheet_number=sheet_number,
                              students=students, front_rules=front_rules, back_rules=back_rules)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(output_pdf), pagesize=A4, pageCompression=1)
    for side_name in ("front", "back"):
        _draw_side(canvas, manifest, side_name, class_name, period_name)
        canvas.showPage()
    canvas.save()
    output_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
