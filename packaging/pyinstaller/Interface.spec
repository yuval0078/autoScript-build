# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
COMPONENT_ID = "interface"
ENTRYPOINT = "AutoScriptInterface.exe"
FORBIDDEN_COMPONENT_MODULES = (
    "builder_main",
    "builder_workspace",
    "exp_initializer",
    "tablet_experiment",
    "launch_experiment",
    "analyzer_refactored",
    "launch_analyzer",
    "audio_processor",
    "convert_audio",
    "pygame",
    "pydub",
    "numpy",
)

analysis = Analysis(
    [str(ROOT / "main_interface.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "component_versions.json"), "."),
        (str(ROOT / "update_trust.json"), "."),
    ],
    hiddenimports=[
        "PyQt5.QtCore",
        "PyQt5.QtGui",
        "PyQt5.QtWidgets",
        "analysis_sync_queue",
        "component_update_manager",
        "component_versions",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
        "experiment_packages",
        "experiment_results",
        "gui_menu",
        "runner_launch_contract",
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
    name="AutoScriptInterface",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_interface",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="AutoScriptInterface",
)
