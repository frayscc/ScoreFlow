from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """Read-only application resources, including PyInstaller bundles."""
    return Path(getattr(sys, "_MEIPASS", ROOT)).resolve()


FRONTEND_DIST = resource_root() / "frontend" / "dist"


def _default_data_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return ROOT / "var"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ScoreFlow"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "ScoreFlow"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "ScoreFlow"


def data_dir() -> Path:
    path = Path(os.environ.get("SCOREFLOW_DATA_DIR", _default_data_dir())).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write-test"
    probe.write_bytes(b"")
    probe.unlink()
    return path
