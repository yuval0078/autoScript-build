import hashlib
import json
import os
import re
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.background import BackgroundTask

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_user
from ..models import (
    Experiment,
    ExperimentBlock,
    ExperimentPublishOperation,
    ExperimentRun,
    ExperimentRevision,
    ExperimentRevisionBlock,
    ExperimentVersion,
    RunArtifact,
    RunResult,
    StagedBlockAsset,
    User,
)
from ..schemas import (
    BlockReorder,
    ExperimentBlockResponse,
    ExperimentCreate,
    ExperimentPublishCreate,
    ExperimentPublishUpdate,
    ExperimentResponse,
    ExperimentRevisionResponse,
    ExperimentUpdate,
    ExperimentVersionResponse,
    StagedBlockAssetResponse,
)
from ..services.experiment_packages import (
    PackageValidationError,
    validate_block_package,
)
from ..services.object_cleanup import drain_object_deletions, queue_object_deletions
from ..services.storage import get_object_storage


router = APIRouter(prefix="/api/v1", tags=["experiments"])


def _version_response(version):
    return ExperimentVersionResponse(
        id=version.id,
        experiment_id=version.experiment_id,
        version_number=version.version_number,
        schema_version=version.schema_version,
        app_version=version.app_version,
        original_filename=version.original_filename,
        sha256=version.sha256,
        size_bytes=version.size_bytes,
        created_at=version.created_at,
        download_url=f"/api/v1/experiment-versions/{version.id}/download",
    )


def _block_response(block):
    return ExperimentBlockResponse(
        id=block.id,
        experiment_id=block.experiment_id,
        position=block.position,
        same_page_as_previous=block.same_page_as_previous,
        name=block.name,
        schema_version=block.schema_version,
        app_version=block.app_version,
        expected_word_count=block.expected_word_count,
        grid_rows=block.grid_rows,
        grid_cols=block.grid_cols,
        original_filename=block.original_filename,
        sha256=block.sha256,
        size_bytes=block.size_bytes,
        created_at=block.created_at,
        download_url=f"/api/v1/blocks/{block.id}/download",
    )


def _experiment_response(
    experiment,
    participant_count=None,
    analyzed_participant_count=None,
):
    versions = sorted(
        experiment.versions,
        key=lambda version: version.version_number,
        reverse=True,
    )
    blocks = sorted(experiment.blocks, key=lambda block: block.position)
    current_revision = next(
        (
            item
            for item in experiment.revisions
            if item.id == experiment.current_revision_id
        ),
        None,
    )
    if current_revision is None and experiment.current_revision_id is None:
        # Compatibility for legacy rows created before the explicit pointer.
        current_revision = max(
            experiment.revisions,
            key=lambda item: item.revision_number,
            default=None,
        )
    if participant_count is None:
        participant_count = len(
            {run.participant_number for run in experiment.runs}
        )
    if analyzed_participant_count is None:
        analyzed_participant_count = len(
            {
                run.participant_number
                for run in experiment.runs
                if run.analysis_completed is True
            }
        )
    return ExperimentResponse(
        id=experiment.id,
        name=experiment.name,
        description=experiment.description,
        owner_id=experiment.owner_id,
        created_at=experiment.created_at,
        current_revision_id=(
            experiment.current_revision_id
            or (current_revision.id if current_revision is not None else None)
        ),
        blocks=[_block_response(block) for block in blocks],
        versions=[_version_response(version) for version in versions],
        current_revision=_revision_response(current_revision) if current_revision else None,
        participant_count=participant_count,
        analyzed_participant_count=analyzed_participant_count,
        download_url=f"/api/v1/experiments/{experiment.id}/download",
    )


def _revision_response(revision):
    blocks = sorted(revision.blocks, key=lambda block: block.position)
    return {
        "id": revision.id,
        "experiment_id": revision.experiment_id,
        "revision_number": revision.revision_number,
        "name": revision.name,
        "created_at": revision.created_at,
        "blocks": [
            {
                "id": block.id,
                "source_block_id": block.source_block_id,
                "position": block.position,
                "same_page_as_previous": block.same_page_as_previous,
                "name": block.name,
                "expected_word_count": block.expected_word_count,
                "grid_rows": block.grid_rows,
                "grid_cols": block.grid_cols,
                "sha256": block.sha256,
                "size_bytes": block.size_bytes,
            }
            for block in blocks
        ],
        "download_url": f"/api/v1/experiment-revisions/{revision.id}/download",
    }


