from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from scoreflow.omr.recognize import recognize_aligned

parser = argparse.ArgumentParser(description="识别已经与模板对齐的 Phase 1 样本")
parser.add_argument("image", type=Path)
parser.add_argument("--side", choices=("front", "back"), default="front")
args = parser.parse_args()
manifest = ROOT / "var" / "phase1" / "ScoreFlow-演示周期-纸表01.manifest.json"
result = recognize_aligned(args.image, manifest, args.side)
output = args.image.with_suffix(".recognition.json")
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result["counts"], ensure_ascii=False))
print(output)

