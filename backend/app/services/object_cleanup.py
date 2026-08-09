"""Durable object-storage cleanup used after database retention/deletion."""

import uuid

from sqlalchemy import select

from ..models import ObjectDeletionTask


def queue_object_deletions(database, storage_keys):
    keys = {str(key) for key in storage_keys if key}
    if not keys:
        return
    existing = set(
        database.scalars(
            select(ObjectDeletionTask.storage_key).where(
                ObjectDeletionTask.storage_key.in_(keys)
            )
        ).all()
    )
    database.add_all(
        ObjectDeletionTask(id=uuid.uuid4(), storage_key=key)
        for key in sorted(keys - existing)
    )


def drain_object_deletions(database, storage, *, limit=100):
    """Attempt queued removals, retaining failures for a later API operation."""
    tasks = database.scalars(
        select(ObjectDeletionTask)
        .order_by(ObjectDeletionTask.created_at, ObjectDeletionTask.id)
        .limit(limit)
    ).all()
    for task in tasks:
        try:
            storage.remove_object(task.storage_key)
        except Exception as exc:
            task.attempt_count += 1
            task.last_error = str(exc)[:4000]
        else:
            database.delete(task)
    database.commit()
