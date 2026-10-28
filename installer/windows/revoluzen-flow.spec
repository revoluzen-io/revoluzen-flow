# -*- mode: python ; coding: utf-8 -*-
"""Revoluzen Flow — PyInstaller spec (Windows directory or single-file freeze).

Produces ``dist/revoluzen-flow/revoluzen.exe`` — a self-contained CLI + server that the
Inno Setup script (revoluzen-flow.iss) packages into the installer.
Set REVOLUZEN_FLOW_ONEFILE=1 for a single executable, or use build-portable.ps1.

Run from the repo root:
    .venv\\Scripts\\python -m PyInstaller installer\\windows\\revoluzen-flow.spec --noconfirm --clean

Why a spec instead of the inline ``--collect-all`` flags: this app pulls in
pandas / pyarrow / duckdb / cryptography / reportlab / tzdata / fastavro and
loads several nodes + connectors by NAME at runtime. A spec lets us collect
their data files (the 46 connector JSONs, the IANA tz database, reportlab
fonts, python-docx templates) and submodules reliably, which the 4-package
inline command did not. Validate the result with: ``revoluzen.exe selftest``.
"""

import os

# SPECPATH is injected by PyInstaller = this file's directory (installer/windows).
HERE = SPECPATH
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(REPO, "backend")
import sys
sys.path.insert(0, BACKEND)
ONEFILE = os.environ.get("REVOLUZEN_FLOW_ONEFILE") == "1"
# The import package is revoluzen_flow (underscore). The hyphen spelling is
# the PyPI/dist name and the install folder; it is not importable, and a
# rebrand find/replace left it here, pointing the freeze at a path that does
# not exist. Keep these two spellings straight: anything Python resolves by
# name takes the underscore, anything on disk or in a URL takes the hyphen.
ENTRY = os.path.join(BACKEND, "revoluzen_flow", "__main__.py")
if not os.path.exists(ENTRY):      # fail at spec-parse, not 40s into Analysis
    raise SystemExit(f"revoluzen-flow.spec: entry script not found: {ENTRY}")
ICON = os.path.join(HERE, "icons", "revoluzen-flow.ico")

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Collect WHOLESALE (code + data files + native libs + submodules) for the
# packages that ship data/native payloads or that we import dynamically.
_COLLECT = [
    "revoluzen_flow",  # our app — brings connectors/*.json, static/, seed_data/, every submodule
    "duckdb",        # native engine lib
    "pyarrow",       # native + data
    "pandas",
    "numpy",
    "fastavro",      # Avro reader
    "tzdata",        # IANA tz db — pyarrow.orc requires it on Windows
    "reportlab",     # bundled fonts (PDF reports)
    "docx",          # python-docx default template (DOCX reports)
    "openpyxl",
    "pydantic",
    "cryptography",
]

# 2026-10-28 — macOS now builds from this same spec (installer/macos/build.sh),
# so the Windows-only GUI stack is gated. pywebview's WebView2 backend and its
# pythonnet loader exist only on Windows; asking for them elsewhere logs a wall
# of "hidden import not found" and collects nothing useful.
IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    _COLLECT += [
        "webview",     # pywebview — native `revoluzen app` window (its JS bridge files)
        "clr_loader",  # pythonnet runtime loader used by the WebView2 backend
    ]
for pkg in _COLLECT:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# pywebview's Windows backend loads via pythonnet (clr); these are imported
# dynamically and must be forced in. If the freeze ever can't bundle them,
# `revoluzen-flow app` still falls back to a Chromium --app window at runtime.
if IS_WINDOWS:
    hiddenimports += [
        "clr", "webview.platforms.edgechromium", "webview.platforms.winforms",
    ]

# Submodules imported by name (PyInstaller's static analysis can miss these).
for pkg in ("revoluzen_flow", "uvicorn", "fastapi", "starlette", "anyio"):
    hiddenimports += collect_submodules(pkg)

# uvicorn[standard] picks its loop/http/ws implementations at runtime, plus a
# few other runtime/dynamic deps that aren't statically referenced.
hiddenimports += [
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "httptools", "websockets", "watchfiles", "dotenv",
    "bcrypt", "psutil", "httpx", "requests", "aiofiles",
    "multipart", "yaml", "prometheus_client", "fastavro._read_py",
]

# De-dup while preserving order.
hiddenimports = list(dict.fromkeys(hiddenimports))

a = Analysis(
    [ENTRY],
    pathex=[BACKEND],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "jupyter", "notebook", "pytest", "_pytest",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries if ONEFILE else [],
    a.datas if ONEFILE else [],
    exclude_binaries=not ONEFILE,
    # revoluzen.exe, NOT revoluzen-flow.exe. The console script in
    # pyproject.toml is `revoluzen`, revoluzen-flow.iss sets
    # MyAppExeName "revoluzen.exe", build.ps1 runs
    # dist\revoluzen-flow\revoluzen.exe selftest, and the portable
    # workflow uploads dist/portable/revoluzen.exe. The COLLECT name
    # below stays hyphenated — that one is the output FOLDER, which is
    # what the .iss copies from.
    name="revoluzen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX can trip antivirus heuristics; keep off
    console=True,               # revoluzen-flow is a CLI/server
    disable_windowed_traceback=False,
    icon=(ICON if os.path.exists(ICON) else None),
)

if not ONEFILE:
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="revoluzen-flow")
