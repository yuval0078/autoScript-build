import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..database import get_db
from ..dependencies import require_researcher
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
    RunAnalysisCopyResponse,
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
    del actor
    statement = (
        select(ExperimentRun)
        .join(Experiment, ExperimentRun.experiment_id == Experiment.id)
        .where(
            ExperimentRun.id == run_id,
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


def _analysis_copy_response(run, revision):
    artifacts = {artifact.kind: artifact for artifact in revision.artifacts}
    analyzed_csv = artifacts.get("analysis_csv")
    trainable_json = artifacts.get("trainable_json")
    if analyzed_csv is None or trainable_json is None:
        raise HTTPException(
            status_code=500,
            detail="Finalized analysis revision has incomplete export artifacts.",
        )
    return RunAnalysisCopyResponse(
        id=revision.id,
        run_id=revision.run_id,
        revision=revision.revision_number,
        created_at=revision.created_at,
        completed=bool(revision.completed),
        is_current_editable=run.current_analysis_revision_id == revision.id,
        analyzed_csv=_artifact_response(analyzed_csv),
        trainable_json=_artifact_response(trainable_json),
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


def _parse_existing_analysis_policy(value):
    policy = str(value or "keep").strip().lower()
    if policy not in {"keep", "replace"}:
        raise HTTPException(
            status_code=400,
            detail="X-Existing-Analysis-Policy must be 'keep' or 'replace'.",
        )
    return policy


def _finalize_operation_sha256(request_sha256, policy):
    # Preserve the original digest for the default policy so idempotency keys written
    # by older servers remain retryable after this feature is deployed.
    if policy == "keep":
        return request_sha256
    return hashlib.sha256(f"replace\0{request_sha256}".encode("ascii")).hexdigest()


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


def _finalized_analysis_copy(run, revision_id):
    revision = next(
        (
            item
            for item in run.analysis_revisions
            if item.id == revision_id and item.finalized
        ),
        None,
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Analysis copy was not found.")
    return revision


@router.get(
    "/runs/{run_id}/analysis-copies",
    response_model=list[RunAnalysisCopyResponse],
    summary="List saved analyzed copies",
    response_description="Newest-first immutable analyzed copies for the participant Run.",
    description=(
        "Returns every finalized analysis revision in the shared lab workspace. "
        "Each item groups the analyzed CSV and trainable JSON that were produced "
        "together, reports its creation time, and identifies the revision currently "
        "used to restore Analyzer editing state."
    ),
    responses={
        401: {"description": "Authentication is required or the token is invalid."},
        404: {"description": "The Run is not visible to the authenticated user."},
    },
)
def list_run_analysis_copies(
    run_id: uuid.UUID,
    database: Session = Depends(get_db),
    actor: User = Depends(require_researcher),
):
    run = _owned_analysis_run(database, actor, run_id)
    revisions = sorted(
        (revision for revision in run.analysis_revisions if revision.finalized),
        key=lambda revision: revision.revision_number,
        reverse=True,
    )
    return [_analysis_copy_response(run, revision) for revision in revisions]


@router.post(
    "/runs/{run_id}/analysis-copies/{revision_id}/set-editable",
    response_model=RunAnalysisRevisionResponse,
    summary="Use an analyzed copy as the editable state",
    response_description="The selected revision and its new concurrency ETag.",
    description=(
        "Makes the analysis-state snapshot stored alongside the selected trainable "
        "JSON the current editable state. The next Analyzer launch restores that "
        "snapshot. No CSV, JSON, or raw result bytes are modified."
    ),
    responses={
        401: {"description": "Authentication is required or the token is invalid."},
        404: {"description": "The Run or finalized analysis copy was not found."},
        409: {"description": "The copy has no restorable state snapshot."},
    },
)
def set_run_analysis_copy_editable(
    run_id: uuid.UUID,
    revision_id: uuid.UUID,
    response: Response,
    database: Session = Depends(get_db),
    actor: User = Depends(require_researcher),
):
    run = _owned_analysis_run(database, actor, run_id, lock=True)
    revision = _finalized_analysis_copy(run, revision_id)
    artifact_kinds = {artifact.kind for artifact in revision.artifacts}
    if not {"analysis_state", "trainable_json"}.issubset(artifact_kinds):
        raise HTTPException(
            status_code=409,
            detail="This analysis copy cannot restore an editable state.",
        )
    run.current_analysis_revision = revision
    run.analysis_completed = revision.completed
    run.analysis_updated_at = datetime.now(timezone.utc)
    database.commit()
    _set_analysis_headers(response, revision)
    return _analysis_response(revision)


@router.delete(
    "/runs/{run_id}/analysis-copies/{revision_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one analyzed copy",
    response_description="The analyzed copy was deleted.",
    description=(
        "Deletes the selected finalized revision, its analyzed CSV, trainable JSON, "
        "and matching edit-state snapshot. Raw Runner data is never deleted. If the "
        "copy was current, the newest remaining restorable revision becomes current."
    ),
    responses={
        401: {"description": "Authentication is required or the token is invalid."},
        404: {"description": "The Run or finalized analysis copy was not found."},
    },
)
def delete_run_analysis_copy(
    run_id: uuid.UUID,
    revision_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(require_researcher),
):
    run = _owned_analysis_run(database, actor, run_id, lock=True)
    revision = _finalized_analysis_copy(run, revision_id)
    storage_keys = [artifact.storage_key for artifact in revision.artifacts]

    if run.current_analysis_revision_id == revision.id:
        remaining = [
            candidate
            for candidate in run.analysis_revisions
            if candidate.id != revision.id and _state_artifact(candidate) is not None
        ]
        fallback = max(
            remaining,
            key=lambda candidate: candidate.revision_number,
            default=None,
        )
        run.current_analysis_revision = fallback
        if fallback is None:
            run.analysis_completed = (
                False if _legacy_state_artifact(run) is not None else None
            )
        else:
            run.analysis_completed = fallback.completed if fallback.finalized else False
        run.analysis_updated_at = datetime.now(timezone.utc)
        # Persist the pointer change before deleting the referenced revision. This is
        # required by databases that enforce the FK immediately.
        database.flush()

    database.execute(
        RunAnalysisOperation.__table__.update()
        .where(RunAnalysisOperation.revision_id == revision.id)
        .values(revision_id=None)
    )
    queue_object_deletions(database, storage_keys)
    database.delete(revision)
    database.commit()
    drain_object_deletions(database, storage)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runs/{run_id}/analysis-state")
def get_run_analysis_state(
    run_id: uuid.UUID,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(require_researcher),
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
    actor: User = Depends(require_researcher),
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
    x_existing_analysis_policy: str = Header(
        default="keep",
        alias="X-Existing-Analysis-Policy",
        description=(
            "How to handle older finalized copies: 'keep' appends a new immutable "
            "copy; 'replace' atomically retains only the newly finalized copy."
        ),
    ),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(require_researcher),
):
    del x_filename
    request_id = _parse_request_id(x_idempotency_key)
    existing_analysis_policy = _parse_existing_analysis_policy(
        x_existing_analysis_policy
    )
    response.headers["X-Existing-Analysis-Policy"] = existing_analysis_policy
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
        raise HTTPException(
            status_code=415,
            detail="Analysis finalization must use application/zip.",
        )
    upload_path, request_sha256, _bundle_size = await _receive_body(
        request, ".zip", "Analysis finalization ZIP"
    )
    operation_sha256 = _finalize_operation_sha256(
        request_sha256, existing_analysis_policy
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
                database, run, request_id, "finalize", operation_sha256
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
                request_sha256=operation_sha256,
                revision_id=revision.id,
                revision_number=revision.revision_number,
            )
            database.add_all([revision, *artifacts, operation])
            # See the draft-save path above: this preserves FK ordering on PostgreSQL.
            run.current_analysis_revision = revision
            run.analysis_completed = bundle.completed
            run.analysis_updated_at = now
            database.flush()
            if existing_analysis_policy == "replace":
                replaced_revisions = [
                    candidate
                    for candidate in run.analysis_revisions
                    if candidate.finalized and candidate.id != revision.id
                ]
                replaced_storage_keys = [
                    artifact.storage_key
                    for candidate in replaced_revisions
                    for artifact in candidate.artifacts
                ]
                replaced_revision_ids = [
                    candidate.id for candidate in replaced_revisions
                ]
                if replaced_revision_ids:
                    database.execute(
                        RunAnalysisOperation.__table__.update()
                        .where(
                            RunAnalysisOperation.revision_id.in_(
                                replaced_revision_ids
                            )
                        )
                        .values(revision_id=None)
                    )
                    for candidate in replaced_revisions:
                        database.delete(candidate)
                    queue_object_deletions(database, replaced_storage_keys)
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
            database, run, request_id, "finalize", operation_sha256
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
    actor: User = Depends(require_researcher),
):
    results = database.scalars(
        select(RunResult)
        .join(ExperimentRun, RunResult.run_id == ExperimentRun.id)
        .join(Experiment, ExperimentRun.experiment_id == Experiment.id)
        .where(
            RunResult.sha256.in_(payload.sha256),
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
