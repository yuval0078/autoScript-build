"""GUI-independent Block and Experiment archive compatibility helpers.

An AutoScript ``Block`` is the ZIP format emitted by older Builders under the
name "experiment".  A new ``Experiment`` bundle is a ZIP containing an
``experiment.json`` manifest and one or more unchanged Block ZIPs.  This
module deliberately accepts legacy Block ZIPs as one-Block Experiments.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence, Union

from archive_utils import UnsafeArchiveError, safe_extract_zip


EXPERIMENT_MANIFEST = "experiment.json"
EXPERIMENT_SCHEMA_VERSION = "1.0"
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_BLOCK_MEMBER_PATTERN = re.compile(r"^blocks/[0-9]{3}_[^/]+\.zip$")
_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

PathLike = Union[str, os.PathLike[str]]


class ExperimentPackageError(ValueError):
    """Raised when a Block or Experiment archive violates the contract."""


@dataclass(frozen=True)
class BlockSource:
    """A Block ZIP plus optional manifest metadata overrides."""

    path: PathLike
    name: str | None = None
    block_id: str | None = None
    same_page_as_previous: bool = False


@dataclass(frozen=True)
class BlockInspection:
    """Validated metadata read from a Block ZIP."""

    name: str
    block_id: str | None
    schema_version: str | None
    package_type: str
    config_member: str
    config: Mapping[str, Any]
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ResolvedBlock:
    """One validated Block materialized from an input package."""

    name: str
    block_id: str | None
    same_page_as_previous: bool
    sha256: str
    archive_path: Path
    extracted_dir: Path
    config_path: Path
    config: Mapping[str, Any]


@dataclass(frozen=True)
class ResolvedExperiment:
    """A legacy one-Block package or a multi-Block Experiment bundle."""

    name: str
    experiment_id: str | None
    schema_version: str | None
    source_kind: str
    blocks: tuple[ResolvedBlock, ...]
    manifest: Mapping[str, Any] | None


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_text(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExperimentPackageError(f"{label} must be a non-empty string")
    return value


def _member_path(member: zipfile.ZipInfo) -> PurePosixPath:
    filename = member.filename
    normalized = filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not filename or filename.startswith(("/", "\\")):
        raise UnsafeArchiveError(f"Unsafe absolute ZIP path: {filename!r}")
    if _DRIVE_PATTERN.match(filename):
        raise UnsafeArchiveError(f"Unsafe drive-qualified ZIP path: {filename!r}")
    if any(part in ("", ".", "..") for part in path.parts):
        raise UnsafeArchiveError(f"Unsafe ZIP path component: {filename!r}")
    if stat.S_ISLNK(member.external_attr >> 16):
        raise UnsafeArchiveError(f"ZIP symbolic links are not allowed: {filename!r}")
    return path


def _validated_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    casefolded_members: set[str] = set()
    for member in archive.infolist():
        path = _member_path(member)
        normalized = path.as_posix()
        casefolded = normalized.casefold()
        if normalized in members or casefolded in casefolded_members:
            raise ExperimentPackageError(
                f"Duplicate ZIP member is not allowed: {normalized!r}"
            )
        members[normalized] = member
        casefolded_members.add(casefolded)
    return members


def _load_json_member(
    archive: zipfile.ZipFile, member: str | zipfile.ZipInfo
) -> Mapping[str, Any]:
    member_name = member.filename if isinstance(member, zipfile.ZipInfo) else member
    try:
        payload = json.loads(archive.read(member).decode("utf-8-sig"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentPackageError(
            f"Invalid JSON in ZIP member {member_name!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExperimentPackageError(f"{member_name!r} must contain a JSON object")
    return payload


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExperimentPackageError(f"Not a readable ZIP archive: {path}") from exc


def inspect_block(block_zip: PathLike) -> BlockInspection:
    """Validate and inspect a legacy or new Block ZIP.

    Legacy ZIPs need no new discriminator: the existing root config ``name``
    becomes the Block name.  New Block configs may additionally contain
    ``schema_version``, ``package_type: \"block\"``, and ``block_id``.
    """

    path = Path(block_zip)
    if not path.is_file():
        raise ExperimentPackageError(f"Block ZIP does not exist: {path}")
    size_bytes = path.stat().st_size
    sha256 = _sha256_path(path)

    with _open_zip(path) as archive:
        members = _validated_members(archive)
        root_json = [
            name
            for name, member in members.items()
            if not member.is_dir()
            and PurePosixPath(name).parent == PurePosixPath(".")
            and name.lower().endswith(".json")
        ]
        if len(root_json) != 1:
            raise ExperimentPackageError(
                "A Block ZIP must contain exactly one JSON config at its root"
            )
        config_member = root_json[0]
        config = _load_json_member(archive, members[config_member])
        package_type = config.get("package_type", "block")
        if package_type != "block":
            raise ExperimentPackageError(
                f"Block package_type must be 'block', got {package_type!r}"
            )
        name = _validate_text(config.get("name"), "Block name")
        block_id = _validate_text(config.get("block_id"), "Block id", optional=True)
        schema_version = _validate_text(
            config.get("schema_version"), "Block schema version", optional=True
        )

        files = config.get("files")
        if files is not None:
            if not isinstance(files, list):
                raise ExperimentPackageError("Block files must be an array")
            for index, media in enumerate(files):
                if not isinstance(media, dict):
                    raise ExperimentPackageError(
                        f"Block files[{index}] must be an object"
                    )
                media_path = media.get("path")
                if (
                    not isinstance(media_path, str)
                    or media_path not in members
                    or members[media_path].is_dir()
                ):
                    raise ExperimentPackageError(
                        f"Block media member is missing: {media_path!r}"
                    )

    return BlockInspection(
        name=name,
        block_id=block_id,
        schema_version=schema_version,
        package_type="block",
        config_member=config_member,
        config=config,
        sha256=sha256,
        size_bytes=size_bytes,
    )


def _safe_filename(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return (value or "block")[:80]


def _zip_info(member_name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(member_name, date_time=_FIXED_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _normalize_sources(
    blocks: Iterable[PathLike | BlockSource],
) -> list[tuple[BlockSource, BlockInspection]]:
    normalized: list[tuple[BlockSource, BlockInspection]] = []
    for block in blocks:
        source = block if isinstance(block, BlockSource) else BlockSource(block)
        inspection = inspect_block(source.path)
        name = source.name if source.name is not None else inspection.name
        block_id = source.block_id if source.block_id is not None else inspection.block_id
        _validate_text(name, "Block manifest name")
        _validate_text(block_id, "Block manifest id", optional=True)
        if source.name is not None and source.name != inspection.name:
            raise ExperimentPackageError(
                f"Block manifest name {source.name!r} does not match "
                f"Block config name {inspection.name!r}"
            )
        if (
            source.block_id is not None
            and inspection.block_id is not None
            and source.block_id != inspection.block_id
        ):
            raise ExperimentPackageError(
                f"Block manifest id {source.block_id!r} does not match "
                f"Block config id {inspection.block_id!r}"
            )
        if not isinstance(source.same_page_as_previous, bool):
            raise ExperimentPackageError(
                "Block same_page_as_previous must be a boolean"
            )
        normalized.append(
            (
                BlockSource(
                    source.path,
                    name,
                    block_id,
                    source.same_page_as_previous,
                ),
                inspection,
            )
        )
    if not normalized:
        raise ExperimentPackageError("An Experiment must contain at least one Block")
    if len(normalized) > 999:
        raise ExperimentPackageError("An Experiment cannot contain more than 999 Blocks")
    return normalized


def build_experiment_bundle(
    output_path: PathLike,
    experiment_name: str,
    blocks: Sequence[PathLike | BlockSource],
    *,
    experiment_id: str | None = None,
    schema_version: str = EXPERIMENT_SCHEMA_VERSION,
) -> Path:
    """Build a deterministic multi-Block Experiment ZIP.

    Block order is the input order.  Nested ZIP bytes are preserved exactly;
    rebuilding with identical arguments produces identical output bytes.
    """

    _validate_text(experiment_name, "Experiment name")
    _validate_text(experiment_id, "Experiment id", optional=True)
    _validate_text(schema_version, "Experiment schema version")
    sources = _normalize_sources(blocks)

    entries: list[dict[str, Any]] = []
    for index, (source, inspection) in enumerate(sources, start=1):
        member_path = f"blocks/{index:03d}_{_safe_filename(source.name or inspection.name)}.zip"
        entry = {
            "name": source.name or inspection.name,
            "path": member_path,
            "sha256": inspection.sha256,
            "same_page_as_previous": bool(
                index > 1 and source.same_page_as_previous
            ),
        }
        if source.block_id is not None:
            entry["id"] = source.block_id
        entries.append(entry)

    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "package_type": "experiment",
        "name": experiment_name,
        "blocks": entries,
    }
    if experiment_id is not None:
        manifest["id"] = experiment_id
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    temp_path = Path(temp_handle.name)
    temp_handle.close()
    try:
        with zipfile.ZipFile(temp_path, "w") as bundle:
            bundle.writestr(_zip_info(EXPERIMENT_MANIFEST), manifest_bytes)
            for entry, (source, _inspection) in zip(entries, sources):
                with bundle.open(_zip_info(entry["path"]), "w") as target:
                    with Path(source.path).open("rb") as block_file:
                        shutil.copyfileobj(block_file, target, length=1024 * 1024)
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return destination


def _load_manifest(
    archive: zipfile.ZipFile,
    members: Mapping[str, zipfile.ZipInfo],
) -> Mapping[str, Any] | None:
    member = members.get(EXPERIMENT_MANIFEST)
    if member is None or member.is_dir():
        return None
    candidate = _load_json_member(archive, member)
    if candidate.get("package_type") == "experiment":
        return candidate
    return None


def _validate_manifest(
    manifest: Mapping[str, Any], members: Mapping[str, zipfile.ZipInfo]
) -> tuple[str, str | None, str, list[Mapping[str, Any]]]:
    package_type = manifest.get("package_type")
    if package_type != "experiment":
        raise ExperimentPackageError(
            f"Experiment package_type must be 'experiment', got {package_type!r}"
        )
    name = _validate_text(manifest.get("name"), "Experiment name")
    experiment_id = _validate_text(
        manifest.get("id"), "Experiment id", optional=True
    )
    schema_version = _validate_text(
        manifest.get("schema_version"), "Experiment schema version"
    )
    blocks = manifest.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ExperimentPackageError("Experiment blocks must be a non-empty array")

    seen_paths: set[str] = set()
    validated: list[Mapping[str, Any]] = []
    for index, entry in enumerate(blocks):
        if not isinstance(entry, dict):
            raise ExperimentPackageError(f"Experiment blocks[{index}] must be an object")
        _validate_text(entry.get("name"), f"Experiment blocks[{index}].name")
        _validate_text(entry.get("id"), f"Experiment blocks[{index}].id", optional=True)
        same_page_as_previous = entry.get("same_page_as_previous", False)
        if not isinstance(same_page_as_previous, bool):
            raise ExperimentPackageError(
                f"Experiment blocks[{index}].same_page_as_previous must be a boolean"
            )
        if index == 0 and same_page_as_previous:
            raise ExperimentPackageError(
                "The first Experiment Block cannot share a page with a previous Block"
            )
        path_value = _validate_text(entry.get("path"), f"Experiment blocks[{index}].path")
        assert path_value is not None
        path = PurePosixPath(path_value.replace("\\", "/"))
        if (
            len(path.parts) != 2
            or path.parts[0] != "blocks"
            or not path.parts[1].lower().endswith(".zip")
            or path_value != path.as_posix()
            or not _BLOCK_MEMBER_PATTERN.fullmatch(path_value)
        ):
            raise ExperimentPackageError(
                f"Invalid nested Block path: {path_value!r}"
            )
        if path_value in seen_paths:
            raise ExperimentPackageError(f"Duplicate Block path: {path_value!r}")
        seen_paths.add(path_value)
        if path_value not in members or members[path_value].is_dir():
            raise ExperimentPackageError(f"Nested Block ZIP is missing: {path_value!r}")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _HASH_PATTERN.fullmatch(digest):
            raise ExperimentPackageError(
                f"Invalid SHA-256 for Experiment blocks[{index}]"
            )
        validated.append(entry)
    return name, experiment_id, schema_version, validated


def _copy_member_with_hash(
    archive: zipfile.ZipFile, member_name: str, target: Path
) -> str:
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member_name, "r") as source, target.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest()


def _materialize_block(
    archive_path: Path,
    extracted_dir: Path,
    *,
    expected_name: str | None = None,
    expected_id: str | None = None,
    same_page_as_previous: bool = False,
) -> ResolvedBlock:
    inspection = inspect_block(archive_path)
    if expected_name is not None and expected_name != inspection.name:
        raise ExperimentPackageError(
            f"Block manifest name {expected_name!r} does not match "
            f"Block config name {inspection.name!r}"
        )
    if (
        expected_id is not None
        and inspection.block_id is not None
        and expected_id != inspection.block_id
    ):
        raise ExperimentPackageError(
            f"Block manifest id {expected_id!r} does not match "
            f"Block config id {inspection.block_id!r}"
        )
    with _open_zip(archive_path) as block_archive:
        safe_extract_zip(block_archive, extracted_dir)
    config_path = extracted_dir.joinpath(*PurePosixPath(inspection.config_member).parts)
    return ResolvedBlock(
        name=expected_name or inspection.name,
        block_id=expected_id or inspection.block_id,
        same_page_as_previous=same_page_as_previous,
        sha256=inspection.sha256,
        archive_path=archive_path,
        extracted_dir=extracted_dir,
        config_path=config_path,
        config=inspection.config,
    )


def unpack_experiment_package(
    package_path: PathLike, destination: PathLike
) -> ResolvedExperiment:
    """Safely resolve a legacy Block ZIP or a new Experiment bundle.

    The destination must not already exist.  Exact Block ZIPs are materialized
    under ``block_archives`` and their contents under ``blocks``.  On any
    validation failure, the newly-created destination is removed.
    """

    source_path = Path(package_path)
    if not source_path.is_file():
        raise ExperimentPackageError(f"Package ZIP does not exist: {source_path}")
    destination_path = Path(destination)
    if destination_path.exists():
        raise ExperimentPackageError(
            f"Package destination already exists: {destination_path}"
        )
    destination_path.mkdir(parents=True)

    try:
        with _open_zip(source_path) as archive:
            members = _validated_members(archive)
            manifest = _load_manifest(archive, members)
            if manifest is None:
                inspection = inspect_block(source_path)
                filename = f"001_{_safe_filename(inspection.name)}.zip"
                archive_path = destination_path / "block_archives" / filename
                archive_path.parent.mkdir(parents=True)
                shutil.copyfile(source_path, archive_path)
                block = _materialize_block(
                    archive_path,
                    destination_path / "blocks" / filename[:-4],
                    same_page_as_previous=False,
                )
                return ResolvedExperiment(
                    name=inspection.name,
                    experiment_id=None,
                    schema_version=inspection.schema_version,
                    source_kind="block",
                    blocks=(block,),
                    manifest=None,
                )

            name, experiment_id, schema_version, entries = _validate_manifest(
                manifest, members
            )
            resolved_blocks: list[ResolvedBlock] = []
            for index, entry in enumerate(entries, start=1):
                filename = f"{index:03d}_{_safe_filename(entry['name'])}.zip"
                archive_path = destination_path / "block_archives" / filename
                actual_hash = _copy_member_with_hash(
                    archive, entry["path"], archive_path
                )
                if actual_hash != entry["sha256"]:
                    raise ExperimentPackageError(
                        f"SHA-256 mismatch for nested Block {entry['path']!r}"
                    )
                resolved_blocks.append(
                    _materialize_block(
                        archive_path,
                        destination_path / "blocks" / filename[:-4],
                        expected_name=entry["name"],
                        expected_id=entry.get("id"),
                        same_page_as_previous=entry.get(
                            "same_page_as_previous", False
                        ),
                    )
                )
            return ResolvedExperiment(
                name=name,
                experiment_id=experiment_id,
                schema_version=schema_version,
                source_kind="experiment",
                blocks=tuple(resolved_blocks),
                manifest=manifest,
            )
    except Exception:
        shutil.rmtree(destination_path, ignore_errors=True)
        raise


__all__ = [
    "BlockInspection",
    "BlockSource",
    "EXPERIMENT_MANIFEST",
    "EXPERIMENT_SCHEMA_VERSION",
    "ExperimentPackageError",
    "ResolvedBlock",
    "ResolvedExperiment",
    "build_experiment_bundle",
    "inspect_block",
    "unpack_experiment_package",
]
