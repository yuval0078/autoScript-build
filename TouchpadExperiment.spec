# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

# Define common source files to bundle
common_sources = [
    ('analyzer_refactored.py', '.'),
    ('tablet_experiment.py', '.'),
    ('audio_processor.py', '.'),
    ('app_paths.py', '.'),
    ('qt_bootstrap.py', '.'),
    ('gui_menu.py', '.'),
    ('exp_initializer.py', '.'),
    ('convert_audio.py', '.'),
]

# Main application
a_main = Analysis(
    ['main_interface.py'],
    pathex=[],
    binaries=[],
    datas=common_sources,
    hiddenimports=[
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'PyQt5.QtMultimedia',
        'pydub',
        'pydub.silence',
        'pygame',
        'numpy',
        'analyzer_refactored',
        'tablet_experiment',
        'audio_processor',
        'app_paths',
        'qt_bootstrap',
        'gui_menu',
        'exp_initializer',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Analyzer application
a_analyzer = Analysis(
    ['launch_analyzer.py'],
    pathex=[],
    binaries=[],
    datas=common_sources,
    hiddenimports=[
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'analyzer_refactored',
        'app_paths',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Experiment runner application
a_experiment = Analysis(
    ['launch_experiment.py'],
    pathex=[],
    binaries=[],
    datas=common_sources,
    hiddenimports=[
        'PyQt5.QtCore',
        'PyQt5.QtGui',
        'PyQt5.QtWidgets',
        'tablet_experiment',
        'qt_bootstrap',
        'app_paths',
        'pygame',
        'pydub',
        'numpy',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Merge all analyses to share dependencies
MERGE((a_main, 'TouchpadExperiment', 'TouchpadExperiment'),
      (a_analyzer, 'Analyzer', 'Analyzer'),
      (a_experiment, 'ExperimentRunner', 'ExperimentRunner'))

pyz_main = PYZ(a_main.pure, a_main.zipped_data, cipher=block_cipher)
pyz_analyzer = PYZ(a_analyzer.pure, a_analyzer.zipped_data, cipher=block_cipher)
pyz_experiment = PYZ(a_experiment.pure, a_experiment.zipped_data, cipher=block_cipher)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    [],
    exclude_binaries=True,
    name='TouchpadExperiment',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

exe_analyzer = EXE(
    pyz_analyzer,
    a_analyzer.scripts,
    [],
    exclude_binaries=True,
    name='Analyzer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

exe_experiment = EXE(
    pyz_experiment,
    a_experiment.scripts,
    [],
    exclude_binaries=True,
    name='ExperimentRunner',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe_main,
    a_main.binaries,
    a_main.zipfiles,
    a_main.datas,
    exe_analyzer,
    a_analyzer.binaries,
    a_analyzer.zipfiles,
    a_analyzer.datas,
    exe_experiment,
    a_experiment.binaries,
    a_experiment.zipfiles,
    a_experiment.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TouchpadExperiment',
)
