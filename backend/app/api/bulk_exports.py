"""Bounded shared-lab ZIP exports for immutable Runner and Analyzer data."""

import hashlib
import json
import os
import tempfile
import unicodedata
import zipfile
import uuid
from datetime import timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.background import BackgroundTask

from ..config import get_settings
from ..database import get_db
from ..dependencies import require_researcher
from ..models import (
    Experiment,
    ExperimentRun,
    RunAnalysisRevision,
    User,
)
from ..schemas.bulk_exports import BulkExportRequest
from ..services.storage import get_object_storage


router = APIRouter(prefix="/api/v1", tags=["bulk exports"])

_ANALYSIS_FILENAMES = {
    "analysis_csv": "analysis.csv",
    "trainable_json": "trainable.json",
}
_ZIP_CHUNK_SIZE = 1024 * 1024


def _safe_component(value, fallback, *, max_length=80):
    value = unicodedata.normalize("NFC", str(value or ""))
    cleaned = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in value
        if ord(character) >= 32
    ).strip(" ._")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    return cleaned[:max_length].rstrip(" .") or fallback


def _iso_timestamp(value):
    if value is None:
        return None
    value = _utc_datetime(value)
    return value.isoformat().replace("+00:00", "Z")


def _utc_datetime(value):
    """Normalize database timestamps before comparing mixed storage models."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _zip_info(path):
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _run_root(run):
    participant = f"{run.participant_number:06d}"
    return f"participants/{participant}/runs/{run.id}"


def _raw_entry(run, result):
    block_name = _safe_component(result.block_name, "block")
    path = (
        f"{_run_root(run)}/raw/"
        f"block-{result.block_index:04d}-{block_name}.json"
    )
    return {
        "path": path,
        "run_id": str(run.id),
        "participant_number": run.participant_number,
        "kind": "raw_data",
        "source_id": str(result.id),
        "sha256": result.sha256,
        "size_bytes": result.size_bytes,
        "created_at": _iso_timestamp(result.created_at),
        "revision": None,
        "storage_key": result.storage_key,
    }


def _analysis_entry(run, artifact, revision=None):
    if revision is None:
        created = _iso_timestamp(artifact.created_at).replace(":", "-")
        copy_path = f"legacy-{created}-{artifact.id}"
        revision_number = None
    else:
        copy_path = f"revision-{revision.revision_number:04d}-{revision.id}"
        revision_number = revision.revision_number
    path = (
        f"{_run_root(run)}/analysis/{copy_path}/"
        f"{_ANALYSIS_FILENAMES[artifact.kind]}"
    )
    return {
        "path": path,
        "run_id": str(run.id),
        "participant_number": run.participant_number,
        "kind": artifact.kind,
        "source_id": str(artifact.id),
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "created_at": _iso_timestamp(artifact.created_at),
        "revision": revision_number,
        "storage_key": artifact.storage_key,
    }


def _analysis_artifacts(run, kind, policy):
    candidates = []
    for revision in run.analysis_revisions:
        if not revision.finalized:
            continue
        artifact = next(
            (item for item in revision.artifacts if item.kind == kind),
            None,
        )
        if artifact is not None:
            candidates.append((revision, artifact))
    candidates.extend(
        (None, artifact)
        for artifact in run.artifacts
        if artifact.kind == kind and artifact.analysis_revision_id is None
    )
    # Artifact creation time is the source of truth across both storage models.
    # A revision number and immutable artifact UUID provide a stable tie-break.
    candidates.sort(
        key=lambda item: (
            _utc_datetime(item[1].created_at),
            item[0].revision_number if item[0] is not None else 0,
            str(item[1].id),
        )
    )
    if policy == "latest" and candidates:
        return candidates[-1:]
    return candidates


def _selected_entries(runs, payload):
    entries = []
    requested = set(payload.include)
    for run in sorted(
        runs,
        key=lambda item: (item.participant_number, item.created_at, str(item.id)),
    ):
        if "raw_data" in requested:
            for result in sorted(
                run.results,
                key=lambda item: (item.block_index, str(item.id)),
            ):
                entries.append(_raw_entry(run, result))
        for kind in ("analysis_csv", "trainable_json"):
            if kind not in requested:
                continue
            analysis_artifacts = _analysis_artifacts(
                run, kind, payload.analysis_policy
            )
            entries.extend(
                _analysis_entry(run, artifact, revision)
                for revision, artifact in analysis_artifacts
            )
    entries.sort(key=lambda item: item["path"])
    return entries


def _public_entry(entry):
    return {
        key: value
        for key, value in entry.items()
        if key != "storage_key"
    }


def _write_archive(path, storage, experiment, runs, payload, entries):
    manifest = {
        "schema_version": "1.0",
        "experiment": {
            "id": str(experiment.id),
            "name": experiment.name,
        },
        "selection": {
            "run_ids": [str(run_id) for run_id in payload.run_ids],
            "include": list(payload.include),
            "analysis_policy": payload.analysis_policy,
        },
        "runs": [
            {
                "id": str(run.id),
                "participant_number": run.participant_number,
                "session_id": run.session_id,
                "created_at": _iso_timestamp(run.created_at),
            }
            for run in sorted(
                runs,
                key=lambda item: (
                    item.participant_number,
                    item.created_at,
                    str(item.id),
                ),
            )
        ],
        "entries": [_public_entry(entry) for entry in entries],
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with zipfile.ZipFile(path, "w", allowZip64=True) as archive:
        archive.writestr(_zip_info("manifest.json"), manifest_bytes)
        for entry in entries:
            with archive.open(_zip_info(entry["path"]), "w") as destination:
                for chunk in storage.iter_object(
                    entry["storage_key"], chunk_size=_ZIP_CHUNK_SIZE
                ):
                    destination.write(chunk)


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_ZIP_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_and_remove(path):
    try:
        with path.open("rb") as source:
            while chunk := source.read(_ZIP_CHUNK_SIZE):
                yield chunk
    finally:
        path.unlink(missing_ok=True)


@router.post(
    "/experiments/{experiment_id}/bulk-export",
    summary="Download selected Run data as one ZIP",
    description=(
        "Builds a bounded temporary ZIP from immutable shared-lab cloud objects. "
        "Raw data always contains every available Block result. "
        "The analysis policy applies independently to analyzed CSV and trainable JSON. "
        "Finalized analysis revisions and legacy unversioned artifacts participate "
        "in the same newest/all selection. The ZIP includes "
        "manifest.json with the exact selection, artifact checksums, creation times, "
        "and analysis revision numbers."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "The generated ZIP archive.",
            "content": {
                "application/zip": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
            "headers": {
                "Content-Disposition": {
                    "description": "UTF-8 attachment filename for the generated ZIP.",
                    "schema": {"type": "string"},
                },
                "Content-Length": {
                    "description": "Compressed archive size in bytes.",
                    "schema": {"type": "integer"},
                },
                "X-Checksum-SHA256": {
                    "description": "SHA-256 digest of the complete ZIP archive.",
                    "schema": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                },
            },
        },
        404: {
            "description": (
                "The Experiment is unavailable to this user, or at least one Run "
                "does not belong to it."
            )
        },
        401: {"description": "Authentication is required or the token is invalid."},
        413: {"description": "Selected uncompressed artifacts exceed the server limit."},
        422: {"description": "Invalid, empty, duplicate, or oversized selection."},
    },
)
def bulk_export_experiment_results(
    experiment_id: uuid.UUID,
    payload: BulkExportRequest,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(require_researcher),
):
    experiment = database.scalar(
        select(Experiment).where(
            Experiment.id == experiment_id,
            Experiment.archived_at.is_(None),
        )
    )
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment was not found.")

    runs = list(
        database.scalars(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.id.in_(payload.run_ids),
            )
            .options(
                selectinload(ExperimentRun.results),
                selectinload(ExperimentRun.artifacts),
                selectinload(ExperimentRun.analysis_revisions).selectinload(
                    RunAnalysisRevision.artifacts
                ),
            )
        ).unique()
    )
    if len(runs) != len(payload.run_ids):
        raise HTTPException(
            status_code=404,
            detail="One or more experiment runs were not found.",
        )

    entries = _selected_entries(runs, payload)
    total_size = sum(entry["size_bytes"] for entry in entries)
    limit = get_settings().max_uncompressed_package_bytes
    if total_size > limit:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                "Selected artifacts exceed the maximum uncompressed bulk-export "
                "size. Reduce the selection and try again."
            ),
        )

    descriptor, raw_path = tempfile.mkstemp(
        prefix="autoscript-bulk-export-", suffix=".zip"
    )
    os.close(descriptor)
    archive_path = Path(raw_path)
    try:
        _write_archive(archive_path, storage, experiment, runs, payload, entries)
        archive_size = archive_path.stat().st_size
        archive_sha256 = _file_sha256(archive_path)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    filename = f"{_safe_component(experiment.name, 'experiment')}-results.zip"
    return StreamingResponse(
        _stream_and_remove(archive_path),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                "attachment; filename=\"experiment-results.zip\"; "
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(archive_size),
            "X-Checksum-SHA256": archive_sha256,
        },
        # Starlette runs this after the response task is cancelled as well as after
        # a normal response. The generator's finally block is the eager cleanup;
        # this idempotent task is the client-disconnect safety net.
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )
