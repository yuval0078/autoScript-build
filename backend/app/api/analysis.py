import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_user
from ..models import (
    Experiment,
    ExperimentRun,
    RunAnalysisOperation,
    RunAnalysisRevision,
    RunArtifact,
    RunResult,
    User,
)
from ..schemas import (
    RunAnalysisRevisionResponse,
    RunArtifactResponse,
    RunResultResolveRequest,
    RunResultResolveResponse,
)
from ..services.analysis import (
    AnalysisValidationError,
    analysis_source_fingerprint,
    validate_analysis_bundle,
    validate_analysis_state,
)
from ..services.object_cleanup import (
    drain_object_deletions,
    queue_object_deletions,
)
from ..services.storage import get_object_storage


router = APIRouter(prefix="/api/v1", tags=["analysis"])
ANALYSIS_CONTENT_TYPES = {
    "analysis_state": "application/json",
    "analysis_csv": "text/csv; charset=utf-8",
    "trainable_json": "application/json",
}
ANALYSIS_SUFFIXES = {
    "analysis_state": ".json",
    "analysis_csv": ".csv",
    "trainable_json": ".json",
}


def _owned_analysis_run(database, actor, run_id, *, lock=False):
    statement = (
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
            selectinload(ExperimentRun.analysis_revisions).selectinload(
                RunAnalysisRevision.artifacts
            ),
            selectinload(ExperimentRun.current_analysis_revision).selectinload(
                RunAnalysisRevision.artifacts
            ),
        )
    )
    if lock:
        statement = statement.with_for_update()
    run = database.scalar(statement)
    if run is None:
        raise HTTPException(status_code=404, detail="Experiment run was not found.")
    return run


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


def _state_artifact(revision):
    return next(
        (artifact for artifact in revision.artifacts if artifact.kind == "analysis_state"),
        None,
    )


def _analysis_etag(revision, state_artifact):
    return f'"analysis-{revision.revision_number}-{state_artifact.sha256}"'


def _legacy_state_artifact(run):
    candidates = [
        artifact
        for artifact in run.artifacts
        if artifact.kind == "analysis_state" and artifact.analysis_revision_id is None
    ]
    return max(candidates, key=lambda item: item.created_at, default=None)


def _legacy_etag(artifact):
    return f'"analysis-legacy-{artifact.sha256}"'


def _analysis_response(revision):
    state_artifact = _state_artifact(revision)
    if state_artifact is None:
        raise HTTPException(status_code=500, detail="Analysis revision has no state artifact.")
    return RunAnalysisRevisionResponse(
        id=revision.id,
        run_id=revision.run_id,
        revision=revision.revision_number,
        etag=_analysis_etag(revision, state_artifact),
        source_fingerprint=revision.source_fingerprint,
        finalized=revision.finalized,
        completed=revision.completed,
        created_at=revision.created_at,
        finalized_at=revision.finalized_at,
        artifacts=[
            _artifact_response(artifact)
            for artifact in sorted(revision.artifacts, key=lambda item: item.kind)
        ],
    )


def _set_analysis_headers(response, revision):
    state_artifact = _state_artifact(revision)
    if state_artifact is None:
        raise HTTPException(status_code=500, detail="Analysis revision has no state artifact.")
    response.headers["ETag"] = _analysis_etag(revision, state_artifact)
    response.headers["X-Analysis-Revision"] = str(revision.revision_number)
    response.headers["X-Checksum-SHA256"] = state_artifact.sha256
    response.headers["X-Source-Fingerprint"] = revision.source_fingerprint


def _parse_request_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="X-Idempotency-Key must be a UUID.",
        ) from exc


def _safe_filename(value, default, suffix):
    filename = Path(unquote(value or default)).name
    if not filename.lower().endswith(suffix):
        filename = f"{filename}{suffix}"
    return (filename or default)[:255]


async def _receive_body(request, suffix, label):
    settings = get_settings()
    descriptor, raw_path = tempfile.mkstemp(prefix="autoscript-analysis-", suffix=suffix)
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
                    raise HTTPException(status_code=413, detail=f"{label} is too large.")
                digest.update(chunk)
                output.write(chunk)
        if size_bytes == 0:
            raise HTTPException(status_code=422, detail=f"{label} is empty.")
        return upload_path, digest.hexdigest(), size_bytes
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise


def _current_precondition_resource(run):
    revision = run.current_analysis_revision
    if revision is not None:
        state = _state_artifact(revision)
        if state is None:
            raise HTTPException(status_code=500, detail="Current analysis state is unavailable.")
        return revision, _analysis_etag(revision, state)
    legacy = _legacy_state_artifact(run)
    if legacy is not None:
        return legacy, _legacy_etag(legacy)
    return None, None


