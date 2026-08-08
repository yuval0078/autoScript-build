# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[1]
COMPONENT_ID = "bootstrap"
ENTRYPOINT = "AutoScriptLauncher.exe"
FORBIDDEN_COMPONENT_MODULES = (
    "main_interface",
    "gui_menu",
    "experiment_results",
    "builder_main",
    "builder_workspace",
    "exp_initializer",
    "tablet_experiment",
    "launch_experiment",
    "analyzer_refactored",
    "launch_analyzer",
    "audio_processor",
    "pygame",
    "pydub",
    "numpy",
)

analysis = Analysis(
    [str(ROOT / "autoscript_launcher.py")],
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
        "component_runtime",
        "component_update_manager",
        "component_versions",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
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
    name="AutoScriptLauncher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    contents_directory="_launcher",
)
collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    name="AutoScriptLauncher",
)
