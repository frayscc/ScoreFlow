# -*- mode: python ; coding: utf-8 -*-
import sys
from pathlib import Path


root = Path(SPECPATH)
a = Analysis(
    [str(root / "packaging" / "scoreflow_entry.py")],
    pathex=[str(root / "backend")],
    binaries=[],
    datas=[
        (str(root / "frontend" / "dist"), "frontend/dist"),
        (str(root / "assets" / "fonts"), "assets/fonts"),
        (str(root / "THIRD_PARTY_LICENSES.md"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScoreFlow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
bundle = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ScoreFlow")
if sys.platform == "darwin":
    app = BUNDLE(
        bundle,
        name="ScoreFlow.app",
        icon=None,
        bundle_identifier="com.scoreflow.local",
        info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"},
    )
