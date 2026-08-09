import hashlib
import os
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, aliased, selectinload

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_user
from ..models import (
    Experiment,
    ExperimentBlock,
    ExperimentRun,
    ExperimentRevision,
    ExperimentRevisionBlock,
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
from .pagination import decode_cursor, encode_cursor, escaped_contains_pattern


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


def _run_response(run, *, include_files=True, summary=None):
    if include_files:
        results = sorted(run.results, key=lambda result: result.block_index)
        artifacts = sorted(run.artifacts, key=lambda item: item.created_at, reverse=True)
        completed_word_count = sum(result.completed_word_count for result in results)
        revision_blocks = _authoritative_revision_blocks(run)
        expected_word_count = (
            sum(block.expected_word_count for block in revision_blocks)
            if revision_blocks is not None
            else sum(result.expected_word_count for result in results)
        )
        result_count = len(results)
        raw_data_count = len(results)
        analyzed_csv_count = sum(
            artifact.kind == "analysis_csv" for artifact in artifacts
        )
        trainable_json_count = sum(
            artifact.kind == "trainable_json" for artifact in artifacts
        )
        complete = _run_is_complete(run, results)
        response_results = [_result_response(result) for result in results]
        response_artifacts = [_artifact_response(artifact) for artifact in artifacts]
    else:
        if summary is None:
            raise ValueError("A summary is required when file details are omitted.")
        result_count = summary["result_count"]
        raw_data_count = summary["raw_data_count"]
        analyzed_csv_count = summary["analyzed_csv_count"]
        trainable_json_count = summary["trainable_json_count"]
        completed_word_count = summary["completed_word_count"]
        expected_word_count = summary["expected_word_count"]
        complete = summary["complete"]
        response_results = []
        response_artifacts = []
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
        result_count=result_count,
        raw_data_count=raw_data_count,
        analyzed_csv_count=analyzed_csv_count,
        trainable_json_count=trainable_json_count,
        completed_word_count=completed_word_count,
        expected_word_count=expected_word_count,
        complete=complete,
        results=response_results,
        artifacts=response_artifacts,
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


def _run_complete_sql_expression():
    """Build the SQL equivalent of ``_run_is_complete`` for list filtering."""
    revision_block = aliased(ExperimentRevisionBlock)
    matching_result = aliased(RunResult)

    revision_block_count = (
        select(func.count(revision_block.id))
        .where(revision_block.revision_id == ExperimentRun.revision_id)
        .correlate(ExperimentRun)
        .scalar_subquery()
    )
    revision_null_expected_count = (
        select(func.count(revision_block.id))
        .where(
            revision_block.revision_id == ExperimentRun.revision_id,
            revision_block.expected_word_count.is_(None),
        )
        .correlate(ExperimentRun)
        .scalar_subquery()
    )
    result_count = (
        select(func.count(RunResult.id))
        .where(RunResult.run_id == ExperimentRun.id)
        .correlate(ExperimentRun)
        .scalar_subquery()
    )
    missing_or_mismatched_revision_result = (
        exists(
            select(1)
            .select_from(revision_block)
            .where(
                revision_block.revision_id == ExperimentRun.revision_id,
                ~exists(
                    select(1)
                    .select_from(matching_result)
                    .where(
                        matching_result.run_id == ExperimentRun.id,
                        matching_result.block_index == revision_block.position + 1,
                        matching_result.completed_word_count
                        == revision_block.expected_word_count,
                    )
                    .correlate(ExperimentRun, revision_block)
                ),
            )
        )
        .correlate(ExperimentRun)
    )
    authoritative_revision_available = and_(
        ExperimentRun.revision_id.is_not(None),
        revision_block_count == ExperimentRun.block_count,
        revision_null_expected_count == 0,
    )
    authoritative_complete = and_(
        result_count == ExperimentRun.block_count,
        ~missing_or_mismatched_revision_result,
    )

    incomplete_legacy_result_exists = (
        exists(
            select(1)
            .select_from(RunResult)
            .where(
                RunResult.run_id == ExperimentRun.id,
                or_(
                    RunResult.block_completed.is_(False),
                    RunResult.experiment_completed.is_(False),
                ),
            )
        )
        .correlate(ExperimentRun)
    )
    completed_word_count = (
        select(func.coalesce(func.sum(RunResult.completed_word_count), 0))
        .where(RunResult.run_id == ExperimentRun.id)
        .correlate(ExperimentRun)
        .scalar_subquery()
    )
    expected_word_count = (
        select(func.coalesce(func.sum(RunResult.expected_word_count), 0))
        .where(RunResult.run_id == ExperimentRun.id)
        .correlate(ExperimentRun)
        .scalar_subquery()
    )
    legacy_complete = and_(
        result_count == ExperimentRun.block_count,
        ~incomplete_legacy_result_exists,
        completed_word_count == expected_word_count,
    )
    return case(
        (authoritative_revision_available, authoritative_complete),
        else_=legacy_complete,
    )


def _run_presence_expressions():
    has_raw_data = (
        exists(select(1).where(RunResult.run_id == ExperimentRun.id))
        .correlate(ExperimentRun)
    )

    def has_artifact(kind):
        return (
            exists(
                select(1).where(
                    RunArtifact.run_id == ExperimentRun.id,
                    RunArtifact.kind == kind,
                )
            )
            .correlate(ExperimentRun)
        )

    return (
        has_raw_data,
        has_artifact("analysis_csv"),
        has_artifact("trainable_json"),
    )


def _run_summaries(database, runs):
    """Fetch only the columns needed for compact list responses, in fixed queries."""
    if not runs:
        return {}
    run_ids = [run.id for run in runs]
    result_rows = database.execute(
        select(
            RunResult.run_id,
            RunResult.block_index,
            RunResult.block_completed,
            RunResult.experiment_completed,
            RunResult.completed_word_count,
            RunResult.expected_word_count,
        )
        .where(RunResult.run_id.in_(run_ids))
        .order_by(RunResult.run_id, RunResult.block_index)
    ).all()
    results_by_run = defaultdict(list)
    for result in result_rows:
        results_by_run[result.run_id].append(result)

    artifact_counts = defaultdict(lambda: defaultdict(int))
    for run_id, kind, count in database.execute(
        select(RunArtifact.run_id, RunArtifact.kind, func.count(RunArtifact.id))
        .where(RunArtifact.run_id.in_(run_ids))
        .group_by(RunArtifact.run_id, RunArtifact.kind)
    ).all():
        artifact_counts[run_id][kind] = int(count)

    summaries = {}
    for run in runs:
        results = results_by_run[run.id]
        revision_blocks = _authoritative_revision_blocks(run)
        summaries[run.id] = {
            "result_count": len(results),
            "raw_data_count": len(results),
            "analyzed_csv_count": artifact_counts[run.id]["analysis_csv"],
            "trainable_json_count": artifact_counts[run.id]["trainable_json"],
            "completed_word_count": sum(
                result.completed_word_count for result in results
            ),
            "expected_word_count": (
                sum(block.expected_word_count for block in revision_blocks)
                if revision_blocks is not None
                else sum(result.expected_word_count for result in results)
            ),
            "complete": _run_is_complete(run, results),
        }
    return summaries


def _owned_experiment(
    database,
    actor,
    experiment_id,
    *,
    lock=False,
    load_blocks=True,
):
    statement = (
        select(Experiment)
        .where(
            Experiment.id == experiment_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
    )
    if load_blocks:
        statement = statement.options(selectinload(Experiment.blocks))
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
    responses={
        200: {
            "description": (
                "Experiment runs as a JSON array. Pagination headers are emitted "
                "when limit is supplied."
            ),
            "headers": {
                "X-Total-Count": {
                    "description": "Total owner-scoped runs matching all filters.",
                    "schema": {"type": "integer", "minimum": 0},
                },
                "X-Next-Cursor": {
                    "description": (
                        "Opaque cursor for the next page; absent on the final page."
                    ),
                    "schema": {"type": "string"},
                },
            },
        }
    },
)
def list_experiment_runs(
    experiment_id: uuid.UUID,
    response: Response,
    participant_number: int | None = Query(
        default=None,
        ge=1,
        description="Return only runs for this participant number.",
    ),
    session_search: str | None = Query(
        default=None,
        max_length=128,
        description=(
            "Case-insensitive literal substring match against session_id. "
            "Percent and underscore characters are not wildcards."
        ),
    ),
    run_status: Literal[
        "created",
        "running",
        "completed",
        "incomplete",
        "failed",
        "cancelled",
    ]
    | None = Query(
        default=None,
        alias="status",
        description="Return only runs in this lifecycle status.",
    ),
    complete: bool | None = Query(
        default=None,
        description="Filter by computed raw-result completeness.",
    ),
    has_raw_data: bool | None = Query(
        default=None,
        description="Filter by whether at least one raw Block result exists.",
    ),
    has_analyzed_csv: bool | None = Query(
        default=None,
        description="Filter by whether an analyzed CSV artifact exists.",
    ),
    has_trainable_json: bool | None = Query(
        default=None,
        description="Filter by whether a trainable JSON artifact exists.",
    ),
    include_files: bool = Query(
        default=True,
        description=(
            "Include nested raw-result and artifact metadata. Set false for a "
            "compact list that retains accurate counts and completion totals."
        ),
    ),
    limit: int | None = Query(
        default=None,
        ge=1,
        le=200,
        description=(
            "Maximum number of runs to return. Omit for the legacy "
            "unpaginated array response."
        ),
    ),
    cursor: str | None = Query(
        default=None,
        min_length=1,
        max_length=1000,
        description=(
            "Opaque X-Next-Cursor value from the previous page. Requires limit."
        ),
    ),
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    if cursor is not None and limit is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="cursor requires limit.",
        )
    experiment = _owned_experiment(
        database,
        actor,
        experiment_id,
        load_blocks=False,
    )
    filters = [ExperimentRun.experiment_id == experiment.id]
    if participant_number is not None:
        filters.append(ExperimentRun.participant_number == participant_number)
    normalized_session_search = (session_search or "").strip()
    if normalized_session_search:
        filters.append(
            ExperimentRun.session_id.ilike(
                escaped_contains_pattern(normalized_session_search),
                escape="\\",
            )
        )
    if run_status is not None:
        filters.append(ExperimentRun.status == run_status)

    has_raw_expression, has_csv_expression, has_json_expression = (
        _run_presence_expressions()
    )
    if complete is not None:
        filters.append(_run_complete_sql_expression().is_(complete))
    for requested, expression in (
        (has_raw_data, has_raw_expression),
        (has_analyzed_csv, has_csv_expression),
        (has_trainable_json, has_json_expression),
    ):
        if requested is not None:
            filters.append(expression if requested else ~expression)

    cursor_position = decode_cursor(cursor) if cursor is not None else None
    if limit is not None:
        total_count = database.scalar(
            select(func.count()).select_from(ExperimentRun).where(*filters)
        )
        response.headers["X-Total-Count"] = str(total_count or 0)

    page_filters = list(filters)
    if cursor_position is not None:
        cursor_created_at, cursor_id = cursor_position
        page_filters.append(
            or_(
                ExperimentRun.created_at < cursor_created_at,
                and_(
                    ExperimentRun.created_at == cursor_created_at,
                    ExperimentRun.id < cursor_id,
                ),
            )
        )

    statement = (
        select(ExperimentRun)
        .where(*page_filters)
        .options(
            selectinload(ExperimentRun.revision).selectinload(ExperimentRevision.blocks),
        )
        .order_by(ExperimentRun.created_at.desc(), ExperimentRun.id.desc())
    )
    if include_files:
        statement = statement.options(
            selectinload(ExperimentRun.results),
            selectinload(ExperimentRun.artifacts),
        )
    if limit is not None:
        statement = statement.limit(limit + 1)
    runs = list(database.scalars(statement).all())
    has_next_page = limit is not None and len(runs) > limit
    if has_next_page:
        runs = runs[:limit]
        last_run = runs[-1]
        response.headers["X-Next-Cursor"] = encode_cursor(
            last_run.created_at,
            last_run.id,
        )

    summaries = {} if include_files else _run_summaries(database, runs)
    return [
        _run_response(
            run,
            include_files=include_files,
            summary=summaries.get(run.id),
        )
        for run in runs
    ]


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
