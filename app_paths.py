from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Return the runtime base directory.

    - When bundled with PyInstaller, this points to the unpacked bundle dir.
    - When running from source, this points to the repo folder containing this file.
    """

    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def asset_path(rel: str) -> Path:
    """Return an absolute path for a bundled/read-only asset."""

    return app_dir() / rel


def user_data_dir(app_name: str = "TouchpadExperimentManager") -> Path:
    """Return a user-writable directory for outputs and session data."""

    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))

    d = base / app_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
