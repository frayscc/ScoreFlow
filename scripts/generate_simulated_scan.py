from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium


ROOT = Path(__file__).resolve().parents[1]


def roi_pixels(roi, page, width, height):
    return (
        round(roi["x_mm"] / page["width_mm"] * width),
        round((page["height_mm"] - roi["y_mm"] - roi["height_mm"]) / page["height_mm"] * height),
        round((roi["x_mm"] + roi["width_mm"]) / page["width_mm"] * width),
        round((page["height_mm"] - roi["y_mm"]) / page["height_mm"] * height),
    )


def draw_mark(image, box, kind, rng):
    x0, y0, x1, y1 = box
    pad = max(3, round((x1 - x0) * 0.20))
    jitter = lambda: rng.randint(-2, 2)
    color, width = (25, 25, 25), max(2, round((x1 - x0) * 0.07))
    if kind in ("slash_forward", "x"):
        cv2.line(image, (x0 + pad + jitter(), y1 - pad + jitter()), (x1 - pad + jitter(), y0 + pad + jitter()), color, width, cv2.LINE_AA)
    if kind in ("slash_back", "x"):
        cv2.line(image, (x0 + pad + jitter(), y0 + pad + jitter()), (x1 - pad + jitter(), y1 - pad + jitter()), color, width, cv2.LINE_AA)
    if kind == "review":
        cv2.circle(image, ((x0 + x1) // 2, (y0 + y1) // 2), max(3, (x1 - x0) // 5), color, width, cv2.LINE_AA)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--marks-per-side", type=int, default=120)
    args = parser.parse_args()
    out = ROOT / "var" / "phase1"
    pdf_path = out / "ScoreFlow-演示周期-纸表01.pdf"
    manifest_path = out / "ScoreFlow-演示周期-纸表01.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pdf = pdfium.PdfDocument(pdf_path)
    rng = random.Random(20260913)
    truth = {}
    for page_index, side_name in enumerate(("front", "back")):
        bitmap = pdf[page_index].render(scale=300 / 72)
        image = cv2.cvtColor(np.asarray(bitmap.to_pil().convert("RGB")), cv2.COLOR_RGB2BGR)
        slots = manifest["sides"][side_name]["slots"]
        chosen = rng.sample(slots, min(args.marks_per_side, len(slots)))
        side_truth = {}
        kinds = ["slash_forward", "slash_back", "x", "review"]
        for index, slot in enumerate(chosen):
            kind = kinds[index % len(kinds)]
            box = roi_pixels(slot["roi"], manifest["page"], image.shape[1], image.shape[0])
            draw_mark(image, box, kind, rng)
            side_truth[slot["slot_id"]] = kind
        path = out / f"simulated-{side_name}.png"
        cv2.imwrite(str(path), image)
        truth[side_name] = side_truth
        print(path)
    (out / "simulated-ground-truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

