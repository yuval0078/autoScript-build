"""Strict component-version metadata for independently delivered applications."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app_paths import asset_path


COMPONENT_NAMES = ("interface", "builder", "runner", "analyzer")
_VERSION_PATTERN = re.compile(r"^(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*)){1,3}$")


class ComponentVersionError(ValueError):
    """Raised when bundled or remote component version metadata is invalid."""


def parse_component_version(value: str) -> tuple[int, int, int, int]:
    """Parse a strict two-to-four-part numeric version into an ordered tuple."""

    if not isinstance(value, str) or not _VERSION_PATTERN.fullmatch(value):
        raise ComponentVersionError(
            "Component versions must contain two to four numeric parts without "
            "leading zeroes."
        )
    parts = [int(part) for part in value.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def compare_component_versions(left: str, right: str) -> int:
    """Return -1, 0, or 1 according to the numeric component-version order."""

    left_parts = parse_component_version(left)
    right_parts = parse_component_version(right)
    return (left_parts > right_parts) - (left_parts < right_parts)


def load_component_versions(path: str | Path | None = None) -> dict[str, str]:
    """Load and strictly validate the bundled component version registry."""

    source = Path(path) if path is not None else asset_path("component_versions.json")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ComponentVersionError(
            f"Could not read component versions from {source}: {exc}"
        ) from exc

    if not isinstance(payload, dict) or set(payload) != {"schema_version", "components"}:
        raise ComponentVersionError("Component version metadata has unexpected fields.")
    if payload["schema_version"] != 1:
        raise ComponentVersionError("Unsupported component version schema.")
    components = payload["components"]
    if not isinstance(components, dict) or set(components) != set(COMPONENT_NAMES):
        raise ComponentVersionError(
            "Component version metadata must define interface, builder, runner, and analyzer."
        )
    validated = {}
    for name in COMPONENT_NAMES:
        version = components[name]
        parse_component_version(version)
        validated[name] = version
    return validated


def get_component_version(component: str, path: str | Path | None = None) -> str:
    if component not in COMPONENT_NAMES:
        raise ComponentVersionError(f"Unknown component: {component!r}")
    return load_component_versions(path)[component]


COMPONENT_VERSIONS = load_component_versions()


__all__ = [
    "COMPONENT_NAMES",
    "COMPONENT_VERSIONS",
    "ComponentVersionError",
    "compare_component_versions",
    "get_component_version",
    "load_component_versions",
    "parse_component_version",
]
