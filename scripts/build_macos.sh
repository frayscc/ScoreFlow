#!/bin/zsh
set -euo pipefail

cd "${0:A:h}/.."
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "此脚本的首版交付目标是 macOS Apple Silicon (arm64)。" >&2
  exit 1
fi

npm --prefix frontend ci
npm --prefix frontend run build
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python scripts/package_smoke.py
.venv/bin/python -m PyInstaller --noconfirm --clean ScoreFlow.spec
ditto -c -k --sequesterRsrc --keepParent dist/ScoreFlow.app dist/ScoreFlow-macOS-arm64.zip
echo "完成：dist/ScoreFlow.app"
echo "完成：dist/ScoreFlow-macOS-arm64.zip"
