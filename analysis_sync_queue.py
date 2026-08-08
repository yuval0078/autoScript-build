"""Durable, content-preserving queue for Analyzer cloud synchronization."""

from contextlib import contextmanager
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from app_paths import ensure_dir, user_data_dir


def queue_root():
    return ensure_dir(user_data_dir() / "analysis_upload_queue")


class QueueBusyError(RuntimeError):
    pass


@contextmanager
def _queue_lock(root):
    lock_path = root / ".queue.lock"
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise QueueBusyError("The Analyzer sync queue is busy.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise QueueBusyError("The Analyzer sync queue is busy.") from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _copy_payload(root, source):
    payload_name = f"{uuid.uuid4().hex}.payload"
    destination = root / payload_name
    temporary = destination.with_suffix(".part")
    shutil.copy2(source, temporary)
    temporary.replace(destination)
    return payload_name


def _remove_entry(root, path, item):
    payload = item.get("payload") if isinstance(item, dict) else None
    path.unlink(missing_ok=True)
    if payload:
        (root / payload).unlink(missing_ok=True)


def _entries(root):
    values = []
    for path in root.glob("*.queue.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            values.append((float(item.get("created_at", 0)), path.name, path, item))
        except (OSError, ValueError, TypeError):
            values.append((0, path.name, path, None))
    return sorted(values)


def _enqueue(kind, run_id, source_path, base_etag, request_id=None):
    root = queue_root()
    request_id = str(request_id or uuid.uuid4())
    with _queue_lock(root):
        superseded = []
        # A new draft supersedes only an older unsent draft. A finalization
        # contains the newest draft and therefore supersedes all queued drafts.
        for _created, _name, path, item in _entries(root):
            if not isinstance(item, dict) or item.get("run_id") != str(run_id):
                continue
            if (
                (
                    item.get("kind") == "state_put"
                    or (kind == "finalize" and item.get("kind") == "finalize")
                )
                and int(item.get("attempt_count", 0)) == 0
                and kind in {"state_put", "finalize"}
            ):
                superseded.append((path, item))
        payload_name = _copy_payload(root, Path(source_path))
        item_id = uuid.uuid4().hex
        item_path = root / f"{item_id}.queue.json"
        _atomic_json(
            item_path,
            {
                "kind": kind,
                "created_at": time.time(),
                "run_id": str(run_id),
                "request_id": request_id,
                "base_etag": base_etag,
                "payload": payload_name,
                "attempt_count": 0,
            },
        )
        # The replacement is durable before the older unsent draft is removed.
        for old_path, old_item in superseded:
            _remove_entry(root, old_path, old_item)
    return item_path


def enqueue_analysis_state(run_id, state_path, base_etag=None, request_id=None):
    return _enqueue("state_put", run_id, state_path, base_etag, request_id)


def enqueue_analysis_finalize(run_id, bundle_path, base_etag=None, request_id=None):
    return _enqueue("finalize", run_id, bundle_path, base_etag, request_id)


def _move_entry(root, path, item, folder, reason):
    target_root = ensure_dir(root / folder)
    suffix = uuid.uuid4().hex[:8]
    destination = target_root / f"{path.stem}.{suffix}.queue.json"
    path.replace(destination)
    payload_name = item.get("payload") if isinstance(item, dict) else None
    if payload_name:
        payload = root / payload_name
        if payload.is_file():
            payload.replace(target_root / f"{payload.name}.{suffix}")
    (target_root / f"{destination.name}.error.txt").write_text(
        str(reason), encoding="utf-8"
    )


def drain_analysis_queue(api):
    """Drain queued operations.

    Returns ``(completed_count, errors, outcomes)``. Outcomes are successful
    server response dictionaries keyed by Run ID, allowing a live Analyzer to
    advance its ETag without rereading the state.
    """
    root = queue_root()
    completed = 0
    errors = []
    outcomes = {}
    try:
        with _queue_lock(root):
            entries = _entries(root)
            for entry_index, (_created, _name, path, item) in enumerate(entries):
                if not isinstance(item, dict):
                    _move_entry(root, path, {}, "quarantine", "Invalid queue metadata.")
                    errors.append(f"Quarantined invalid Analyzer queue item {path.name}.")
                    continue
                try:
                    payload = root / item["payload"]
                    if not payload.is_file():
                        raise ValueError(f"Queued payload is missing: {item['payload']}")
                    # Persist the attempt before network I/O. If the server commits
                    # and the response is lost, a later autosave must retain this
                    # exact idempotency key and replay it first.
                    item["attempt_count"] = int(item.get("attempt_count", 0)) + 1
                    item["last_attempt_at"] = time.time()
                    _atomic_json(path, item)
                    if item.get("kind") == "state_put":
                        response = api.put_run_analysis_state(
                            item["run_id"],
                            payload,
                            base_etag=item.get("base_etag"),
                            request_id=item["request_id"],
                        )
                    elif item.get("kind") == "finalize":
                        response = api.finalize_run_analysis(
                            item["run_id"],
                            payload,
                            base_etag=item.get("base_etag"),
                            request_id=item["request_id"],
                        )
                    else:
                        raise ValueError(f"Unknown Analyzer queue kind: {item.get('kind')!r}")
                    outcomes[item["run_id"]] = response or {}
                    _remove_entry(root, path, item)
                    completed += 1
                    next_etag = (response or {}).get("etag")
                    if next_etag:
                        # Operations queued while this request was in flight were
                        # based on the previous revision. Advance them durably
                        # before making the next request.
                        for _later_created, _later_name, later_path, later_item in entries[entry_index + 1:]:
                            if (
                                isinstance(later_item, dict)
                                and later_item.get("run_id") == item.get("run_id")
                                and later_path.is_file()
                            ):
                                later_item["base_etag"] = next_etag
                                _atomic_json(later_path, later_item)
                except (KeyError, TypeError, ValueError) as exc:
                    _move_entry(root, path, item, "quarantine", exc)
                    errors.append(f"Quarantined invalid Analyzer queue item: {exc}")
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    if status_code == 412:
                        _move_entry(root, path, item, "conflicts", exc)
                        errors.append(
                            "Analyzer edits conflicted with a newer cloud revision; "
                            "the local payload was preserved in the conflicts folder."
                        )
                        continue
                    if status_code in {400, 404, 409, 413, 415, 422}:
                        _move_entry(root, path, item, "quarantine", exc)
                        errors.append(f"Analyzer upload was rejected and quarantined: {exc}")
                        continue
                    errors.append(str(exc))
                    break
    except QueueBusyError as exc:
        errors.append(str(exc))
    return completed, errors, outcomes
