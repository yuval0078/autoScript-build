"""Stable bootstrap that launches the active Main Interface component."""

from __future__ import annotations

import subprocess
import sys

from component_runtime import component_launch_command


def launch_interface(arguments=None):
    command = component_launch_command(
        "interface",
        arguments or [],
        source_script="main_interface.py",
        legacy_executable="AutoScriptInterface.exe",
    )
    if command is None:
        raise FileNotFoundError(
            "The AutoScript Interface is not installed. Reinstall the AutoScript bootstrap."
        )
    return subprocess.call(command)


def main():
    try:
        return launch_interface(sys.argv[1:])
    except Exception as exc:
        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "AutoScript Launch Failed", str(exc))
            del app
        except Exception:
            print(f"AutoScript launch failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
