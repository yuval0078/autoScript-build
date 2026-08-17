"""Idempotent import of byte-exact historical participant Runs."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..models import (
    Experiment,
    ExperimentRevision,
    ExperimentRun,
    RunArtifact,
    RunResult,
    User,
)


MAX_ARCHIVE_MEMBERS = 1_000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 500 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500
ARTIFACT_CONTENT_TYPES = {
    "analysis_csv": "text/csv; charset=utf-8",
    "trainable_json": "application/json",
    "screenshots_zip": "application/zip",
}


class HistoricalImportError(ValueError):
    pass


def _parse_datetime(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalImportError(f"Invalid import timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise HistoricalImportError("Historical timestamps must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _validate_digest(value):
    value = str(value).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise HistoricalImportError("Every imported object must have a valid SHA-256 digest.")
    return value


def _load_archive(archive_path, extraction_root):
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
            raise HistoricalImportError("Historical import archive has an invalid member count.")
        names = set()
        total_bytes = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                info.is_dir()
                or info.flag_bits & 0x1
                or path.is_absolute()
                or "\\" in info.filename
                or any(part in {"", ".", ".."} for part in path.parts)
                or info.filename in names
            ):
                raise HistoricalImportError(f"Unsafe import archive member: {info.filename!r}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise HistoricalImportError(f"Import member is too large: {info.filename}")
            if info.compress_size == 0 and info.file_size:
                raise HistoricalImportError(f"Invalid compressed member: {info.filename}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise HistoricalImportError(f"Import member compression ratio is unsafe: {info.filename}")
            names.add(info.filename)
            total_bytes += info.file_size
        if total_bytes > MAX_ARCHIVE_BYTES or "manifest.json" not in names:
            raise HistoricalImportError("Historical import archive is incomplete or too large.")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HistoricalImportError("Historical import manifest is not valid UTF-8 JSON.") from exc
        if not isinstance(manifest, dict) or manifest.get("format_version") != "1.0":
            raise HistoricalImportError("Unsupported historical import format.")
        runs = manifest.get("runs")
        if not isinstance(runs, list) or not runs or manifest.get("run_count") != len(runs):
            raise HistoricalImportError("Historical import run count is invalid.")

        referenced = set()
        for run in runs:
            if not isinstance(run, dict):
                raise HistoricalImportError("Every historical Run must be an object.")
            for item in [*run.get("raw_results", []), *run.get("artifacts", [])]:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise HistoricalImportError("Historical file metadata is invalid.")
                path = item["path"]
                if path in referenced or path not in names:
                    raise HistoricalImportError(f"Missing or duplicate historical file: {path}")
                referenced.add(path)
                data = archive.read(path)
                if len(data) != int(item.get("size_bytes", -1)):
                    raise HistoricalImportError(f"Historical file size mismatch: {path}")
                if hashlib.sha256(data).hexdigest() != _validate_digest(item.get("sha256")):
                    raise HistoricalImportError(f"Historical file checksum mismatch: {path}")
                destination = extraction_root.joinpath(*PurePosixPath(path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        if names != referenced | {"manifest.json"}:
            raise HistoricalImportError("Historical import archive contains unreferenced files.")
        return manifest


def _find_experiment(database, name):
    experiments = list(
        database.scalars(
            select(Experiment)
            .where(
                func.lower(Experiment.name) == str(name).lower(),
                Experiment.archived_at.is_(None),
            )
            .options(
                selectinload(Experiment.blocks),
            )
        )
    )
    if len(experiments) != 1:
        raise HistoricalImportError(
            f"Expected exactly one active Experiment named {name!r}; found {len(experiments)}."
        )
    experiment = experiments[0]
    revision = database.scalar(
        select(ExperimentRevision)
        .where(ExperimentRevision.id == experiment.current_revision_id)
        .options(selectinload(ExperimentRevision.blocks))
    )
    if revision is None:
        raise HistoricalImportError("The target Experiment has no current revision.")
    return experiment, revision


def _existing_run_matches(run, spec):
    expected_results = sorted(
        (int(item["block_index"]), str(item["sha256"]))
        for item in spec["raw_results"]
    )
    actual_results = sorted((item.block_index, item.sha256) for item in run.results)
    expected_artifacts = sorted(
        (str(item["kind"]), str(item["sha256"])) for item in spec.get("artifacts", [])
    )
    actual_artifacts = sorted(
        (item.kind, item.sha256)
        for item in run.artifacts
        if item.analysis_revision_id is None
    )
    return (
        run.participant_number == int(spec["participant_number"])
        and run.participant_age == int(spec["participant_age"])
        and run.participant_gender == str(spec["participant_gender"])
        and actual_results == expected_results
        and actual_artifacts == expected_artifacts
    )


def import_historical_archive(
    database,
    storage,
    archive_path,
    *,
    actor_username="admin",
    apply=False,
):
    created_storage_keys = []
    with tempfile.TemporaryDirectory(prefix="autoscript-historical-import-") as temporary:
        extraction_root = Path(temporary)
        manifest = _load_archive(Path(archive_path), extraction_root)
        experiment, revision = _find_experiment(
            database, manifest.get("experiment_name")
        )
        actor = database.scalar(
            select(User).where(
                User.username == actor_username,
                User.role == "admin",
                User.is_active.is_(True),
            )
        )
        if actor is None:
            raise HistoricalImportError("The import actor must be an active administrator.")

        blocks = sorted(experiment.blocks, key=lambda item: item.position)
        revision_blocks = sorted(
            revision.blocks, key=lambda item: item.position
        )
        expected_counts = [int(value) for value in manifest.get("expected_block_word_counts", [])]
        if len(blocks) != len(expected_counts) or len(revision_blocks) != len(expected_counts):
            raise HistoricalImportError("Historical Block count does not match the target revision.")
        for index, expected in enumerate(expected_counts):
            for current in (blocks[index].expected_word_count, revision_blocks[index].expected_word_count):
                if current not in (None, 0, expected):
                    raise HistoricalImportError(
                        f"Block {index + 1} word count conflicts with the historical data."
                    )

        sessions = [str(run.get("session_id") or "") for run in manifest["runs"]]
        if len(set(sessions)) != len(sessions) or any(not value or len(value) > 128 for value in sessions):
            raise HistoricalImportError("Historical session IDs must be unique and bounded.")
        existing_runs = {
            run.session_id: run
            for run in database.scalars(
                select(ExperimentRun)
                .where(
                    ExperimentRun.experiment_id == experiment.id,
                    ExperimentRun.session_id.in_(sessions),
                )
                .options(
                    selectinload(ExperimentRun.results),
                    selectinload(ExperimentRun.artifacts),
                )
            )
        }
        unchanged = []
        pending = []
        for spec in manifest["runs"]:
            raw_results = spec.get("raw_results")
            if not isinstance(raw_results, list) or len(raw_results) != len(blocks):
                raise HistoricalImportError("Every historical Run must contain every Block.")
            indices = sorted(int(item.get("block_index", 0)) for item in raw_results)
            if indices != list(range(1, len(blocks) + 1)):
                raise HistoricalImportError("Historical Block indices are not contiguous.")
            artifacts = spec.get("artifacts", [])
            kinds = [item.get("kind") for item in artifacts]
            if len(set(kinds)) != len(kinds) or any(kind not in ARTIFACT_CONTENT_TYPES for kind in kinds):
                raise HistoricalImportError("Historical artifact kinds are invalid or duplicated.")
            existing = existing_runs.get(spec["session_id"])
            if existing is not None:
                if not _existing_run_matches(existing, spec):
                    raise HistoricalImportError(
                        f"Session {spec['session_id']} already exists with different content."
                    )
                unchanged.append(existing)
            else:
                pending.append(spec)

        summary = {
            "experiment_id": str(experiment.id),
            "experiment_name": experiment.name,
            "source_id": str(manifest.get("source_id") or ""),
            "run_count": len(manifest["runs"]),
            "new_runs": len(pending),
            "unchanged_runs": len(unchanged),
            "raw_results": sum(len(run["raw_results"]) for run in manifest["runs"]),
            "analysis_csv": sum(
                artifact["kind"] == "analysis_csv"
                for run in manifest["runs"]
                for artifact in run.get("artifacts", [])
            ),
            "trainable_json": sum(
                artifact["kind"] == "trainable_json"
                for run in manifest["runs"]
                for artifact in run.get("artifacts", [])
            ),
            "screenshots_zip": sum(
                artifact["kind"] == "screenshots_zip"
                for run in manifest["runs"]
                for artifact in run.get("artifacts", [])
            ),
            "expected_block_word_counts": expected_counts,
            "applied": bool(apply),
        }
        if not apply:
            return summary

        try:
            for index, expected in enumerate(expected_counts):
                blocks[index].expected_word_count = expected
                revision_blocks[index].expected_word_count = expected
            for spec in pending:
                run_id = uuid.uuid4()
                started_at = _parse_datetime(spec["started_at"])
                finalized_at = _parse_datetime(spec["finalized_at"])
                if finalized_at < started_at:
                    raise HistoricalImportError("Run finalization precedes its start time.")
                artifacts = spec.get("artifacts", [])
                run = ExperimentRun(
                    id=run_id,
                    experiment_id=experiment.id,
                    revision_id=experiment.current_revision_id,
                    session_id=spec["session_id"],
                    participant_number=int(spec["participant_number"]),
                    participant_age=int(spec["participant_age"]),
                    participant_gender=str(spec["participant_gender"]),
                    block_count=len(blocks),
                    source_experiment_name=experiment.name,
                    source_experiment_id=str(manifest.get("source_id") or "")[:255] or None,
                    status="completed",
                    started_at=started_at,
                    finalized_at=finalized_at,
                    analysis_completed=(
                        True
                        if {item["kind"] for item in artifacts}
                        >= {"analysis_csv", "trainable_json"}
                        else None
                    ),
                    analysis_updated_at=(datetime.now(timezone.utc) if artifacts else None),
                    created_by=actor.id,
                    created_at=finalized_at,
                )
                database.add(run)
                for item in spec["raw_results"]:
                    index = int(item["block_index"])
                    result_id = uuid.uuid4()
                    path = extraction_root.joinpath(*PurePosixPath(item["path"]).parts)
                    storage_key = (
                        f"experiments/{experiment.id}/runs/{run_id}/results/"
                        f"{index:04d}/{result_id}/{item['sha256']}.json"
                    )
                    storage.put_file(storage_key, path, "application/json")
                    created_storage_keys.append(storage_key)
                    database.add(
                        RunResult(
                            id=result_id,
                            run_id=run_id,
                            block_id=blocks[index - 1].id,
                            block_index=index,
                            block_count=len(blocks),
                            block_name=blocks[index - 1].name,
                            block_completed=True,
                            experiment_completed=True,
                            completed_word_count=int(item["completed_word_count"]),
                            expected_word_count=int(item["expected_word_count"]),
                            schema_version=str(item["schema_version"])[:32],
                            app_version=str(item["app_version"])[:32],
                            result_timestamp=str(item["result_timestamp"])[:32],
                            storage_key=storage_key,
                            original_filename=str(item["original_filename"])[:255],
                            sha256=item["sha256"],
                            size_bytes=int(item["size_bytes"]),
                            created_by=actor.id,
                            created_at=finalized_at,
                        )
                    )
                for item in artifacts:
                    artifact_id = uuid.uuid4()
                    path = extraction_root.joinpath(*PurePosixPath(item["path"]).parts)
                    suffix = {"analysis_csv": ".csv", "trainable_json": ".json", "screenshots_zip": ".zip"}[item["kind"]]
                    storage_key = (
                        f"experiments/{experiment.id}/runs/{run_id}/artifacts/"
                        f"{item['kind']}/{artifact_id}/{item['sha256']}{suffix}"
                    )
                    storage.put_file(storage_key, path, ARTIFACT_CONTENT_TYPES[item["kind"]])
                    created_storage_keys.append(storage_key)
                    database.add(
                        RunArtifact(
                            id=artifact_id,
                            run_id=run_id,
                            kind=item["kind"],
                            storage_key=storage_key,
                            original_filename=str(item["original_filename"])[:255],
                            sha256=item["sha256"],
                            size_bytes=int(item["size_bytes"]),
                            created_by=actor.id,
                        )
                    )
            database.commit()
            return summary
        except Exception:
            database.rollback()
            for storage_key in reversed(created_storage_keys):
                try:
                    storage.remove_object(storage_key)
                except Exception:
                    pass
            raise
