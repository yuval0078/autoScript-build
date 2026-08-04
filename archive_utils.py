"""Safe archive extraction and collision-free temporary-file helpers."""

from __future__ import annotations

import os
import re
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Union


class UnsafeArchiveError(ValueError):
    """Raised when a ZIP member could escape the extraction directory."""


def _normalized_member_path(member_name: str) -> PurePosixPath:
    """Normalize ZIP separators without allowing platform-specific escapes."""

    normalized = member_name.replace("\\", "/")
    return PurePosixPath(normalized)


def _is_symlink(member: zipfile.ZipInfo) -> bool:
    unix_mode = member.external_attr >> 16
    return stat.S_ISLNK(unix_mode)


def _validate_member(member: zipfile.ZipInfo, destination: Path) -> None:
    member_path = _normalized_member_path(member.filename)
    parts = member_path.parts

    if not member.filename or member.filename.startswith(("/", "\\")):
        raise UnsafeArchiveError(f"Unsafe absolute ZIP path: {member.filename!r}")

    if re.match(r"^[A-Za-z]:", member.filename):
        raise UnsafeArchiveError(f"Unsafe drive-qualified ZIP path: {member.filename!r}")

    if any(part in ("", ".", "..") for part in parts):
        raise UnsafeArchiveError(f"Unsafe ZIP path component: {member.filename!r}")

    if _is_symlink(member):
        raise UnsafeArchiveError(f"ZIP symbolic links are not allowed: {member.filename!r}")

    target = (destination.joinpath(*parts)).resolve()
    try:
        target.relative_to(destination)
    except ValueError as exc:
        raise UnsafeArchiveError(
            f"ZIP member escapes extraction directory: {member.filename!r}"
        ) from exc


def safe_extract_zip(
    archive: Union[str, os.PathLike[str], zipfile.ZipFile],
    destination: Union[str, os.PathLike[str]],
) -> Path:
    """Extract a ZIP only after validating every member path.

    Absolute paths, drive-qualified paths, ``..`` traversal, empty path
    components, and symbolic-link entries are rejected before any extraction
    occurs.
    """

    destination_path = Path(destination).resolve()
    destination_path.mkdir(parents=True, exist_ok=True)

    owns_archive = not isinstance(archive, zipfile.ZipFile)
    zip_ref = zipfile.ZipFile(archive, "r") if owns_archive else archive

    try:
        members = zip_ref.infolist()
        for member in members:
            _validate_member(member, destination_path)
        for member in members:
            zip_ref.extract(member, destination_path)
    finally:
        if owns_archive:
            zip_ref.close()

    return destination_path


def unique_temp_path(
    directory: Union[str, os.PathLike[str]],
    *,
    prefix: str = "autoscript_",
    suffix: str = ".wav",
) -> Path:
    """Reserve and return a unique temporary path in ``directory``."""

    directory_path = Path(directory)
    directory_path.mkdir(parents=True, exist_ok=True)
    safe_prefix = re.sub(r"[^0-9A-Za-z._-]+", "_", prefix) or "autoscript_"

    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=safe_prefix,
        suffix=suffix,
        dir=directory_path,
        delete=False,
    )
    try:
        return Path(handle.name)
    finally:
        handle.close()


def remove_file_quietly(path: Union[str, os.PathLike[str], None]) -> None:
    """Best-effort removal for temporary files."""

    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
