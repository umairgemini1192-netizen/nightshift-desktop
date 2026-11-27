# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for Night-Shift Desktop (Windows, windowed, no admin)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"
ICON_PATH = SPEC_DIR / "nightshift.ico"

# Generate the multi-resolution Windows .ico deterministically at build time.
_gen_spec = importlib.util.spec_from_file_location(
    "nightshift_generate_icon",
    str(SPEC_DIR / "generate_icon.py"),
)
assert _gen_spec is not None and _gen_spec.loader is not None
_gen_mod = importlib.util.module_from_spec(_gen_spec)
_gen_spec.loader.exec_module(_gen_mod)
_gen_mod.generate_icon_ico(ICON_PATH)

if not (FRONTEND_DIST / "index.html").exists():
    raise FileNotFoundError(
        f"Missing built frontend at {FRONTEND_DIST / 'index.html'}. "
        "Run 'npm ci && npm run tokens && npm run build' in frontend/ first."
    )

hidden_imports = [
    *collect_submodules("nightshift"),
    "pystray._win32",
    "PIL.Image",
    "webview.platforms.edgechromium",
]

datas = [
    (str(FRONTEND_DIST), "frontend/dist"),
    (str(ICON_PATH), "packaging"),
]

block_cipher = None

a = Analysis(
    [str(BACKEND_DIR / "nightshift" / "app.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NightShift",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NightShift",
)
