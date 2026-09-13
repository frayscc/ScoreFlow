import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from scoreflow.omr.template import generate_template

parser = argparse.ArgumentParser(description="生成可打印的 ScoreFlow 双面验收样表")
parser.add_argument("--output-dir", type=Path, default=ROOT / "var" / "phase1")
parser.add_argument("--filename", default="ScoreFlow-演示周期-纸表01.pdf")
args = parser.parse_args()
out = args.output_dir
pdf_path = out / args.filename
manifest = generate_template(
    pdf_path,
    pdf_path.with_suffix(".manifest.json"),
    project_id="11111111-1111-4111-8111-111111111111",
    period_id="22222222-2222-4222-8222-222222222222",
    sheet_id="33333333-3333-4333-8333-333333333333",
    class_name="二四一五班",
    period_name="第1—2周",
)
print(f"PDF与manifest已生成：{out}")
print(f"正面槽位：{len(manifest['sides']['front']['slots'])}")
print(f"背面槽位：{len(manifest['sides']['back']['slots'])}")
print(f"布局哈希：{manifest['layout_hash']}")
