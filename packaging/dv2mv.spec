# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the dv2mv macOS .app.
#
# Build:  pyinstaller packaging/dv2mv.spec        (run from the repo root)
# Output: dist/dv2mv.app
#
# See packaging/entry.py for the re-entrant dispatch this bundles, and
# packaging/build_macos.sh for the full icns → build → sign → dmg pipeline.
import os
import shutil

from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller = the dir holding this spec (packaging/).
ROOT = os.path.dirname(SPECPATH)            # repo root (engine.py, tkapp.py, …)

# ── the scientific stack PyInstaller can't see by static analysis ───────────
# The pipeline scripts run via runpy from bundled *data*, and librosa/sklearn
# are imported lazily — so nothing in the analyzed import graph references these.
# collect_all drags in every submodule + data file (numba/llvmlite JIT bits,
# opentimelineio's plugin manifests, sklearn's compiled extensions).
#
# Deliberately NOT collect_all'd: numpy, scipy, cv2, PIL — they're reachable
# through the analyzed graph (librosa→numpy/scipy, scenedetect→cv2, tkapp→PIL)
# and PyInstaller's built-in hooks bundle them correctly. collect_all('scipy')
# in particular force-imports scipy._lib.array_api_compat.{torch,dask}, dragging
# in gigabytes of torch — the built-in hook avoids that. matplotlib is omitted
# too: it's only the optional track_analyze --plot path, which the engine's
# analyze stage doesn't use.
datas, binaries, hiddenimports = [], [], []
for pkg in (
    "librosa", "numba", "llvmlite", "sklearn", "scenedetect", "opentimelineio",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ── our own resources ───────────────────────────────────────────────────────
# assets/ (icons, SGI fonts, hero img) and the pipeline scripts themselves —
# engine.SCRIPT resolves to <bundle>/pipeline/*.py, and entry.py runpy-runs them.
datas += [
    (os.path.join(ROOT, "assets"), "assets"),
    (os.path.join(ROOT, "pipeline"), "pipeline"),
]

# ── vendored native binaries → <bundle>/bin (entry.py prepends it to PATH) ──
def _native(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(
            f"dv2mv.spec: required binary '{name}' not found on PATH — "
            f"install it (e.g. `brew install {name}`) before building.")
    return (path, "bin")

# ffmpeg/ffprobe are required; rubberband is optional (Retempo R3, ffmpeg
# atempo fallback) but cheap to include so the bundled app gets the better path.
binaries += [_native("ffmpeg"), _native("ffprobe")]
_rb = shutil.which("rubberband")
if _rb:
    binaries += [(_rb, "bin")]

block_cipher = None

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],                 # so `import engine` / `import tkapp` resolve
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + ["engine", "tkapp"],
    hookspath=[],
    runtime_hooks=[],
    # web/test tiers + heavy optional backends scipy's array_api_compat probes
    excludes=["fastapi", "uvicorn", "starlette", "pytest", "torch", "dask"],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="dv2mv",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # windowed GUI app
    target_arch=None,              # host arch (arm64); universal2 not supported
    codesign_identity=None,        # signing handled in build_macos.sh
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="dv2mv",
)

_icon = os.path.join(SPECPATH, "dv2mv.icns")
app = BUNDLE(
    coll,
    name="dv2mv.app",
    icon=_icon if os.path.exists(_icon) else None,
    bundle_identifier="org.kevingriffin.dv2mv",
    info_plist={
        "CFBundleName": "dv2mv",
        "CFBundleDisplayName": "dv2mv",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # The app shells out to bundled CLIs; it is not a document-based app.
        "LSBackgroundOnly": False,
    },
)
