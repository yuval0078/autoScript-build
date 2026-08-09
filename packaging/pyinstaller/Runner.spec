# -*- mode: python ; coding: utf-8 -*-

import shutil
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
COMPONENT_ID = "runner"
ENTRYPOINT = "ExperimentRunner.exe"
FORBIDDEN_COMPONENT_MODULES = (
    "main_interface",
    "gui_menu",
    "experiment_results",
    "builder_main",
    "builder_workspace",
    "exp_initializer",
    "analyzer_refactored",
    "launch_analyzer",
)


def _ffmpeg_binaries():
    bundled = ROOT / "assets" / "bin" / "ffmpeg.exe"
    discovered = shutil.which("ffmpeg")
    ffmpeg = bundled if bundled.is_file() else (Path(discovered) if discovered else None)
    if ffmpeg is None or not ffmpeg.is_file():
        raise RuntimeError("Runner build requires ffmpeg.exe in assets/bin or PATH.")
    binaries = [(str(ffmpeg), "assets/bin")]
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    if ffprobe.is_file():
        binaries.append((str(ffprobe), "assets/bin"))
    return binaries


analysis = Analysis(
    [str(ROOT / "launch_experiment.py")],
    pathex=[str(ROOT)],
    binaries=_ffmpeg_binaries(),
    datas=[(str(ROOT / "component_versions.json"), ".")],
    hiddenimports=[
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "audio_processor",
        "component_versions",
        "numpy",
        "pydub",
        "pygame",
        "result_upload_queue",
        "runner_launch_contract",
        "tablet_experiment",
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
    name="ExperimentRunner",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_runner",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="ExperimentRunner",
)
