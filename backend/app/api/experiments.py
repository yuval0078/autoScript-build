import hashlib
import json
import os
import re
import tempfile
import uuid
import zipfile
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
    ExperimentRun,
    ExperimentVersion,
    RunArtifact,
    RunResult,
    User,
)
from ..schemas import (
    BlockReorder,
    ExperimentBlockResponse,
    ExperimentCreate,
    ExperimentResponse,
    ExperimentUpdate,
    ExperimentVersionResponse,
)
from ..services.experiment_packages import (
    PackageValidationError,
    validate_block_package,
)
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
        original_filename=block.original_filename,
        sha256=block.sha256,
        size_bytes=block.size_bytes,
        created_at=block.created_at,
        download_url=f"/api/v1/blocks/{block.id}/download",
    )


def _experiment_response(experiment):
    versions = sorted(
        experiment.versions,
        key=lambda version: version.version_number,
        reverse=True,
    )
    blocks = sorted(experiment.blocks, key=lambda block: block.position)
    return ExperimentResponse(
        id=experiment.id,
        name=experiment.name,
        description=experiment.description,
        owner_id=experiment.owner_id,
        created_at=experiment.created_at,
        blocks=[_block_response(block) for block in blocks],
        versions=[_version_response(version) for version in versions],
        download_url=f"/api/v1/experiments/{experiment.id}/download",
    )


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
        )
        .order_by(Experiment.created_at.desc())
    ).all()
    return [_experiment_response(experiment) for experiment in experiments]


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
            *(result for run in experiment.runs for result in run.results),
            *(artifact for run in experiment.runs for artifact in run.artifacts),
        ]
    }
    database.delete(experiment)
    database.commit()
    for storage_key in storage_keys:
        if not _storage_key_is_referenced(database, storage_key):
            storage.remove_object(storage_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
                    storage_key=destination_key,
                    original_filename=source_block.original_filename,
                    sha256=source_block.sha256,
                    size_bytes=source_block.size_bytes,
                    created_by=actor.id,
                )
            )
        database.commit()
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


@router.get("/experiments/{experiment_id}/download")
def download_experiment(
    experiment_id: uuid.UUID,
    database: Session = Depends(get_db),
    storage=Depends(get_object_storage),
    actor: User = Depends(get_current_user),
):
    experiment = _owned_experiment(database, actor, experiment_id)
    blocks = sorted(experiment.blocks, key=lambda block: block.position)
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
