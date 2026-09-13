from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch, mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .omr.template import FONT_NAME, FONT_PATH


DISPLAY_SIZE = (13.333 * inch, 7.5 * inch)


def _register_font() -> None:
    try:
        pdfmetrics.getFont(FONT_NAME)
    except KeyError:
        pdfmetrics.registerFont(TTFont(FONT_NAME, FONT_PATH))


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("zh-title", parent=base["Title"], fontName=FONT_NAME, fontSize=22, leading=28, textColor=colors.HexColor("#173d2a")),
        "h1": ParagraphStyle("zh-h1", parent=base["Heading1"], fontName=FONT_NAME, fontSize=15, leading=20, textColor=colors.HexColor("#245b3e"), spaceBefore=8, spaceAfter=7),
        "body": ParagraphStyle("zh-body", parent=base["BodyText"], fontName=FONT_NAME, fontSize=9, leading=14, textColor=colors.HexColor("#27322c")),
        "small": ParagraphStyle("zh-small", parent=base["BodyText"], fontName=FONT_NAME, fontSize=7.5, leading=11, textColor=colors.HexColor("#526058")),
        "center": ParagraphStyle("zh-center", parent=base["BodyText"], fontName=FONT_NAME, fontSize=9, leading=12, alignment=TA_CENTER),
    }


def _table(rows, widths=None, font_size=8, repeat_rows=1, padding=4):
    table = Table(rows, colWidths=widths, repeatRows=repeat_rows, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size + 3),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dfeee4")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#173d2a")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#aab9af")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f9f6")]),
        ("TOPPADDING", (0, 0), (-1, -1), padding),
        ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
    ]))
    return table


def _group_chart(groups: list[dict[str, object]], width: float = 650, height: float = 210) -> Drawing:
    drawing = Drawing(width, height)
    chart = HorizontalBarChart()
    chart.x, chart.y, chart.width, chart.height = 65, 25, width - 100, height - 45
    chart.data = [[float(group["average_score"]) for group in reversed(groups)]]
    chart.categoryAxis.categoryNames = [f"{group['group_number']}组" for group in reversed(groups)]
    chart.categoryAxis.labels.fontName = FONT_NAME
    chart.categoryAxis.labels.fontSize = 8
    chart.valueAxis.labels.fontName = FONT_NAME
    chart.valueAxis.labels.fontSize = 8
    chart.bars[0].fillColor = colors.HexColor("#3b8c61")
    chart.strokeColor = None
    drawing.add(chart)
    return drawing


def _page_callback(is_draft: bool, class_name: str, period_name: str):
    def draw(canvas, document):
        canvas.saveState()
        canvas.setFont(FONT_NAME, 7.5)
        canvas.setFillColor(colors.HexColor("#66736b"))
        canvas.drawString(document.leftMargin, 8 * mm, f"{class_name} · {period_name}")
        canvas.drawRightString(document.pagesize[0] - document.rightMargin, 8 * mm, f"第 {document.page} 页")
        if is_draft:
            canvas.setFillColor(colors.Color(0.75, 0.18, 0.18, alpha=0.14))
            canvas.setFont(FONT_NAME, 38)
            canvas.translate(document.pagesize[0] / 2, document.pagesize[1] / 2)
            canvas.rotate(28)
            canvas.drawCentredString(0, 0, "未结算草稿")
        canvas.restoreState()
    return draw


def _meta_story(data: dict[str, object], styles, is_draft: bool):
    period = data["period"]
    status = "未结算草稿" if is_draft else f"正式结果 · 版本 {period['result_version']}"
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return [
        Paragraph(f"{escape(str(period['class_name']))} · {escape(str(period['name']))}", styles["title"]),
        Paragraph(
            f"周期：{period['start_date']} 至 {period['expected_end_date']}　状态：{status}　生成时间：{generated}",
            styles["small"],
        ),
        Paragraph("统计口径：基础分加已入账纸表的有效单斜线分值；X、空白和已撤销流水不计分。纸表没有逐事件日期，本报告不推定事件发生日期。", styles["small"]),
        Spacer(1, 5 * mm),
    ]