def _enforce_precondition(run, if_match, if_none_match):
    if if_match is not None and if_none_match is not None:
        raise HTTPException(
            status_code=400,
            detail="Send If-Match or If-None-Match, not both.",
        )
    current, current_etag = _current_precondition_resource(run)
    if if_match is None and if_none_match is None:
        raise HTTPException(
            status_code=428,
            detail="An If-Match or If-None-Match precondition is required.",
        )
    if current is None:
        if if_none_match != "*":
            raise HTTPException(status_code=412, detail="Analysis state does not exist.")
        return
    if if_match != current_etag:
        raise HTTPException(
            status_code=412,
            detail="Analysis state changed; reload it before saving.",
            headers={"ETag": current_etag},
        )


def _idempotent_revision(database, run, request_id, operation_kind, request_sha256):
    operation = database.scalar(
        select(RunAnalysisOperation).where(
            RunAnalysisOperation.run_id == run.id,
            RunAnalysisOperation.request_id == request_id,
        )
    )
    if operation is None:
        return None
    if (
        operation.operation_kind != operation_kind
        or operation.request_sha256 != request_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="This analysis idempotency key was already used for a different request.",
        )
    if operation.revision_id is None:
        raise HTTPException(
            status_code=410,
            detail="The idempotent draft response has expired under the retention policy.",
        )
    revision = database.scalar(
        select(RunAnalysisRevision)
        .where(RunAnalysisRevision.id == operation.revision_id)
        .options(selectinload(RunAnalysisRevision.artifacts))
    )
    if revision is None:
        raise HTTPException(
            status_code=410,
            detail="The idempotent draft response has expired under the retention policy.",
        )
    return revision


def _next_analysis_revision(database, run_id):
    return (
        database.scalar(
            select(func.max(RunAnalysisOperation.revision_number)).where(
                RunAnalysisOperation.run_id == run_id
            )
        )
        or 0
    ) + 1


def _new_artifact(run, revision, actor, kind, path, sha256, size_bytes):
    artifact = RunArtifact(
        id=uuid.uuid4(),
        run_id=run.id,
        analysis_revision_id=revision.id,
        kind=kind,
        storage_key="pending",
        original_filename={
            "analysis_state": "analysis_state.json",
            "analysis_csv": "analysis.csv",
            "trainable_json": "trainable.json",
        }[kind],
        sha256=sha256,
        size_bytes=size_bytes,
        created_by=actor.id,
    )
    artifact.storage_key = (
        f"experiments/{run.experiment_id}/runs/{run.id}/analysis/"
        f"{revision.revision_number}/{revision.id}/{kind}/{artifact.id}/"
        f"{sha256}{ANALYSIS_SUFFIXES[kind]}"
    )
    return artifact


def _prune_analysis_drafts(database, run):
    retention = get_settings().analysis_draft_retention
    drafts = database.scalars(
        select(RunAnalysisRevision)
        .where(
            RunAnalysisRevision.run_id == run.id,
            RunAnalysisRevision.finalized.is_(False),
        )
        .options(selectinload(RunAnalysisRevision.artifacts))
        .order_by(RunAnalysisRevision.revision_number.desc())
    ).all()
    storage_keys = []
    for revision in drafts[retention:]:
        storage_keys.extend(artifact.storage_key for artifact in revision.artifacts)
        database.execute(
            RunAnalysisOperation.__table__.update()
            .where(RunAnalysisOperation.revision_id == revision.id)
            .values(revision_id=None)
        )
        for artifact in list(revision.artifacts):
            database.delete(artifact)
        database.delete(revision)
    queue_object_deletions(database, storage_keys)


def _cleanup_uncommitted_objects(database, storage, storage_keys):
    database.rollback()
    if not storage_keys:
        return
    queue_object_deletions(database, storage_keys)
    database.commit()
    drain_object_deletions(database, storage)


