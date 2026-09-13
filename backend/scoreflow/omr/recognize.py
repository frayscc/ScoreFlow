from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np


CLASSES = ("blank", "slash_forward", "slash_back", "x", "review")


def align_with_markers(image: np.ndarray, manifest: dict[str, Any], side_name: str) -> np.ndarray:
    """Correct mild perspective distortion using the four solid registration squares."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    binary = cv2.threshold(gray, 90, 255, cv2.THRESH_BINARY_INV)[1]
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = gray.shape
    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if not (0.00004 * width * height <= area <= 0.0005 * width * height):
            continue
        if not 0.75 <= w / max(h, 1) <= 1.33:
            continue
        if float(binary[y:y+h, x:x+w].mean()) / 255 < 0.72:
            continue
        candidates.append(np.array([x + w / 2, y + h / 2], dtype=np.float32))
    if len(candidates) < 4:
        raise ValueError("定位标记不足，页面可能被严重裁切")
    corners = [np.array([0, 0]), np.array([width, 0]), np.array([0, height]), np.array([width, height])]
    observed = np.array([min(candidates, key=lambda point: np.linalg.norm(point - corner)) for corner in corners], dtype=np.float32)
    expected = []
    page = manifest["page"]
    for marker in (manifest["sides"][side_name]["markers"][2], manifest["sides"][side_name]["markers"][3], manifest["sides"][side_name]["markers"][0], manifest["sides"][side_name]["markers"][1]):
        expected.append([
            (marker["x_mm"] + marker["width_mm"] / 2) / page["width_mm"] * width,
            (page["height_mm"] - marker["y_mm"] - marker["height_mm"] / 2) / page["height_mm"] * height,
        ])
    transform = cv2.getPerspectiveTransform(observed, np.array(expected, dtype=np.float32))
    return cv2.warpPerspective(image, transform, (width, height), borderValue=(255, 255, 255))


def _roi_pixels(roi: dict[str, float], page: dict[str, float], width: int, height: int) -> tuple[int, int, int, int]:
    x0 = round(roi["x_mm"] / page["width_mm"] * width)
    x1 = round((roi["x_mm"] + roi["width_mm"]) / page["width_mm"] * width)
    y0 = round((page["height_mm"] - roi["y_mm"] - roi["height_mm"]) / page["height_mm"] * height)
    y1 = round((page["height_mm"] - roi["y_mm"]) / page["height_mm"] * height)
    return x0, y0, x1, y1


def classify_slot(gray: np.ndarray) -> tuple[str, float, dict[str, float]]:
    h, w = gray.shape
    inset = max(2, round(min(h, w) * 0.16))
    inner = gray[inset:h-inset, inset:w-inset]
    ink = inner < 175
    count = int(ink.sum())
    area = max(1, inner.size)
    density = count / area
    yy, xx = np.indices(inner.shape)
    tolerance = max(1.5, inner.shape[0] * 0.12)
    forward_band = np.abs((inner.shape[0] - 1 - yy) - xx) <= tolerance
    back_band = np.abs(yy - xx) <= tolerance
    forward = float(ink[forward_band].mean())
    back = float(ink[back_band].mean())
    features = {"ink_density": round(density, 4), "forward": round(forward, 4), "back": round(back, 4)}
    if density < 0.018:
        return "blank", min(0.99, 0.82 + (0.018 - density) * 8), features
    # A circle or filled correction often crosses both diagonal bands weakly.
    # Require strong evidence on both diagonals before accepting X.
    if forward >= 0.42 and back >= 0.42 and density >= 0.16:
        return "x", min(0.99, 0.60 + min(forward, back)), features
    if forward >= 0.16 and forward >= back * 1.45:
        return "slash_forward", min(0.98, 0.55 + forward), features
    if back >= 0.16 and back >= forward * 1.45:
        return "slash_back", min(0.98, 0.55 + back), features
    return "review", 0.0, features


def recognize_aligned(image_path: Path, manifest_path: Path, side_name: str) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"无法读取图像：{image_path}")
    observations = []
    for slot in manifest["sides"][side_name]["slots"]:
        x0, y0, x1, y1 = _roi_pixels(slot["roi"], manifest["page"], image.shape[1], image.shape[0])
        classification, confidence, features = classify_slot(image[y0:y1, x0:x1])
        observations.append({"slot_id": slot["slot_id"], "classification": classification, "confidence": round(confidence, 4), "features": features})
    counts = {name: sum(item["classification"] == name for item in observations) for name in CLASSES}
    return {"side": side_name, "algorithm_version": "prototype-1", "aligned_input": True, "counts": counts, "observations": observations}
