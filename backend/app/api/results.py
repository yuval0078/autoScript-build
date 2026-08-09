import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_user
from ..models import (
    Experiment,
    ExperimentBlock,
    ExperimentRun,
    ExperimentRevision,
    RunArtifact,
    RunResult,
    User,
)
from ..schemas import (
    ExperimentRunResponse,
    RunAnalysisUpdate,
    RunArtifactResponse,
    RunCreate,
    RunResultResponse,
)
from ..services.raw_results import RawResultValidationError, validate_raw_result
from ..services.object_cleanup import drain_object_deletions, queue_object_deletions
from ..services.storage import get_object_storage


router = APIRouter(prefix="/api/v1", tags=["results"])
ARTIFACT_TYPES = {
    "analysis_csv": (".csv", "text/csv; charset=utf-8"),
    "trainable_json": (".json", "application/json"),
    "analysis_state": (".json", "application/json"),
}


def _artifact_response(artifact):
    return RunArtifactResponse(
        id=artifact.id,
        run_id=artifact.run_id,
        kind=artifact.kind,
        original_filename=artifact.original_filename,
        sha256=artifact.sha256,
        size_bytes=artifact.size_bytes,
        created_at=artifact.created_at,
        download_url=f"/api/v1/run-artifacts/{artifact.id}/download",
    )


def _result_response(result):
    return RunResultResponse(
        id=result.id,
        run_id=result.run_id,
        block_id=result.block_id,
        block_index=result.block_index,
        block_count=result.block_count,
        block_name=result.block_name,
        block_completed=result.block_completed,
        experiment_completed=result.experiment_completed,
        completed_word_count=result.completed_word_count,
        expected_word_count=result.expected_word_count,
        schema_version=result.schema_version,
        app_version=result.app_version,
        result_timestamp=result.result_timestamp,
        original_filename=result.original_filename,
        sha256=result.sha256,
        size_bytes=result.size_bytes,
        created_at=result.created_at,
        download_url=f"/api/v1/run-results/{result.id}/download",
    )


def _run_response(run):
    results = sorted(run.results, key=lambda result: result.block_index)
    artifacts = sorted(run.artifacts, key=lambda item: item.created_at, reverse=True)
    completed_word_count = sum(result.completed_word_count for result in results)
    revision_blocks = _authoritative_revision_blocks(run)
    expected_word_count = (
        sum(block.expected_word_count for block in revision_blocks)
        if revision_blocks is not None
        else sum(result.expected_word_count for result in results)
    )
    return ExperimentRunResponse(
        id=run.id,
        experiment_id=run.experiment_id,
        revision_id=run.revision_id,
        status=run.status,
        started_at=run.started_at,
        finalized_at=run.finalized_at,
        session_id=run.session_id,
        participant_number=run.participant_number,
        participant_age=run.participant_age,
        participant_gender=run.participant_gender,
        block_count=run.block_count,
        source_experiment_name=run.source_experiment_name,
        source_experiment_id=run.source_experiment_id,
        analysis_completed=run.analysis_completed,
        analysis_updated_at=run.analysis_updated_at,
        created_at=run.created_at,
        result_count=len(results),
        raw_data_count=len(results),
        analyzed_csv_count=sum(
            artifact.kind == "analysis_csv" for artifact in artifacts
        ),
        trainable_json_count=sum(
            artifact.kind == "trainable_json" for artifact in artifacts
        ),
        completed_word_count=completed_word_count,
        expected_word_count=expected_word_count,
        complete=_run_is_complete(run, results),
        results=[_result_response(result) for result in results],
        artifacts=[_artifact_response(artifact) for artifact in artifacts],
    )


def _ordered_revision_blocks(run):
    revision = getattr(run, "revision", None)
    if revision is None:
        return []
    return sorted(revision.blocks, key=lambda block: block.position)


def _authoritative_revision_blocks(run):
    blocks = _ordered_revision_blocks(run)
    if (
        len(blocks) != run.block_count
        or any(block.expected_word_count is None for block in blocks)
    ):
        return None
    return blocks


def _run_is_complete(run, results=None):
    results = list(run.results if results is None else results)
    revision_blocks = _authoritative_revision_blocks(run)
    if revision_blocks is not None:
        results_by_index = {result.block_index: result for result in results}
        return (
            len(results_by_index) == len(revision_blocks)
            and all(
                (result := results_by_index.get(block.position + 1)) is not None
                and result.completed_word_count == block.expected_word_count
                for block in revision_blocks
            )
        )
    return (
        len(results) == run.block_count
        and all(result.block_completed for result in results)
        and all(result.experiment_completed for result in results)
        and sum(result.completed_word_count for result in results)
        == sum(result.expected_word_count for result in results)
    )


