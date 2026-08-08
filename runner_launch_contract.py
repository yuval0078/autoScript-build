"""Small file-based launch contract shared by Interface and Runner.

The Interface writes a seed beside each extracted Block configuration before
starting the Runner. The Runner reads that seed while preparing the word
order. Keeping this sidecar protocol here prevents the Interface from
importing the Runner application merely to prepare a launch.
"""

from __future__ import annotations

import os


SESSION_SEED_SUFFIX = ".autoscript_session_seed"


def session_seed_path(config_path: str) -> str:
    """Return the stable sidecar path for an extracted Block config."""

    return f"{os.path.abspath(config_path)}{SESSION_SEED_SUFFIX}"


def write_runtime_session_seed(config_path: str, session_seed: str) -> None:
    """Persist the launch seed before handing the config to the Runner."""

    with open(session_seed_path(config_path), "w", encoding="utf-8") as handle:
        handle.write(str(session_seed).strip())


def read_runtime_session_seed(config_path: str):
    """Return the launch seed, or ``None`` for legacy/direct launches."""

    try:
        with open(session_seed_path(config_path), "r", encoding="utf-8") as handle:
            seed = handle.read().strip()
            return seed or None
    except OSError:
        return None


__all__ = [
    "SESSION_SEED_SUFFIX",
    "read_runtime_session_seed",
    "session_seed_path",
    "write_runtime_session_seed",
]
