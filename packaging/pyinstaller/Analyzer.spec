# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
COMPONENT_ID = "analyzer"
ENTRYPOINT = "Analyzer.exe"
FORBIDDEN_COMPONENT_MODULES = (
    "main_interface",
    "gui_menu",
    "experiment_results",
    "builder_main",
    "builder_workspace",
    "exp_initializer",
    "tablet_experiment",
    "launch_experiment",
    "audio_processor",
    "pygame",
    "pydub",
    "numpy",
)

analysis = Analysis(
    [str(ROOT / "launch_analyzer.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "component_versions.json"), ".")],
    hiddenimports=[
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "analysis_local_state",
        "analysis_sync_queue",
        "analyzer_refactored",
        "app_paths",
        "autoscript_api",
        "component_versions",
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
    name="Analyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_analyzer",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="Analyzer",
)
