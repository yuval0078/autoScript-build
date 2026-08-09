# -*- mode: python ; coding: utf-8 -*-

import shutil
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
COMPONENT_ID = "builder"
ENTRYPOINT = "Builder.exe"
FORBIDDEN_COMPONENT_MODULES = (
    "main_interface",
    "gui_menu",
    "experiment_results",
    "tablet_experiment",
    "launch_experiment",
    "analyzer_refactored",
    "launch_analyzer",
    "pygame",
)


def _ffmpeg_binaries():
    bundled = ROOT / "assets" / "bin" / "ffmpeg.exe"
    discovered = shutil.which("ffmpeg")
    ffmpeg = bundled if bundled.is_file() else (Path(discovered) if discovered else None)
    if ffmpeg is None or not ffmpeg.is_file():
        raise RuntimeError("Builder build requires ffmpeg.exe in assets/bin or PATH.")
    binaries = [(str(ffmpeg), "assets/bin")]
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    if ffprobe.is_file():
        binaries.append((str(ffprobe), "assets/bin"))
    return binaries


analysis = Analysis(
    [str(ROOT / "builder_main.py")],
    pathex=[str(ROOT)],
    binaries=_ffmpeg_binaries(),
    datas=[(str(ROOT / "component_versions.json"), ".")],
    hiddenimports=[
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "PyQt5.QtMultimedia",
        "audio_processor",
        "builder_workspace",
        "component_versions",
        "exp_initializer",
        "experiment_packages",
        "numpy",
        "pydub",
        "pydub.silence",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(FORBIDDEN_COMPONENT_MODULES),
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Builder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_builder",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="Builder",
)