def _experiment_query(actor, experiment_id, *, lock=False):
    statement = (
        select(Experiment)
        .where(
            Experiment.id == experiment_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
        .options(
            selectinload(Experiment.blocks),
            selectinload(Experiment.versions),
            selectinload(Experiment.revisions).selectinload(ExperimentRevision.blocks),
            selectinload(Experiment.runs).selectinload(ExperimentRun.results),
            selectinload(Experiment.runs).selectinload(ExperimentRun.artifacts),
        )
    )
    return statement.with_for_update() if lock else statement


def _owned_experiment(database, actor, experiment_id, *, lock=False):
    experiment = database.scalar(_experiment_query(actor, experiment_id, lock=lock))
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment was not found.")
    return experiment


def _safe_zip_filename(raw_filename, default="block.zip"):
    filename = unquote(raw_filename or default)
    if (
        len(filename) > 255
        or "/" in filename
        or "\\" in filename
        or Path(filename).name != filename
        or not filename.lower().endswith(".zip")
    ):
        raise HTTPException(status_code=400, detail="X-Filename must be a ZIP filename.")
    return filename


def _safe_block_name(raw_name):
    name = unquote(raw_name).strip()
    if not name or len(name) > 200:
        raise HTTPException(
            status_code=400,
            detail="X-Block-Name must contain between 1 and 200 characters.",
        )
    return name


async def _receive_package(request):
    settings = get_settings()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="Block package is too large.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from exc

    descriptor, raw_path = tempfile.mkstemp(prefix="autoscript-block-", suffix=".zip")
    os.close(descriptor)
    upload_path = Path(raw_path)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with upload_path.open("wb") as package_file:
            async for chunk in request.stream():
                size_bytes += len(chunk)
                if size_bytes > settings.max_upload_bytes:
                    raise HTTPException(status_code=413, detail="Block package is too large.")
                digest.update(chunk)
                package_file.write(chunk)
        if size_bytes == 0:
            raise HTTPException(status_code=400, detail="Block package is empty.")
        try:
            metadata = validate_block_package(
                upload_path,
                settings.max_uncompressed_package_bytes,
            )
        except PackageValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return upload_path, digest.hexdigest(), size_bytes, metadata
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise


def _move_blocks(database, blocks):
    """Assign dense positions without tripping an immediate unique constraint."""
    for index, block in enumerate(blocks):
        block.position = 10_000_000 + index
    database.flush()
    for index, block in enumerate(blocks):
        block.position = index
    if blocks:
        blocks[0].same_page_as_previous = False


def _storage_key_is_referenced(database, storage_key):
    return bool(
        database.scalar(
            select(func.count(ExperimentBlock.id)).where(
                ExperimentBlock.storage_key == storage_key
            )
        )
        or database.scalar(
            select(func.count(StagedBlockAsset.id)).where(
                StagedBlockAsset.storage_key == storage_key
            )
        )
        or database.scalar(
            select(func.count(ExperimentRevisionBlock.id)).where(
                ExperimentRevisionBlock.storage_key == storage_key
            )
        )
        or database.scalar(
            select(func.count(ExperimentVersion.id)).where(
                ExperimentVersion.storage_key == storage_key
            )
        )
        or database.scalar(
            select(func.count(RunResult.id)).where(
                RunResult.storage_key == storage_key
            )
        )
        or database.scalar(
            select(func.count(RunArtifact.id)).where(
                RunArtifact.storage_key == storage_key
            )
        )
    )


def _staged_block_response(asset):
    return StagedBlockAssetResponse(
        id=asset.id,
        request_id=asset.request_id,
        name=asset.name,
        schema_version=asset.schema_version,
        app_version=asset.app_version,
        expected_word_count=asset.expected_word_count,
        grid_rows=asset.grid_rows,
        grid_cols=asset.grid_cols,
        original_filename=asset.original_filename,
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        created_at=asset.created_at,
        expires_at=asset.expires_at,
    )


def _cleanup_expired_staged_blocks(database, storage, owner_id):
    """Delete expired staging rows first, then best-effort delete their objects."""
    now = datetime.now(timezone.utc)
    expired = database.scalars(
        select(StagedBlockAsset).where(
            StagedBlockAsset.owner_id == owner_id,
            StagedBlockAsset.expires_at <= now,
        )
    ).all()
    if not expired:
        return
    storage_keys = [asset.storage_key for asset in expired]
    for asset in expired:
        database.delete(asset)
    database.commit()
    for storage_key in storage_keys:
        if not _storage_key_is_referenced(database, storage_key):
            try:
                storage.remove_object(storage_key)
            except Exception:
                # The DB row is authoritative. A storage lifecycle policy may
                # remove a leaked staging object if this best-effort call fails.
                pass


def _publish_payload_sha256(payload, experiment_id=None):
    canonical = {
        "target_experiment_id": str(experiment_id) if experiment_id else None,
        "payload": payload.model_dump(mode="json"),
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _idempotent_publish_result(
    database,
    actor,
    request_id,
    payload_sha256,
    experiment_id=None,
):
    operation = database.scalar(
        select(ExperimentPublishOperation).where(
            ExperimentPublishOperation.owner_id == actor.id,
            ExperimentPublishOperation.request_id == request_id,
        )
    )
    if operation is None:
        return None
    if (
        operation.payload_sha256 != payload_sha256
        or (experiment_id is not None and operation.experiment_id != experiment_id)
    ):
        raise HTTPException(
            status_code=409,
            detail="This publish request ID was already used for a different request.",
        )
    return _owned_experiment(database, actor, operation.experiment_id)


def _validate_publish_page_layout(sources, references):
    group = []
    for source, reference in zip(sources, references):
        if not reference.same_page_as_previous and group:
            _validate_publish_page_group(group)
            group = []
        group.append(source)
    if group:
        _validate_publish_page_group(group)


def _validate_publish_page_group(group):
    if len(group) < 2:
        return
    metrics = [
        (item.grid_rows, item.grid_cols, item.expected_word_count) for item in group
    ]
    # Older live Blocks may predate authoritative metrics. Preserve their
    # reorderability; newly staged assets always have complete metrics.
    if any(value is None for item in metrics for value in item):
        return
    rows, cols, _ = metrics[0]
    if any(item[0] != rows or item[1] != cols for item in metrics):
        raise HTTPException(
            status_code=422,
            detail="Blocks sharing a page must use the same grid.",
        )
    if sum(item[2] for item in metrics) > rows * cols:
        raise HTTPException(
            status_code=422,
            detail="Blocks sharing a page exceed the grid capacity.",
        )


@router.post(
    "/staged-blocks",
    response_model=StagedBlockAssetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def stage_block(
    request: Request,
    response: Response,
    x_block_name: str = Header(alias="X-Block-Name"),
    x_idempotency_key: str = Header(alias="X-Idempotency-Key"),
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    """Store one validated immutable Block without changing a live Experiment."""
    try:
        request_id = str(uuid.UUID(x_idempotency_key))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="X-Idempotency-Key must be a UUID.",
        ) from exc
    filename = _safe_zip_filename(x_filename)
    block_name = _safe_block_name(x_block_name)
    _cleanup_expired_staged_blocks(database, storage, actor.id)
    upload_path, sha256, size_bytes, metadata = await _receive_package(request)
    storage_key = None
    try:
        if metadata.name != block_name:
            raise HTTPException(
                status_code=422,
                detail="X-Block-Name must match the name in the block package.",
            )
        existing = database.scalar(
            select(StagedBlockAsset).where(
                StagedBlockAsset.owner_id == actor.id,
                StagedBlockAsset.request_id == request_id,
            )
        )
        if existing is not None:
            if (
                existing.sha256 != sha256
                or existing.name != block_name
                or existing.original_filename != filename
            ):
                raise HTTPException(
                    status_code=409,
                    detail="This staging idempotency key was already used for different content.",
                )
            response.status_code = status.HTTP_200_OK
            return _staged_block_response(existing)

        asset_id = uuid.uuid4()
        storage_key = f"staging/{actor.id}/blocks/{asset_id}/{sha256}.zip"
        storage.put_file(storage_key, upload_path, "application/zip")
        asset = StagedBlockAsset(
            id=asset_id,
            owner_id=actor.id,
            request_id=request_id,
            name=block_name,
            schema_version=metadata.schema_version,
            app_version=metadata.app_version,
            expected_word_count=metadata.expected_word_count,
            grid_rows=metadata.grid_rows,
            grid_cols=metadata.grid_cols,
            storage_key=storage_key,
            original_filename=filename,
            sha256=sha256,
            size_bytes=size_bytes,
            expires_at=datetime.now(timezone.utc)
            + timedelta(hours=get_settings().staged_block_ttl_hours),
        )
        database.add(asset)
        database.commit()
        database.refresh(asset)
        return _staged_block_response(asset)
    except IntegrityError as exc:
        database.rollback()
        if storage_key is not None:
            try:
                storage.remove_object(storage_key)
            except Exception:
                pass
        existing = database.scalar(
            select(StagedBlockAsset).where(
                StagedBlockAsset.owner_id == actor.id,
                StagedBlockAsset.request_id == request_id,
            )
        )
        if existing is not None and (
            existing.sha256 == sha256
            and existing.name == block_name
            and existing.original_filename == filename
        ):
            response.status_code = status.HTTP_200_OK
            return _staged_block_response(existing)
        raise HTTPException(
            status_code=409,
            detail="This staging idempotency key was committed for different content.",
        ) from exc
    except Exception:
        database.rollback()
        if storage_key is not None:
            try:
                storage.remove_object(storage_key)
            except Exception:
                pass
        raise
    finally:
        upload_path.unlink(missing_ok=True)


def _resolve_publish_sources(database, actor, payload, experiment=None):
    existing_blocks = {
        block.id: block for block in (experiment.blocks if experiment is not None else [])
    }
    staged_ids = [
        reference.id for reference in payload.blocks if reference.source == "staged"
    ]
    staged_assets = {}
    if staged_ids:
        now = datetime.now(timezone.utc)
        staged_assets = {
            asset.id: asset
            for asset in database.scalars(
                select(StagedBlockAsset)
                .where(
                    StagedBlockAsset.id.in_(staged_ids),
                    StagedBlockAsset.owner_id == actor.id,
                    StagedBlockAsset.expires_at > now,
                )
                .with_for_update()
            ).all()
        }

    sources = []
    for reference in payload.blocks:
        if reference.source == "existing":
            if experiment is None:
                raise HTTPException(
                    status_code=422,
                    detail="A new Experiment cannot reference an existing live Block.",
                )
            source = existing_blocks.get(reference.id)
            if source is None:
                raise HTTPException(
                    status_code=422,
                    detail="An existing Block reference does not belong to this Experiment.",
                )
        else:
            source = staged_assets.get(reference.id)
            if source is None:
                raise HTTPException(
                    status_code=409,
                    detail="A staged Block is missing or expired.",
                )
        sources.append(source)
    _validate_publish_page_layout(sources, payload.blocks)
    return sources


def _atomic_publish_experiment(
    database,
    storage,
    actor,
    payload,
    *,
    experiment_id=None,
):
    payload_sha256 = _publish_payload_sha256(payload, experiment_id)
    request_id = str(payload.request_id)
    prior_result = _idempotent_publish_result(
        database, actor, request_id, payload_sha256, experiment_id
    )
    if prior_result is not None:
        return prior_result

    if experiment_id is None:
        experiment = None
    else:
        experiment = _owned_experiment(database, actor, experiment_id, lock=True)
        # A same-request concurrent writer may have committed while this
        # transaction waited for the Experiment row lock.
        prior_result = _idempotent_publish_result(
            database, actor, request_id, payload_sha256, experiment_id
        )
        if prior_result is not None:
            return prior_result
        if experiment.current_revision_id != payload.expected_current_revision_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The Experiment changed since it was loaded. Reload it before saving."
                ),
            )

    sources = _resolve_publish_sources(database, actor, payload, experiment)
    copied_keys = []
    old_live_keys = []
    try:
        if experiment is None:
            experiment = Experiment(
                id=uuid.uuid4(),
                name=payload.name,
                description=payload.description,
                owner_id=actor.id,
            )
            database.add(experiment)
            # Detect a duplicate name while the Experiment is still inside the
            # transaction. A later rollback cannot leave an empty shell.
            database.flush()

        revision_number = (
            database.scalar(
                select(func.max(ExperimentRevision.revision_number)).where(
                    ExperimentRevision.experiment_id == experiment.id
                )
            )
            or 0
        ) + 1
        revision = ExperimentRevision(
            id=uuid.uuid4(),
            experiment_id=experiment.id,
            revision_number=revision_number,
            name=payload.name,
            created_by=actor.id,
        )

        live_blocks = []
        revision_blocks = []
        for position, (source, reference) in enumerate(zip(sources, payload.blocks)):
            live_id = uuid.uuid4()
            revision_block_id = uuid.uuid4()
            live_key = (
                f"experiments/{experiment.id}/blocks/{live_id}/{source.sha256}.zip"
            )
            revision_key = (
                f"experiments/{experiment.id}/revisions/{revision.id}/blocks/"
                f"{revision_block_id}/{source.sha256}.zip"
            )
            storage.copy_object(source.storage_key, live_key)
            copied_keys.append(live_key)
            storage.copy_object(source.storage_key, revision_key)
            copied_keys.append(revision_key)
            common = {
                "position": position,
                "same_page_as_previous": bool(reference.same_page_as_previous),
                "name": source.name,
                "schema_version": source.schema_version,
                "app_version": source.app_version,
                "expected_word_count": source.expected_word_count,
                "grid_rows": source.grid_rows,
                "grid_cols": source.grid_cols,
                "original_filename": source.original_filename,
                "sha256": source.sha256,
                "size_bytes": source.size_bytes,
            }
            live_blocks.append(
                ExperimentBlock(
                    id=live_id,
                    experiment_id=experiment.id,
                    storage_key=live_key,
                    created_by=actor.id,
                    **common,
                )
            )
            revision_blocks.append(
                ExperimentRevisionBlock(
                    id=revision_block_id,
                    revision_id=revision.id,
                    source_block_id=live_id,
                    storage_key=revision_key,
                    **common,
                )
            )

        old_live_keys = [block.storage_key for block in experiment.blocks]
        for block in list(experiment.blocks):
            database.delete(block)
        database.flush()

        experiment.name = payload.name
        experiment.description = payload.description
        database.add_all(live_blocks)
        database.add(revision)
        database.add_all(revision_blocks)
        database.flush()
        experiment.current_revision_id = revision.id
        consumed_at = datetime.now(timezone.utc)
        for source in sources:
            if isinstance(source, StagedBlockAsset):
                source.consumed_at = source.consumed_at or consumed_at
        database.add(
            ExperimentPublishOperation(
                owner_id=actor.id,
                request_id=request_id,
                payload_sha256=payload_sha256,
                experiment_id=experiment.id,
                revision_id=revision.id,
            )
        )
        database.commit()
    except IntegrityError as exc:
        database.rollback()
        for storage_key in copied_keys:
            try:
                storage.remove_object(storage_key)
            except Exception:
                pass
        # Covers an exact create retry that raced the first request and lost
        # the unique request/name constraint after the winner committed.
        prior_result = _idempotent_publish_result(
            database, actor, request_id, payload_sha256, experiment_id
        )
        if prior_result is not None:
            return prior_result
        raise HTTPException(
            status_code=409,
            detail="The Experiment name or publish request conflicts with another save.",
        ) from exc
    except HTTPException:
        database.rollback()
        for storage_key in copied_keys:
            try:
                storage.remove_object(storage_key)
            except Exception:
                pass
        raise
    except Exception as exc:
        database.rollback()
        for storage_key in copied_keys:
            try:
                storage.remove_object(storage_key)
            except Exception:
                pass
        raise HTTPException(
            status_code=503,
            detail="Publishing failed; the runnable Experiment was left unchanged.",
        ) from exc

    database.expire_all()
    published = _owned_experiment(database, actor, experiment.id)
    for storage_key in old_live_keys:
        if not _storage_key_is_referenced(database, storage_key):
            try:
                storage.remove_object(storage_key)
            except Exception:
                pass
    return published


@router.post(
    "/experiments/publish",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_and_publish_experiment(
    payload: ExperimentPublishCreate,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    _cleanup_expired_staged_blocks(database, storage, actor.id)
    return _experiment_response(
        _atomic_publish_experiment(database, storage, actor, payload)
    )


@router.post(
    "/experiments",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_experiment(
    payload: ExperimentCreate,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    experiment = Experiment(
        name=payload.name,
        description=payload.description,
        owner_id=actor.id,
    )
    database.add(experiment)
    try:
        database.commit()
    except IntegrityError as exc:
        database.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An experiment with this name already exists.",
        ) from exc
    database.refresh(experiment)
    return _experiment_response(experiment)


@router.get("/experiments", response_model=list[ExperimentResponse])
def list_experiments(
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    experiments = database.scalars(
        select(Experiment)
        .where(
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
        .options(
            selectinload(Experiment.blocks),
            selectinload(Experiment.versions),
            selectinload(Experiment.revisions).selectinload(ExperimentRevision.blocks),
        )
        .order_by(Experiment.created_at.desc())
    ).all()
    participant_counts = {}
    analyzed_participant_counts = {}
    if experiments:
        experiment_ids = [experiment.id for experiment in experiments]
        participant_counts = dict(
            database.execute(
                select(
                    ExperimentRun.experiment_id,
                    func.count(func.distinct(ExperimentRun.participant_number)),
                )
                .where(
                    ExperimentRun.experiment_id.in_(
                        experiment_ids
                    )
                )
                .group_by(ExperimentRun.experiment_id)
            ).all()
        )
        analyzed_participant_counts = dict(
            database.execute(
                select(
                    ExperimentRun.experiment_id,
                    func.count(func.distinct(ExperimentRun.participant_number)),
                )
                .where(
                    ExperimentRun.experiment_id.in_(experiment_ids),
                    ExperimentRun.analysis_completed.is_(True),
                )
                .group_by(ExperimentRun.experiment_id)
            ).all()
        )
    return [
        _experiment_response(
            experiment,
            participant_count=int(participant_counts.get(experiment.id, 0)),
            analyzed_participant_count=int(
                analyzed_participant_counts.get(experiment.id, 0)
            ),
        )
        for experiment in experiments
    ]


@router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
def get_experiment(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    return _experiment_response(_owned_experiment(database, actor, experiment_id))


@router.post(
    "/experiments/{experiment_id}/publish",
    response_model=ExperimentResponse,
)
def update_and_publish_experiment(
    experiment_id: uuid.UUID,
    payload: ExperimentPublishUpdate,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    _cleanup_expired_staged_blocks(database, storage, actor.id)
    return _experiment_response(
        _atomic_publish_experiment(
            database,
            storage,
            actor,
            payload,
            experiment_id=experiment_id,
        )
    )


@router.patch("/experiments/{experiment_id}", response_model=ExperimentResponse)
def update_experiment(
    experiment_id: uuid.UUID,
    payload: ExperimentUpdate,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    if "name" in payload.model_fields_set:
        experiment.name = payload.name
    if "description" in payload.model_fields_set:
        experiment.description = payload.description
    try:
        database.commit()
    except IntegrityError as exc:
        database.rollback()
        raise HTTPException(
            status_code=409,
            detail="An experiment with this name already exists.",
        ) from exc
    return _experiment_response(experiment)


@router.delete("/experiments/{experiment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_experiment(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    storage_keys = {
        item.storage_key
        for item in [
            *experiment.blocks,
            *experiment.versions,
            *(block for revision in experiment.revisions for block in revision.blocks),
            *(result for run in experiment.runs for result in run.results),
            *(artifact for run in experiment.runs for artifact in run.artifacts),
        ]
    }
    queue_object_deletions(database, storage_keys)
    database.delete(experiment)
    database.commit()
    drain_object_deletions(database, storage)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _create_revision_snapshot(database, storage, actor, experiment):
    blocks = sorted(experiment.blocks, key=lambda block: block.position)
    if not blocks:
        raise HTTPException(status_code=409, detail="Experiment has no Blocks to revise.")
    next_number = max((item.revision_number for item in experiment.revisions), default=0) + 1
    revision = ExperimentRevision(
        id=uuid.uuid4(), experiment_id=experiment.id, revision_number=next_number,
        name=experiment.name, created_by=actor.id,
    )
    database.add(revision)
    database.flush()
    copied_keys = []
    try:
        for block in blocks:
            revision_block_id = uuid.uuid4()
            destination_key = (
                f"experiments/{experiment.id}/revisions/{revision.id}/blocks/"
                f"{revision_block_id}/{block.sha256}.zip"
            )
            storage.copy_object(block.storage_key, destination_key)
            copied_keys.append(destination_key)
            revision.blocks.append(ExperimentRevisionBlock(
                id=revision_block_id, source_block_id=block.id, position=block.position,
                same_page_as_previous=block.same_page_as_previous, name=block.name,
                schema_version=block.schema_version, app_version=block.app_version,
                expected_word_count=block.expected_word_count,
                grid_rows=block.grid_rows, grid_cols=block.grid_cols,
                storage_key=destination_key, original_filename=block.original_filename,
                sha256=block.sha256, size_bytes=block.size_bytes,
            ))
        experiment.current_revision_id = revision.id
        database.commit()
        database.refresh(revision)
        return revision
    except Exception:
        database.rollback()
        for key in copied_keys:
            storage.remove_object(key)
        raise


@router.post(
    "/experiments/{experiment_id}/revisions",
    response_model=ExperimentRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_experiment_revision(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    return _revision_response(_create_revision_snapshot(database, storage, actor, experiment))


@router.get(
    "/experiments/{experiment_id}/revisions",
    response_model=list[ExperimentRevisionResponse],
)
def list_experiment_revisions(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id)
    return [
        _revision_response(revision)
        for revision in sorted(
            experiment.revisions,
            key=lambda item: item.revision_number,
            reverse=True,
        )
    ]


@router.post(
    "/experiments/{experiment_id}/duplicate",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_experiment(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    source = _owned_experiment(database, actor, experiment_id, lock=True)
    existing_names = set(
        database.scalars(
            select(Experiment.name).where(Experiment.owner_id == actor.id)
        ).all()
    )
    base_name = f"{source.name} copy"
    duplicate_name = base_name
    suffix = 2
    while duplicate_name in existing_names:
        duplicate_name = f"{base_name} {suffix}"
        suffix += 1

    duplicate = Experiment(
        name=duplicate_name,
        description=source.description,
        owner_id=actor.id,
    )
    database.add(duplicate)
    database.flush()
    copied_keys = []
    try:
        for source_block in sorted(source.blocks, key=lambda block: block.position):
            block_id = uuid.uuid4()
            destination_key = (
                f"experiments/{duplicate.id}/blocks/{block_id}/{source_block.sha256}.zip"
            )
            storage.copy_object(source_block.storage_key, destination_key)
            copied_keys.append(destination_key)
            duplicate.blocks.append(
                ExperimentBlock(
                    id=block_id,
                    position=source_block.position,
                    same_page_as_previous=source_block.same_page_as_previous,
                    name=source_block.name,
                    schema_version=source_block.schema_version,
                    app_version=source_block.app_version,
                    expected_word_count=source_block.expected_word_count,
                    grid_rows=source_block.grid_rows,
                    grid_cols=source_block.grid_cols,
                    storage_key=destination_key,
                    original_filename=source_block.original_filename,
                    sha256=source_block.sha256,
                    size_bytes=source_block.size_bytes,
                    created_by=actor.id,
                )
            )
        _create_revision_snapshot(database, storage, actor, duplicate)
        database.expire(duplicate, ["revisions"])
    except Exception:
        database.rollback()
        for storage_key in copied_keys:
            storage.remove_object(storage_key)
        raise
    return _experiment_response(duplicate)


@router.post(
    "/experiments/{experiment_id}/blocks",
    response_model=ExperimentBlockResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_block(
    experiment_id: uuid.UUID,
    request: Request,
    x_block_name: str = Header(alias="X-Block-Name"),
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    x_position: int | None = Header(default=None, alias="X-Position"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    filename = _safe_zip_filename(x_filename)
    block_name = _safe_block_name(x_block_name)
    blocks = sorted(experiment.blocks, key=lambda block: block.position)
    position = len(blocks) if x_position is None else x_position
    if position < 0 or position > len(blocks):
        raise HTTPException(
            status_code=422,
            detail=f"X-Position must be between 0 and {len(blocks)}.",
        )

    upload_path, sha256, size_bytes, metadata = await _receive_package(request)
    storage_key = None
    try:
        if metadata.name != block_name:
            raise HTTPException(
                status_code=422,
                detail="X-Block-Name must match the name in the block package.",
            )
        block = ExperimentBlock(
            id=uuid.uuid4(),
            experiment_id=experiment.id,
            position=1_000_000 + len(blocks),
            name=block_name,
            schema_version=metadata.schema_version,
            app_version=metadata.app_version,
            expected_word_count=metadata.expected_word_count,
            grid_rows=metadata.grid_rows,
            grid_cols=metadata.grid_cols,
            original_filename=filename,
            sha256=sha256,
            size_bytes=size_bytes,
            created_by=actor.id,
            storage_key="pending",
        )
        storage_key = f"experiments/{experiment.id}/blocks/{block.id}/{sha256}.zip"
        block.storage_key = storage_key
        storage.put_file(storage_key, upload_path, "application/zip")
        database.add(block)
        database.flush()
        blocks.insert(position, block)
        _move_blocks(database, blocks)
        database.commit()
        database.refresh(block)
        return _block_response(block)
    except Exception:
        database.rollback()
        if storage_key is not None:
            storage.remove_object(storage_key)
        raise
    finally:
        upload_path.unlink(missing_ok=True)


@router.api_route(
    "/experiments/{experiment_id}/blocks/order",
    methods=["PUT", "PATCH"],
    response_model=ExperimentResponse,
)
def reorder_blocks(
    experiment_id: uuid.UUID,
    payload: BlockReorder,
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    blocks_by_id = {block.id: block for block in experiment.blocks}
    if set(payload.block_ids) != set(blocks_by_id):
        raise HTTPException(
            status_code=422,
            detail="block_ids must contain every block in the experiment exactly once.",
        )
    ordered_blocks = [blocks_by_id[block_id] for block_id in payload.block_ids]
    _move_blocks(database, ordered_blocks)
    same_page_block_ids = set(payload.same_page_block_ids)
    for block in ordered_blocks:
        block.same_page_as_previous = block.id in same_page_block_ids
    ordered_blocks[0].same_page_as_previous = False
    database.commit()
    return _experiment_response(experiment)


@router.delete("/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_block(
    block_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    block = database.scalar(
        select(ExperimentBlock)
        .join(Experiment)
        .where(
            ExperimentBlock.id == block_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
    )
    if block is None:
        raise HTTPException(status_code=404, detail="Block was not found.")
    experiment = _owned_experiment(database, actor, block.experiment_id, lock=True)
    storage_key = block.storage_key
    remaining = [item for item in experiment.blocks if item.id != block.id]
    database.delete(block)
    database.flush()
    _move_blocks(database, remaining)
    database.commit()
    if not _storage_key_is_referenced(database, storage_key):
        storage.remove_object(storage_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/blocks/{block_id}/download")
def download_block(
    block_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    block = database.scalar(
        select(ExperimentBlock)
        .join(Experiment)
        .where(
            ExperimentBlock.id == block_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
    )
    if block is None:
        raise HTTPException(status_code=404, detail="Block was not found.")
    headers = {
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(block.original_filename)}"
        ),
        "Content-Length": str(block.size_bytes),
        "X-Checksum-SHA256": block.sha256,
    }
    return StreamingResponse(
        storage.iter_object(block.storage_key),
        media_type="application/zip",
        headers=headers,
    )


def _remove_temp_file(path):
    Path(path).unlink(missing_ok=True)


def _safe_bundle_block_name(value):
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return (value or "block")[:80]


def _revision_download_response(revision, storage):
    blocks = sorted(revision.blocks, key=lambda block: block.position)
    if len(blocks) == 1:
        block = blocks[0]
        return StreamingResponse(
            storage.iter_object(block.storage_key), media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(revision.name + '.zip')}",
                "Content-Length": str(block.size_bytes),
                "X-Checksum-SHA256": block.sha256,
                "X-AutoScript-Package-Type": "block",
                "X-AutoScript-Revision-ID": str(revision.id),
            },
        )
    descriptor, raw_path = tempfile.mkstemp(prefix="autoscript-revision-", suffix=".zip")
    os.close(descriptor)
    bundle_path = Path(raw_path)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_STORED) as bundle:
        manifest_blocks = []
        for index, block in enumerate(blocks, start=1):
            nested_name = f"blocks/{index:03d}_{_safe_bundle_block_name(block.name)}.zip"
            manifest_blocks.append({
                "id": str(block.source_block_id or block.id), "name": block.name, "path": nested_name,
                "sha256": block.sha256,
                "same_page_as_previous": block.same_page_as_previous,
            })
            with bundle.open(nested_name, "w") as nested_file:
                for chunk in storage.iter_object(block.storage_key):
                    nested_file.write(chunk)
        bundle.writestr("experiment.json", json.dumps({
            "schema_version": "1.0", "package_type": "experiment",
            "name": revision.name, "id": str(revision.experiment_id),
            "revision_id": str(revision.id),
            "revision_number": revision.revision_number,
            "blocks": manifest_blocks,
        }, ensure_ascii=False, indent=2).encode("utf-8"))
    return FileResponse(
        bundle_path, media_type="application/zip", filename=f"{revision.name}.zip",
        headers={"X-AutoScript-Package-Type": "experiment-bundle", "X-AutoScript-Revision-ID": str(revision.id)},
        background=BackgroundTask(_remove_temp_file, bundle_path),
    )


@router.get("/experiment-revisions/{revision_id}/download")
def download_experiment_revision(
    revision_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    revision = database.scalar(
        select(ExperimentRevision)
        .join(Experiment, ExperimentRevision.experiment_id == Experiment.id)
        .where(
            ExperimentRevision.id == revision_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        ).options(selectinload(ExperimentRevision.blocks))
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Experiment revision was not found.")
    return _revision_download_response(revision, storage)


@router.get("/experiments/{experiment_id}/download")
def download_experiment(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id)
    revision = next(
        (
            item
            for item in experiment.revisions
            if item.id == experiment.current_revision_id
        ),
        None,
    )
    if revision is None and experiment.current_revision_id is None:
        revision = max(
            experiment.revisions,
            key=lambda item: item.revision_number,
            default=None,
        )
    if revision is not None:
        return _revision_download_response(revision, storage)
    blocks = sorted(
        experiment.blocks,
        key=lambda block: block.position,
    )
    if not blocks:
        raise HTTPException(status_code=409, detail="Experiment has no blocks to download.")
    download_name = f"{experiment.name}.zip"
    if len(blocks) == 1:
        block = blocks[0]
        return StreamingResponse(
            storage.iter_object(block.storage_key),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(download_name)}"
                ),
                "Content-Length": str(block.size_bytes),
                "X-Checksum-SHA256": block.sha256,
                "X-AutoScript-Package-Type": "block",
            },
        )

    descriptor, raw_path = tempfile.mkstemp(
        prefix="autoscript-experiment-",
        suffix=".zip",
    )
    os.close(descriptor)
    bundle_path = Path(raw_path)
    manifest_blocks = []
    try:
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_STORED) as bundle:
            for index, block in enumerate(blocks, start=1):
                nested_name = (
                    f"blocks/{index:03d}_{_safe_bundle_block_name(block.name)}.zip"
                )
                manifest_blocks.append(
                    {
                        "id": str(block.id),
                        "name": block.name,
                        "path": nested_name,
                        "sha256": block.sha256,
                        "same_page_as_previous": block.same_page_as_previous,
                    }
                )
                with bundle.open(nested_name, "w") as nested_file:
                    for chunk in storage.iter_object(block.storage_key):
                        nested_file.write(chunk)
            manifest = {
                "schema_version": "1.0",
                "package_type": "experiment",
                "name": experiment.name,
                "id": str(experiment.id),
                "blocks": manifest_blocks,
            }
            bundle.writestr(
                "experiment.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
    except Exception:
        bundle_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        bundle_path,
        media_type="application/zip",
        filename=download_name,
        headers={"X-AutoScript-Package-Type": "experiment-bundle"},
        background=BackgroundTask(_remove_temp_file, bundle_path),
    )


@router.post(
    "/experiments/{experiment_id}/versions",
    response_model=ExperimentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_experiment_version(
    experiment_id: uuid.UUID,
    request: Request,
    response: Response,
    x_filename: str | None = Header(default=None, alias="X-Filename"),
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    """Legacy publish route: retain version history and append one block."""
    experiment = _owned_experiment(database, actor, experiment_id, lock=True)
    filename = _safe_zip_filename(x_filename, "experiment.zip")
    upload_path, sha256, size_bytes, metadata = await _receive_package(request)
    storage_key = None
    try:
        existing = database.scalar(
            select(ExperimentVersion).where(
                ExperimentVersion.experiment_id == experiment.id,
                ExperimentVersion.sha256 == sha256,
            )
        )
        if existing is not None:
            response.status_code = status.HTTP_200_OK
            return _version_response(existing)

        next_version = (
            database.scalar(
                select(func.max(ExperimentVersion.version_number)).where(
                    ExperimentVersion.experiment_id == experiment.id
                )
            )
            or 0
        ) + 1
        storage_key = f"experiments/{experiment.id}/versions/{next_version}/{sha256}.zip"
        storage.put_file(storage_key, upload_path, "application/zip")
        version = ExperimentVersion(
            experiment_id=experiment.id,
            version_number=next_version,
            schema_version=metadata.schema_version,
            app_version=metadata.app_version,
            storage_key=storage_key,
            original_filename=filename,
            sha256=sha256,
            size_bytes=size_bytes,
            created_by=actor.id,
        )
        if len(experiment.blocks) == 1:
            # A legacy experiment represented one runnable ZIP. Preserve that
            # behavior by advancing its sole block while retaining immutable
            # version history. Multi-block experiments append as a safe fallback.
            block = experiment.blocks[0]
            block.name = metadata.name
            block.schema_version = metadata.schema_version
            block.app_version = metadata.app_version
            block.expected_word_count = metadata.expected_word_count
            block.grid_rows = metadata.grid_rows
            block.grid_cols = metadata.grid_cols
            block.storage_key = storage_key
            block.original_filename = filename
            block.sha256 = sha256
            block.size_bytes = size_bytes
            block.created_by = actor.id
            database.add(version)
        else:
            block = ExperimentBlock(
                experiment_id=experiment.id,
                position=len(experiment.blocks),
                name=metadata.name,
                schema_version=metadata.schema_version,
                app_version=metadata.app_version,
                expected_word_count=metadata.expected_word_count,
                grid_rows=metadata.grid_rows,
                grid_cols=metadata.grid_cols,
                storage_key=storage_key,
                original_filename=filename,
                sha256=sha256,
                size_bytes=size_bytes,
                created_by=actor.id,
            )
            database.add_all([version, block])
        database.commit()
        database.refresh(version)
        return _version_response(version)
    except Exception:
        database.rollback()
        if storage_key is not None:
            storage.remove_object(storage_key)
        raise
    finally:
        upload_path.unlink(missing_ok=True)


@router.get("/experiment-versions/{version_id}/download")
def download_experiment_version(
    version_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    version = database.scalar(
        select(ExperimentVersion)
        .join(Experiment)
        .where(
            ExperimentVersion.id == version_id,
            Experiment.owner_id == actor.id,
            Experiment.archived_at.is_(None),
        )
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Experiment version was not found.")

    headers = {
        "Content-Disposition": (
            f"attachment; filename*=UTF-8''{quote(version.original_filename)}"
        ),
        "Content-Length": str(version.size_bytes),
        "X-Checksum-SHA256": version.sha256,
    }
    return StreamingResponse(
        storage.iter_object(version.storage_key),
        media_type="application/zip",
        headers=headers,
    )
