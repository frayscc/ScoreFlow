import json

import pypdfium2 as pdfium

from scoreflow.omr.template import generate_template


def test_pdf_and_manifest_are_generated(tmp_path):
    pdf_path = tmp_path / "paper.pdf"
    manifest_path = tmp_path / "paper.manifest.json"
    data = generate_template(
        pdf_path, manifest_path, project_id="p", period_id="period", sheet_id="sheet",
        class_name="二四一五班", period_name="第1—2周",
    )
    assert pdf_path.stat().st_size > 10_000
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["layout_hash"] == data["layout_hash"]
    document = pdfium.PdfDocument(pdf_path)
    assert len(document) == 2