@router.get("/runs/{run_id}/analysis-state")
def get_run_analysis_state(
    run_id: uuid.UUID,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    run = _owned_analysis_run(database, actor, run_id)
    revision = run.current_analysis_revision
    if revision is not None:
        state_artifact = _state_artifact(revision)
        if state_artifact is None:
            raise HTTPException(status_code=500, detail="Current analysis state is unavailable.")
        revision_number = revision.revision_number
        etag = _analysis_etag(revision, state_artifact)
        source_fingerprint = revision.source_fingerprint
        modified_at = revision.created_at
    else:
        state_artifact = _legacy_state_artifact(run)
        if state_artifact is None:
            raise HTTPException(status_code=404, detail="Analysis state was not found.")
        revision_number = 0
        etag = _legacy_etag(state_artifact)
        source_fingerprint = analysis_source_fingerprint(run.results)
        modified_at = state_artifact.created_at
    if modified_at.tzinfo is None:
        modified_at = modified_at.replace(tzinfo=timezone.utc)
    common_headers = {
        "ETag": etag,
        "Last-Modified": format_datetime(modified_at.astimezone(timezone.utc), usegmt=True),
        "Cache-Control": "no-store",
        "X-Analysis-Revision": str(revision_number),
        "X-Checksum-SHA256": state_artifact.sha256,
        "X-Source-Fingerprint": source_fingerprint,
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=common_headers)
    return StreamingResponse(
        storage.iter_object(state_artifact.storage_key),
        media_type="application/json",
        headers={**common_headers, "Content-Length": str(state_artifact.size_bytes)},
    )


@router.put(
    "/runs/{run_id}/analysis-state",
    response_model=RunAnalysisRevisionResponse,
)
async def put_run_analysis_state(
    run_id: uuid.UUID,
    request: Request,
    response: Response,
    x_idempotency_key: str = Header(alias="X-Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    del x_filename  # The canonical persisted state name is stable across clients.
    request_id = _parse_request_id(x_idempotency_key)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Analysis state must use application/json.")
    upload_path, request_sha256, size_bytes = await _receive_body(
        request, ".json", "Analysis state"
    )
    storage_keys = []
    committed = False
    try:
        run = _owned_analysis_run(database, actor, run_id, lock=True)
        if not run.results:
            raise HTTPException(status_code=409, detail="Run has no immutable results to analyze.")
        try:
            validated = validate_analysis_state(upload_path, run)
        except AnalysisValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        prior = _idempotent_revision(
            database, run, request_id, "state", request_sha256
        )
        if prior is not None:
            _set_analysis_headers(response, prior)
            return _analysis_response(prior)
        _enforce_precondition(run, if_match, if_none_match)

        revision = RunAnalysisRevision(
            id=uuid.uuid4(),
            run_id=run.id,
            revision_number=_next_analysis_revision(database, run.id),
            source_fingerprint=validated.source_fingerprint,
            finalized=False,
            completed=None,
            finalized_at=None,
            created_by=actor.id,
        )
        artifact = _new_artifact(
            run,
            revision,
            actor,
            "analysis_state",
            upload_path,
            validated.sha256,
            size_bytes,
        )
        storage_keys.append(artifact.storage_key)
        storage.put_file(artifact.storage_key, upload_path, "application/json")
        operation = RunAnalysisOperation(
            id=uuid.uuid4(),
            run_id=run.id,
            request_id=request_id,
            operation_kind="state",
            request_sha256=request_sha256,
            revision_id=revision.id,
            revision_number=revision.revision_number,
        )
        database.add_all([revision, artifact, operation])
        # Assign the relationship, rather than only the scalar FK.  The relationship is
        # configured with ``post_update`` so SQLAlchemy inserts the new revision before
        # pointing the Run at it.  Setting only the UUID lets PostgreSQL emit the Run
        # UPDATE first and violates the immediate foreign-key constraint.
        run.current_analysis_revision = revision
        run.analysis_completed = False
        run.analysis_updated_at = datetime.now(timezone.utc)
        database.flush()
        _prune_analysis_drafts(database, run)
        database.commit()
        committed = True
        database.refresh(revision)
        drain_object_deletions(database, storage)
        _set_analysis_headers(response, revision)
        return _analysis_response(revision)
    except IntegrityError as exc:
        if committed:
            raise
        _cleanup_uncommitted_objects(database, storage, storage_keys)
        run = _owned_analysis_run(database, actor, run_id)
        prior = _idempotent_revision(
            database, run, request_id, "state", request_sha256
        )
        if prior is None:
            raise HTTPException(status_code=409, detail="Concurrent analysis save conflict.") from exc
        _set_analysis_headers(response, prior)
        return _analysis_response(prior)
    except Exception:
        if storage_keys and not committed:
            _cleanup_uncommitted_objects(database, storage, storage_keys)
        else:
            database.rollback()
        raise
    finally:
        upload_path.unlink(missing_ok=True)


@router.post(
    "/runs/{run_id}/analysis/finalize",
    response_model=RunAnalysisRevisionResponse,
)
async def finalize_run_analysis(
    run_id: uuid.UUID,
    request: Request,
    response: Response,
    x_idempotency_key: str = Header(alias="X-Idempotency-Key"),
    if_match: str | None = Header(default=None, alias="If-Match"),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    del x_filename
    request_id = _parse_request_id(x_idempotency_key)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
        raise HTTPException(
            status_code=415,
            detail="Analysis finalization must use application/zip.",
        )
    upload_path, request_sha256, _bundle_size = await _receive_body(
        request, ".zip", "Analysis finalization ZIP"
    )
    storage_keys = []
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix="autoscript-analysis-finalize-") as temp_dir:
            run = _owned_analysis_run(database, actor, run_id, lock=True)
            if not run.results:
                raise HTTPException(status_code=409, detail="Run has no immutable results to analyze.")
            try:
                bundle = validate_analysis_bundle(
                    upload_path,
                    Path(temp_dir),
                    run,
                    max_uncompressed_bytes=get_settings().max_uncompressed_package_bytes,
                )
            except AnalysisValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            prior = _idempotent_revision(
                database, run, request_id, "finalize", request_sha256
            )
            if prior is not None:
                _set_analysis_headers(response, prior)
                return _analysis_response(prior)
            _enforce_precondition(run, if_match, if_none_match)

            now = datetime.now(timezone.utc)
            revision = RunAnalysisRevision(
                id=uuid.uuid4(),
                run_id=run.id,
                revision_number=_next_analysis_revision(database, run.id),
                source_fingerprint=bundle.source_fingerprint,
                finalized=True,
                completed=bundle.completed,
                finalized_at=now,
                created_by=actor.id,
            )
            artifact_specs = (
                ("analysis_state", "analysis_state.json"),
                ("analysis_csv", "analysis.csv"),
                ("trainable_json", "trainable.json"),
            )
            artifacts = []
            for kind, filename in artifact_specs:
                artifact = _new_artifact(
                    run,
                    revision,
                    actor,
                    kind,
                    bundle.paths[filename],
                    bundle.sha256[filename],
                    bundle.size_bytes[filename],
                )
                storage_keys.append(artifact.storage_key)
                storage.put_file(
                    artifact.storage_key,
                    bundle.paths[filename],
                    ANALYSIS_CONTENT_TYPES[kind],
                )
                artifacts.append(artifact)
            operation = RunAnalysisOperation(
                id=uuid.uuid4(),
                run_id=run.id,
                request_id=request_id,
                operation_kind="finalize",
                request_sha256=request_sha256,
                revision_id=revision.id,
                revision_number=revision.revision_number,
            )
            database.add_all([revision, *artifacts, operation])
            # See the draft-save path above: this preserves FK ordering on PostgreSQL.
            run.current_analysis_revision = revision
            run.analysis_completed = bundle.completed
            run.analysis_updated_at = now
            database.flush()
            _prune_analysis_drafts(database, run)
            database.commit()
            committed = True
            database.refresh(revision)
            drain_object_deletions(database, storage)
            _set_analysis_headers(response, revision)
            return _analysis_response(revision)
    except IntegrityError as exc:
        if committed:
            raise
        _cleanup_uncommitted_objects(database, storage, storage_keys)
        run = _owned_analysis_run(database, actor, run_id)
        prior = _idempotent_revision(
            database, run, request_id, "finalize", request_sha256
        )
        if prior is None:
            raise HTTPException(status_code=409, detail="Concurrent analysis finalization conflict.") from exc
        _set_analysis_headers(response, prior)
        return _analysis_response(prior)
    except Exception:
        if storage_keys and not committed:
            _cleanup_uncommitted_objects(database, storage, storage_keys)
        else:
            database.rollback()
        raise
    finally:
        upload_path.unlink(missing_ok=True)


@router.post(
    "/run-results/resolve",
    response_model=RunResultResolveResponse,
)
def resolve_run_results(
    payload: RunResultResolveRequest,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    results = database.scalars(
        select(RunResult)
        .join(ExperimentRun, RunResult.run_id == ExperimentRun.id)
        .join(Experiment, ExperimentRun.experiment_id == Experiment.id)
        .where(
            RunResult.sha256.in_(payload.sha256),
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
        .order_by(RunResult.created_at, RunResult.block_index)
    ).all()
    order = {digest: index for index, digest in enumerate(payload.sha256)}
    results = sorted(results, key=lambda item: (order[item.sha256], item.block_index))
    found = {result.sha256 for result in results}
    return RunResultResolveResponse(
        results=[
            {
                "id": result.id,
                "run_id": result.run_id,
                "block_id": result.block_id,
                "block_index": result.block_index,
                "block_count": result.block_count,
                "block_name": result.block_name,
                "block_completed": result.block_completed,
                "experiment_completed": result.experiment_completed,
                "completed_word_count": result.completed_word_count,
                "expected_word_count": result.expected_word_count,
                "schema_version": result.schema_version,
                "app_version": result.app_version,
                "result_timestamp": result.result_timestamp,
                "original_filename": result.original_filename,
                "sha256": result.sha256,
                "size_bytes": result.size_bytes,
                "created_at": result.created_at,
                "download_url": f"/api/v1/run-results/{result.id}/download",
            }
            for result in results
        ],
        missing_sha256=[digest for digest in payload.sha256 if digest not in found],
    )
