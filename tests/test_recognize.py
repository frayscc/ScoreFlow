import cv2
import numpy as np
import pytest

from scoreflow.omr.layout import build_manifest
from scoreflow.omr.recognize import align_with_markers, classify_slot


def sample(kind: str) -> np.ndarray:
    image = np.full((48, 48), 255, dtype=np.uint8)
    if kind in ("slash_forward", "x"):
        cv2.line(image, (9, 39), (39, 9), 0, 4, cv2.LINE_AA)
    if kind in ("slash_back", "x"):
        cv2.line(image, (9, 9), (39, 39), 0, 4, cv2.LINE_AA)
    if kind == "review":
        cv2.circle(image, (24, 24), 8, 0, 3, cv2.LINE_AA)
    return image


@pytest.mark.parametrize("kind", ["blank", "slash_forward", "slash_back", "x", "review"])
def test_four_state_classifier_and_review(kind):
    result, _, _ = classify_slot(sample(kind))
    assert result == kind


def test_skip_pattern_counts_only_slashes():
    kinds = ["slash_forward", "blank", "slash_back", "blank", "x"]
    results = [classify_slot(sample(kind))[0] for kind in kinds]
    assert sum(result.startswith("slash_") for result in results) == 2
    assert results[-1] == "x"


def test_marker_alignment_rejects_missing_markers():
    image = np.full((600, 420, 3), 255, dtype=np.uint8)
    manifest = build_manifest(project_id="p", period_id="period", sheet_id="sheet")
    with pytest.raises(ValueError, match="定位标记不足"):
        align_with_markers(image, manifest, "front")
