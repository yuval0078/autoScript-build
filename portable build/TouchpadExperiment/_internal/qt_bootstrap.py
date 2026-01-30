import os
from pathlib import Path


def ensure_qt_platform_plugin_path() -> None:
    """Ensure Qt can find the Windows platform plugin (qwindows.dll).

    On some Windows setups with non-ASCII paths, Qt may compute an invalid
    plugin directory. Explicitly pointing to PyQt5's bundled plugins avoids
    "Could not find the Qt platform plugin 'windows'".

    Call this before constructing QApplication.
    """

    if os.name != "nt":
        return

    try:
        import PyQt5

        pyqt_base = Path(PyQt5.__file__).resolve().parent
        plugins_dir = pyqt_base / "Qt5" / "plugins"
        platforms_dir = plugins_dir / "platforms"
        qt_bin_dir = pyqt_base / "Qt5" / "bin"

        if qt_bin_dir.exists():
            os.add_dll_directory(str(qt_bin_dir))

        if platforms_dir.exists():
            os.environ.setdefault("QT_PLUGIN_PATH", str(plugins_dir))
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms_dir))
    except Exception:
        # Best-effort: if this fails, Qt will report a clear error on startup.
        return
