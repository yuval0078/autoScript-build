"""Resolve independently installed AutoScript component entry points."""

from __future__ import annotations

import sys
from pathlib import Path

from app_paths import source_script_path
from component_update_manager import ComponentUpdateManager, InstallStateError


UPDATE_CATALOG_URL = (
    "https://github.com/yuval0078/autoScript-build/releases/latest/download/"
    "autoscript-update-catalog.json"
)


def create_update_manager(**overrides):
    return ComponentUpdateManager(UPDATE_CATALOG_URL, **overrides)


def installed_component_entrypoint(component, manager=None):
    manager = manager or create_update_manager()
    installed = manager.get_installed_component(component)
    if installed is None:
        return None
    entrypoint = (
        manager.root
        / Path(installed.relative_path)
        / Path(installed.entrypoint)
    ).resolve()
    try:
        entrypoint.relative_to(manager.components_dir.resolve())
    except ValueError as exc:
        raise InstallStateError(
            f"Installed {component} entrypoint escapes the component directory."
        ) from exc
    if not entrypoint.is_file():
        raise InstallStateError(f"Installed {component} entrypoint is missing.")
    return entrypoint


def component_launch_command(
    component,
    arguments=None,
    *,
    source_script=None,
    legacy_executable=None,
    manager=None,
    frozen=None,
    executable=None,
):
    """Return a managed, legacy, or source-mode command without credentials."""

    arguments = [str(argument) for argument in (arguments or [])]
    executable = Path(executable or sys.executable).resolve()
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))

    if not frozen:
        if not source_script:
            return None
        script = source_script_path(source_script)
        return [str(executable), str(script), *arguments] if script.is_file() else None

    entrypoint = installed_component_entrypoint(component, manager=manager)
    if entrypoint is not None:
        return [str(entrypoint), *arguments]

    if legacy_executable:
        legacy = executable.parent / legacy_executable
        if legacy.is_file():
            return [str(legacy), *arguments]
    return None


__all__ = [
    "UPDATE_CATALOG_URL",
    "component_launch_command",
    "create_update_manager",
    "installed_component_entrypoint",
]
