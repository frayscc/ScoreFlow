"""Generate the V2-3 two-page proof: page content marks plus PDF Ink annotations."""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from scoreflow.omr.template import generate_template


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "ScoreFlow-V2-3-electronic-ink-proof.pdf"
WORK = ROOT / "tmp" / "pdfs" / "v2-3-proof"


def paths(slot, kind):
    roi = slot["roi"]; x, y, w, h = roi["x_mm"], roi["y_mm"], roi["width_mm"], roi["height_mm"]
    forward = [(x + .55, y + .55), (x + w - .55, y + h - .55)]
    backward = [(x + .55, y + h - .55), (x + w - .55, y + .55)]
    return [forward, backward] if kind == "x" else [forward if kind == "slash_forward" else backward]


def annotation(strokes):
    ink, xs, ys = ArrayObject(), [], []
    for stroke in strokes:
        values = ArrayObject()
        for x, y in stroke:
            values.extend((FloatObject(x * mm), FloatObject(y * mm))); xs.append(x * mm); ys.append(y * mm)
        ink.append(values)
    return DictionaryObject({NameObject("/Type"):NameObject("/Annot"),NameObject("/Subtype"):NameObject("/Ink"),
        NameObject("/Rect"):ArrayObject([FloatObject(min(xs)-2),FloatObject(min(ys)-2),FloatObject(max(xs)+2),FloatObject(max(ys)+2)]),
        NameObject("/InkList"):ink,NameObject("/C"):ArrayObject([FloatObject(0),FloatObject(0),FloatObject(0)]),
        NameObject("/Border"):ArrayObject([NumberObject(0),NumberObject(0),FloatObject(1.8)]),NameObject("/F"):NumberObject(4)})


def main():
    WORK.mkdir(parents=True, exist_ok=True); OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    base = WORK / "base.pdf"; manifest_path = WORK / "base.manifest.json"
    manifest = generate_template(base, manifest_path, project_id="v2-3-proof", period_id="week-1", sheet_id="electronic-ink-proof",
                                 class_name="ScoreFlow 电子笔迹验收", period_name="第1周")
    stream = io.BytesIO(); canvas = Canvas(stream, pagesize=A4); canvas.setLineWidth(1.8)
    front = manifest["sides"]["front"]["slots"]
    for index, kind in ((0,"slash_forward"),(2,"slash_back"),(4,"x")):
        for stroke in paths(front[index],kind): canvas.line(*(coordinate * mm for point in stroke for coordinate in point))
    canvas.save(); stream.seek(0); writer = PdfWriter(clone_from=base); writer.pages[0].merge_page(PdfReader(stream).pages[0])
    back = manifest["sides"]["back"]["slots"]
    for index in range(5): writer.add_annotation(1, annotation(paths(back[index],"slash_forward")))
    with OUTPUT.open("wb") as target: writer.write(target)
    print(OUTPUT)


if __name__ == "__main__":
    main()
