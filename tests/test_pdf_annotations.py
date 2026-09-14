import hashlib
import io

import cv2
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject
from reportlab.lib.units import mm
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from scoreflow.omr.recognize import _roi_pixels, classify_slot
from scoreflow.omr.scan import pages_from_file
from scoreflow.omr.template import generate_template


def _ink_annotation(paths):
    points = []
    all_x, all_y = [], []
    for path in paths:
        converted = ArrayObject()
        for x, y in path:
            converted.extend((FloatObject(x * mm), FloatObject(y * mm)))
            all_x.append(x * mm); all_y.append(y * mm)
        points.append(converted)
    return DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Ink"),
        NameObject("/Rect"): ArrayObject([FloatObject(min(all_x) - 2), FloatObject(min(all_y) - 2), FloatObject(max(all_x) + 2), FloatObject(max(all_y) + 2)]),
        NameObject("/InkList"): ArrayObject(points), NameObject("/C"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(0)]),
        NameObject("/Border"): ArrayObject([NumberObject(0), NumberObject(0), FloatObject(1.8)]), NameObject("/F"): NumberObject(4),
    })


def _slot_paths(slot, kind):
    roi = slot["roi"]; x, y, w, h = roi["x_mm"], roi["y_mm"], roi["width_mm"], roi["height_mm"]
    forward = [(x + .55, y + .55), (x + w - .55, y + h - .55)]
    backward = [(x + .55, y + h - .55), (x + w - .55, y + .55)]
    return [forward, backward] if kind == "x" else [forward if kind == "slash_forward" else backward]


def _annotated_paper(tmp_path, marks, name="annotated.pdf"):
    base = tmp_path / f"base-{name}"; manifest_path = tmp_path / f"{name}.json"
    manifest = generate_template(base, manifest_path, project_id="p", period_id="period", sheet_id="sheet", class_name="电子笔迹测试班", period_name="第1周")
    reader = PdfReader(base); writer = PdfWriter(); writer.clone_document_from_reader(reader)
    slots = manifest["sides"]["front"]["slots"]
    for index, kind in marks:
        writer.add_annotation(0, _ink_annotation(_slot_paths(slots[index], kind)))
    output = tmp_path / name
    with output.open("wb") as stream: writer.write(stream)
    return output, manifest


def _content_paper(tmp_path, marks):
    base, manifest = _annotated_paper(tmp_path, [], "content-base.pdf")
    stream = io.BytesIO(); canvas = Canvas(stream, pagesize=A4)
    canvas.setLineWidth(1.8)
    slots = manifest["sides"]["front"]["slots"]
    for index, kind in marks:
        for path in _slot_paths(slots[index], kind):
            canvas.line(path[0][0] * mm, path[0][1] * mm, path[1][0] * mm, path[1][1] * mm)
    canvas.save(); stream.seek(0)
    overlay = PdfReader(stream); writer = PdfWriter(clone_from=base); writer.pages[0].merge_page(overlay.pages[0])
    output = tmp_path / "page-content.pdf"
    with output.open("wb") as target: writer.write(target)
    return output, manifest


def _classes(pdf, manifest, *, draw_annots=True):
    # pages_from_file is the production PDFium path and must include annotations.
    image = next(pages_from_file(pdf))[1]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    result = []
    for slot in manifest["sides"]["front"]["slots"]:
        x0, y0, x1, y1 = _roi_pixels(slot["roi"], manifest["page"], gray.shape[1], gray.shape[0])
        result.append(classify_slot(gray[y0:y1, x0:x1])[0])
    return result


@pytest.mark.parametrize("marks,expected", [
    ([], []), ([(0,"slash_forward")], [(0,"slash_forward")]),
    ([(0,"slash_forward"),(2,"slash_forward")], [(0,"slash_forward"),(2,"slash_forward")]),
    ([(0,"x")], [(0,"x")]),
    ([(0,"slash_forward"),(1,"slash_back"),(2,"x")], [(0,"slash_forward"),(1,"slash_back"),(2,"x")]),
    ([(0,"slash_forward"),(1,"slash_forward"),(2,"slash_forward"),(3,"slash_forward"),(4,"slash_forward")], [(0,"slash_forward"),(1,"slash_forward"),(2,"slash_forward"),(3,"slash_forward"),(4,"slash_forward")]),
])
def test_pdf_annotation_ink_matrix(tmp_path, marks, expected):
    pdf, manifest = _annotated_paper(tmp_path, marks)
    classes = _classes(pdf, manifest)
    assert [(index, classes[index]) for index, _ in expected] == expected
    assert sum(value.startswith("slash_") for value in classes) == sum(kind.startswith("slash_") for _, kind in marks)
    if len(marks) == 2 and marks[0][0] == 0 and marks[1][0] == 2:
        assert classes[1] == "blank"  # skipped slots remain blank


def test_same_table_updated_annotation_is_a_distinct_version(tmp_path):
    first, manifest = _annotated_paper(tmp_path, [(0,"slash_forward")], "v1.pdf")
    second, _ = _annotated_paper(tmp_path, [(0,"slash_forward"),(1,"slash_back")], "v2.pdf")
    assert hashlib.sha256(first.read_bytes()).digest() != hashlib.sha256(second.read_bytes()).digest()
    assert _classes(first, manifest)[:2] == ["slash_forward", "blank"]
    assert _classes(second, manifest)[:2] == ["slash_forward", "slash_back"]


def test_pdf_page_content_marks_render_with_annotations_enabled(tmp_path):
    pdf, manifest = _content_paper(tmp_path, [(0,"slash_forward"),(1,"slash_back"),(2,"x")])
    assert _classes(pdf, manifest)[:3] == ["slash_forward", "slash_back", "x"]