def _teacher_story(data: dict[str, object], styles, is_draft: bool):
    story = _meta_story(data, styles, is_draft)
    story += [Paragraph("小组汇总", styles["h1"]), _group_chart(data["groups"])]
    group_rows = [["排名", "小组", "人数", "总分", "平均分"]] + [
        [group["rank"], f"{group['group_number']}组", group["member_count"], group["total_score"], f"{group['average_score']:.2f}"]
        for group in data["groups"]
    ]
    story += [_table(group_rows, [22 * mm, 28 * mm, 25 * mm, 30 * mm, 30 * mm]), Spacer(1, 4 * mm)]
    story += [Paragraph("个人总分", styles["h1"])]
    student_rows = [["学号", "姓名", "组别", "基础分", "加分", "扣分", "变动", "总分"]] + [
        [student["student_number"], student["name"], f"{student['group_number']}组", student["base_score"],
         student["positive_score"], student["negative_score"], student["delta"], student["total_score"]]
        for student in data["students"]
    ]
    story += [_table(student_rows, [20 * mm, 34 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm, 20 * mm], 7.5), PageBreak()]
    story += [Paragraph("各项目明细", styles["h1"])]
    rules = data["rules"]
    for start in range(0, len(rules), 6):
        if start:
            story.append(PageBreak())
        chunk = rules[start:start + 6]
        rows = [["学号", "姓名"] + [rule["name"] for rule in chunk]]
        for student in data["students"]:
            cells = []
            for rule in chunk:
                value = student["rules"].get(str(rule["rule_id"]), {"mark_count": 0, "amount": 0})
                cells.append(f"{value['mark_count']}次 / {value['amount']}分")
            rows.append([student["student_number"], student["name"], *cells])
        story += [Paragraph(f"项目 {start + 1}-{start + len(chunk)}", styles["small"]), _table(rows, [16 * mm, 28 * mm] + [31 * mm] * len(chunk), 6.8), Spacer(1, 5 * mm)]
    story += [PageBreak(), Paragraph("项目统计", styles["h1"])]
    rule_rows = [["面别", "项目", "单位分值", "有效次数", "合计分值"]] + [
        ["正面" if rule["side"] == "front" else "背面", rule["name"], rule["unit_score"], rule["mark_count"], rule["amount"]]
        for rule in rules
    ]
    story += [_table(rule_rows, [22 * mm, 45 * mm, 28 * mm, 28 * mm, 30 * mm]), Paragraph("纸表来源", styles["h1"])]
    source_rows = [["表号", "状态", "入账批次", "正面识别版本", "背面识别版本", "入账时间"]] + [
        [f"{sheet['sheet_number']:02d}", sheet["status"], (sheet["batch_id"] or "-")[:10],
         (sheet["front_run_id"] or "空白确认")[:10], (sheet["back_run_id"] or "空白确认")[:10], sheet["posted_at"] or "-"]
        for sheet in data["sheets"]
    ]
    story += [_table(source_rows, [18 * mm, 25 * mm, 35 * mm, 35 * mm, 35 * mm, 45 * mm], 7)]
    if data["notes"]:
        story += [Paragraph("已录入备注", styles["h1"])]
        note_rows = [["表号", "学号", "项目", "备注（录入时间不是事件日期）"]] + [
            [f"{note['sheet_number']:02d}", note["student_number"] or "-", note["rule_name"] or "-", Paragraph(escape(str(note["note_text"])), styles["small"])]
            for note in data["notes"]
        ]
        story += [_table(note_rows, [18 * mm, 22 * mm, 30 * mm, 120 * mm], 7)]
    return story


def _display_story(data: dict[str, object], styles, is_draft: bool):
    story = _meta_story(data, styles, is_draft)
    story += [Paragraph("小组排名", styles["h1"]), _group_chart(data["groups"], 820, 115)]
    rows = [["排名", "小组", "人数", "平均分"]] + [
        [group["rank"], f"{group['group_number']}组", group["member_count"], f"{group['average_score']:.2f}"]
        for group in data["groups"]
    ]
    story += [_table(rows, [32 * mm, 45 * mm, 40 * mm, 48 * mm], 10), PageBreak()]
    story += [Paragraph("正向表现", styles["h1"])]
    positive_rules = [rule for rule in data["rules"] if int(rule["unit_score"]) > 0]
    rule_rows = [["项目", "有效次数", "贡献分值"]] + [
        [rule["name"], rule["mark_count"], rule["amount"]] for rule in positive_rules
    ]
    positive_students = sorted(data["students"], key=lambda item: (-int(item["positive_score"]), int(item["row_index"])))
    student_rows = [["学号", "姓名", "小组", "正向得分"]] + [
        [student["student_number"], student["name"], f"{student['group_number']}组", student["positive_score"]]
        for student in positive_students if int(student["positive_score"]) > 0
    ][:10]
    if len(student_rows) == 1:
        student_rows.append(["-", "本周期暂无正向入账记录", "-", "0"])
    story += [_table(rule_rows, [60 * mm, 45 * mm, 45 * mm], 9, padding=2), Spacer(1, 4 * mm),
              Paragraph("正向表现记录（不展示个人扣分或倒数排名）", styles["h1"]),
              _table(student_rows, [35 * mm, 65 * mm, 40 * mm, 45 * mm], 9, padding=2)]
    return story


def generate_period_report(path: Path, data: dict[str, object], variant: str, is_draft: bool) -> None:
    if variant not in {"teacher", "display"}:
        raise ValueError("报告类型无效")
    _register_font()
    path.parent.mkdir(parents=True, exist_ok=True)
    page_size = landscape(A4) if variant == "teacher" else DISPLAY_SIZE
    document = SimpleDocTemplate(
        str(path), pagesize=page_size, leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"{data['period']['class_name']} {data['period']['name']} {'教师存档版' if variant == 'teacher' else '教室展示版'}",
        author="ScoreFlow",
    )
    styles = _styles()
    story = _teacher_story(data, styles, is_draft) if variant == "teacher" else _display_story(data, styles, is_draft)
    callback = _page_callback(is_draft, str(data["period"]["class_name"]), str(data["period"]["name"]))
    document.build(story, onFirstPage=callback, onLaterPages=callback)
