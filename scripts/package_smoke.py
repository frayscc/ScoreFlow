"""Fast packaging preflight; actual platform packages must be built on that platform."""
from pathlib import Path
from importlib.metadata import version
import cv2
import numpy
import pypdfium2
import reportlab

from scoreflow.config import FRONTEND_DIST

root = Path(__file__).resolve().parents[1]
required = [FRONTEND_DIST / "index.html", root / "backend" / "scoreflow" / "main.py"]
missing = [str(path) for path in required if not path.exists()]
if missing:
    raise SystemExit("打包预检失败，缺少：" + ", ".join(missing))
print("打包预检通过")
print("OpenCV", cv2.__version__)
print("NumPy", numpy.__version__)
print("PDFium", version("pypdfium2"))
print("ReportLab", reportlab.Version)