def _owned_experiment(database, actor, experiment_id, *, lock=False):
    statement = (
        select(Experiment)
        .where(
            Experiment.id == experiment_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
        .options(selectinload(Experiment.blocks))
    )
    if lock:
        statement = statement.with_for_update()
    experiment = database.scalar(statement)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment was not found.")
    return experiment


def _owned_run(database, actor, run_id):
    run = database.scalar(
        select(ExperimentRun)
        .join(Experiment, ExperimentRun.experiment_id == Experiment.id)
        .where(
            ExperimentRun.id == run_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
        .options(
            selectinload(ExperimentRun.results),
            selectinload(ExperimentRun.artifacts),
            selectinload(ExperimentRun.revision).selectinload(ExperimentRevision.blocks),
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Experiment run was not found.")
    return run


@router.post(
    "/experiment-revisions/{revision_id}/runs",
    response_model=ExperimentRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_experiment_run(
    revision_id: uuid.UUID,
    payload: RunCreate,
    response: Response,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    revision = database.scalar(
        select(ExperimentRevision)
        .join(Experiment, ExperimentRevision.experiment_id == Experiment.id)
        .where(
            ExperimentRevision.id == revision_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
        .options(selectinload(ExperimentRevision.blocks))
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Experiment revision was not found.")
    existing = database.scalar(
        select(ExperimentRun)
        .where(
            ExperimentRun.experiment_id == revision.experiment_id,
            ExperimentRun.session_id == payload.session_id,
        )
        .options(
            selectinload(ExperimentRun.results),
            selectinload(ExperimentRun.artifacts),
            selectinload(ExperimentRun.revision).selectinload(ExperimentRevision.blocks),
        )
    )
    if existing is not None:
        expected = (
            existing.revision_id,
            existing.participant_number,
            existing.participant_age,
            existing.participant_gender,
        )
        actual = (
            revision.id,
            payload.participant_number,
            payload.participant_age,
            payload.participant_gender,
        )
        if expected != actual:
            raise HTTPException(status_code=409, detail="Run creation metadata conflicts with the existing session.")
        response.status_code = status.HTTP_200_OK
        return _run_response(existing)
    run = ExperimentRun(
        id=uuid.uuid4(), experiment_id=revision.experiment_id, revision_id=revision.id,
        session_id=payload.session_id,
        participant_number=payload.participant_number,
        participant_age=payload.participant_age,
        participant_gender=payload.participant_gender,
        block_count=len(revision.blocks),
        source_experiment_name=revision.name,
        source_experiment_id=str(revision.experiment_id),
        status="created", created_by=actor.id,
    )
    database.add(run)
    database.commit()
    database.refresh(run)
    return _run_response(run)


def _transition_run(run, target):
    terminal = {"completed", "incomplete", "failed", "cancelled"}
    if run.status == target:
        return
    if target == "running" and run.status in {"incomplete", "failed"}:
        run.status = "running"
        run.finalized_at = None
        return
    if run.status in terminal and run.status != target:
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}.")
    now = datetime.now(timezone.utc)
    if target == "running" and run.started_at is None:
        run.started_at = now
    if target in terminal:
        run.finalized_at = now
    run.status = target


@router.post("/runs/{run_id}/start", response_model=ExperimentRunResponse)
def start_run(
    run_id: uuid.UUID, database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    _transition_run(run, "running")
    database.commit()
    return _run_response(run)


@router.post("/runs/{run_id}/finalize", response_model=ExperimentRunResponse)
def finalize_run(
    run_id: uuid.UUID, database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    _transition_run(run, "completed" if _run_is_complete(run) else "incomplete")
    database.commit()
    return _run_response(run)


@router.post("/runs/{run_id}/cancel", response_model=ExperimentRunResponse)
def cancel_run(
    run_id: uuid.UUID, database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    _transition_run(run, "cancelled")
    database.commit()
    return _run_response(run)


@router.post("/runs/{run_id}/fail", response_model=ExperimentRunResponse)
def fail_run(
    run_id: uuid.UUID, database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    _transition_run(run, "failed")
    database.commit()
    return _run_response(run)


def _safe_json_filename(value):
    value = Path(unquote(value or "result.json")).name
    if not value.lower().endswith(".json"):
        value = f"{value}.json"
    return (value or "result.json")[:255]


def _safe_artifact_filename(value, suffix):
    value = Path(unquote(value or f"artifact{suffix}")).name
    if not value.lower().endswith(suffix):
        value = f"{value}{suffix}"
    return (value or f"artifact{suffix}")[:255]


async def _receive_artifact(request, suffix):
    settings = get_settings()
    descriptor, raw_path = tempfile.mkstemp(
        prefix="autoscript-artifact-", suffix=suffix
    )
    os.close(descriptor)
    upload_path = Path(raw_path)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with upload_path.open("wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Artifact is too large.")
                digest.update(chunk)
                output.write(chunk)
        if size_bytes == 0:
            raise HTTPException(status_code=422, detail="Artifact is empty.")
        if suffix == ".json":
            try:
                import json

                json.loads(upload_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="Trainable artifact must be valid UTF-8 JSON.",
                ) from exc
        return upload_path, digest.hexdigest(), size_bytes
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise


async def _receive_result(request):
    settings = get_settings()
    descriptor, raw_path = tempfile.mkstemp(prefix="autoscript-result-", suffix=".json")
    os.close(descriptor)
    upload_path = Path(raw_path)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with upload_path.open("wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size_bytes += len(chunk)
                if size_bytes > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Result file is too large.")
                digest.update(chunk)
                output.write(chunk)
        if size_bytes == 0:
            raise HTTPException(status_code=422, detail="Result file is empty.")
        try:
            metadata = validate_raw_result(upload_path)
        except RawResultValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return upload_path, digest.hexdigest(), size_bytes, metadata
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise


def _ensure_consistent_run(run, metadata):
    expected = (
        run.participant_number,
        run.participant_age,
        run.participant_gender,
        run.block_count,
        run.source_experiment_name,
        run.source_experiment_id,
        str(run.revision_id) if run.revision_id else None,
    )
    actual = (
        metadata.participant_number,
        metadata.participant_age,
        metadata.participant_gender,
        metadata.block_count,
        metadata.experiment_name,
        metadata.source_experiment_id,
        metadata.experiment_revision_id,
    )
    if expected != actual:
        raise HTTPException(
            status_code=409,
            detail="Result metadata conflicts with the existing experiment run.",
        )


def _revision_block_for_result(run, metadata, *, allow_missing_server_run_id=False):
    """Validate current-format result identity against the pinned snapshot."""
    if run.revision_id is None:
        return None
    revision = run.revision
    if revision is None:
        raise HTTPException(status_code=409, detail="Run revision is unavailable.")

    blocks = _ordered_revision_blocks(run)
    if metadata.block_index < 1 or metadata.block_index > len(blocks):
        raise HTTPException(
            status_code=409,
            detail="Result Block index does not exist in the pinned revision.",
        )
    block = blocks[metadata.block_index - 1]
    expected_block_ids = {str(block.id)}
    if block.source_block_id is not None:
        expected_block_ids.add(str(block.source_block_id))

    identity_conflicts = []
    if metadata.source_experiment_id != str(run.experiment_id):
        identity_conflicts.append("Experiment ID")
    if metadata.experiment_revision_id != str(revision.id):
        identity_conflicts.append("revision ID")
    if metadata.experiment_revision_number != revision.revision_number:
        identity_conflicts.append("revision number")
    if metadata.block_count != len(blocks):
        identity_conflicts.append("Block count")
    if metadata.block_name != block.name:
        identity_conflicts.append("Block name")
    if metadata.source_block_id not in expected_block_ids:
        identity_conflicts.append("Block ID")
    if metadata.schema_version == "1.3":
        if metadata.server_run_id is None and allow_missing_server_run_id:
            pass
        elif metadata.server_run_id != str(run.id):
            identity_conflicts.append("server Run ID")
    if (
        block.expected_word_count is not None
        and metadata.expected_word_count != block.expected_word_count
    ):
        identity_conflicts.append("expected word count")
    if identity_conflicts:
        raise HTTPException(
            status_code=409,
            detail=(
                "Result identity conflicts with the pinned revision: "
                + ", ".join(identity_conflicts)
                + "."
            ),
        )
    return block


def _persist_received_result(
    database, storage, actor, experiment, run, metadata,
    upload_path, sha256, size_bytes, filename, response, auto_finalize=False,
):
    _ensure_consistent_run(run, metadata)
    revision_block = _revision_block_for_result(
        run,
        metadata,
        allow_missing_server_run_id=auto_finalize,
    )
    existing = next(
        (result for result in run.results if result.block_index == metadata.block_index),
        None,
    )
    if existing is not None:
        if existing.sha256 != sha256:
            raise HTTPException(
                status_code=409,
                detail="A different immutable result already exists for this Run and Block index.",
            )
        response.status_code = status.HTTP_200_OK
        return _result_response(existing)
    if run.status in {"completed", "incomplete", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail=f"Run is already {run.status}.")

    block_id = None
    source_block_id = (
        revision_block.source_block_id
        if revision_block is not None
        else metadata.source_block_id
    )
    if source_block_id:
        try:
            candidate_id = uuid.UUID(str(source_block_id))
        except (ValueError, TypeError, AttributeError):
            candidate_id = None
        if candidate_id is not None and any(block.id == candidate_id for block in experiment.blocks):
            block_id = candidate_id
    completed_word_count = len(metadata.payload["words"])
    expected_word_count = (
        revision_block.expected_word_count
        if revision_block is not None and revision_block.expected_word_count is not None
        else metadata.expected_word_count
    )
    block_completed = completed_word_count == expected_word_count
    result = RunResult(
        id=uuid.uuid4(), run_id=run.id, block_id=block_id,
        block_index=metadata.block_index, block_count=metadata.block_count,
        block_name=metadata.block_name, block_completed=block_completed,
        experiment_completed=metadata.experiment_completed,
        completed_word_count=completed_word_count,
        expected_word_count=expected_word_count,
        schema_version=metadata.schema_version, app_version=metadata.app_version,
        result_timestamp=metadata.timestamp,
        original_filename=_safe_json_filename(filename), sha256=sha256,
        size_bytes=size_bytes, created_by=actor.id, storage_key="pending",
    )
    storage_key = (
        f"experiments/{experiment.id}/runs/{run.id}/results/"
        f"{result.id}/{sha256}.json"
    )
    result.storage_key = storage_key
    try:
        storage.put_file(storage_key, upload_path, "application/json")
        database.add(result)
        if run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        candidate_results = [
            item for item in run.results if item.block_index != result.block_index
        ] + [result]
        legacy_complete = auto_finalize and _run_is_complete(run, candidate_results)
        run.status = "completed" if legacy_complete else "running"
        if legacy_complete:
            run.finalized_at = datetime.now(timezone.utc)
        database.commit()
        database.refresh(result)
        return _result_response(result)
    except Exception:
        database.rollback()
        storage.remove_object(storage_key)
        raise


@router.post(
    "/runs/{run_id}/results",
    response_model=RunResultResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_run_result(
    run_id: uuid.UUID,
    request: Request,
    response: Response,
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    experiment = _owned_experiment(database, actor, run.experiment_id, lock=True)
    upload_path, sha256, size_bytes, metadata = await _receive_result(request)
    try:
        return _persist_received_result(
            database, storage, actor, experiment, run, metadata,
            upload_path, sha256, size_bytes, x_filename, response,
        )
    finally:
        upload_path.unlink(missing_ok=True)


@router.post(
    "/experiments/{experiment_id}/results",
    response_model=RunResultResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_result(
    experiment_id: uuid.UUID,
    request: Request,
    response: Response,
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    upload_path, sha256, size_bytes, metadata = await _receive_result(request)
    try:
        run = database.scalar(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == experiment.id,
                ExperimentRun.session_id == metadata.session_id,
            )
            .options(selectinload(ExperimentRun.results))
        )
        if run is None:
            revision_id = None
            if metadata.experiment_revision_id:
                try:
                    revision_id = uuid.UUID(metadata.experiment_revision_id)
                except (ValueError, TypeError, AttributeError) as exc:
                    raise HTTPException(status_code=422, detail="Invalid Experiment revision ID.") from exc
                revision = database.scalar(select(ExperimentRevision).where(
                    ExperimentRevision.id == revision_id,
                    ExperimentRevision.experiment_id == experiment.id,
                ))
                if revision is None:
                    raise HTTPException(status_code=409, detail="Experiment revision does not belong to this Experiment.")
            run = ExperimentRun(
                experiment_id=experiment.id,
                revision_id=revision_id,
                session_id=metadata.session_id,
                participant_number=metadata.participant_number,
                participant_age=metadata.participant_age,
                participant_gender=metadata.participant_gender,
                block_count=metadata.block_count,
                source_experiment_name=metadata.experiment_name,
                source_experiment_id=metadata.source_experiment_id,
                status="running",
                started_at=datetime.now(timezone.utc),
                created_by=actor.id,
            )
            database.add(run)
            database.flush()
        return _persist_received_result(
            database, storage, actor, experiment, run, metadata,
            upload_path, sha256, size_bytes, x_filename, response,
            auto_finalize=True,
        )
    finally:
        upload_path.unlink(missing_ok=True)


@router.get(
    "/experiments/{experiment_id}/runs",
    response_model=list[ExperimentRunResponse],
)
def list_experiment_runs(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id)
    runs = database.scalars(
        select(ExperimentRun)
        .where(ExperimentRun.experiment_id == experiment.id)
        .options(
            selectinload(ExperimentRun.results),
            selectinload(ExperimentRun.artifacts),
            selectinload(ExperimentRun.revision).selectinload(ExperimentRevision.blocks),
        )
        .order_by(ExperimentRun.created_at.desc())
    ).all()
    return [_run_response(run) for run in runs]


@router.get("/runs/{run_id}", response_model=ExperimentRunResponse)
def get_experiment_run(
    run_id: uuid.UUID,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return _run_response(_owned_run(database, actor, run_id))


@router.patch("/runs/{run_id}/analysis", response_model=ExperimentRunResponse)
def update_run_analysis(
    run_id: uuid.UUID,
    payload: RunAnalysisUpdate,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    run.analysis_completed = payload.completed
    run.analysis_updated_at = datetime.now(timezone.utc)
    database.commit()
    return _run_response(run)


@router.post(
    "/runs/{run_id}/artifacts/{kind}",
    response_model=RunArtifactResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_run_artifact(
    run_id: uuid.UUID,
    kind: str,
    request: Request,
    response: Response,
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    if kind not in ARTIFACT_TYPES:
        raise HTTPException(status_code=404, detail="Artifact type was not found.")
    suffix, content_type = ARTIFACT_TYPES[kind]
    upload_path, sha256, size_bytes = await _receive_artifact(request, suffix)
    storage_key = None
    try:
        existing = next(
            (
                artifact
                for artifact in run.artifacts
                if artifact.kind == kind and artifact.sha256 == sha256
            ),
            None,
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return _artifact_response(existing)

        artifact = RunArtifact(
            id=uuid.uuid4(),
            run_id=run.id,
            kind=kind,
            storage_key="pending",
            original_filename=_safe_artifact_filename(x_filename, suffix),
            sha256=sha256,
            size_bytes=size_bytes,
            created_by=actor.id,
        )
        storage_key = (
            f"experiments/{run.experiment_id}/runs/{run.id}/artifacts/"
            f"{kind}/{artifact.id}/{sha256}{suffix}"
        )
        artifact.storage_key = storage_key
        storage.put_file(storage_key, upload_path, content_type)
        database.add(artifact)
        database.commit()
        database.refresh(artifact)
        return _artifact_response(artifact)
    except Exception:
        database.rollback()
        if storage_key is not None:
            storage.remove_object(storage_key)
        raise
    finally:
        upload_path.unlink(missing_ok=True)


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_experiment_run(
    run_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    run = _owned_run(database, actor, run_id)
    storage_keys = {
        item.storage_key for item in [*run.results, *run.artifacts]
    }
    queue_object_deletions(database, storage_keys)
    database.delete(run)
    database.commit()
    drain_object_deletions(database, storage)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/run-results/{result_id}/download")
def download_run_result(
    result_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    result = database.scalar(
        select(RunResult)
        .join(ExperimentRun)
        .join(Experiment)
        .where(
            RunResult.id == result_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Run result was not found.")
    return StreamingResponse(
        storage.iter_object(result.storage_key),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(result.original_filename)}"
            ),
            "Content-Length": str(result.size_bytes),
            "X-Checksum-SHA256": result.sha256,
        },
    )


@router.get("/run-artifacts/{artifact_id}/download")
def download_run_artifact(
    artifact_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    artifact = database.scalar(
        select(RunArtifact)
        .join(ExperimentRun)
        .join(Experiment)
        .where(
            RunArtifact.id == artifact_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Run artifact was not found.")
    _suffix, content_type = ARTIFACT_TYPES[artifact.kind]
    return StreamingResponse(
        storage.iter_object(artifact.storage_key),
        media_type=content_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(artifact.original_filename)}"
            ),
            "Content-Length": str(artifact.size_bytes),
            "X-Checksum-SHA256": artifact.sha256,
        },
    )
