"""Durable FIFO queue for Runner result uploads and Run transitions."""

from contextlib import contextmanager
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from app_paths import ensure_dir, user_data_dir


def queue_root():
    return ensure_dir(user_data_dir() / "api_upload_queue")


def _write_item(item):
    root = queue_root()
    item_id = uuid.uuid4().hex
    path = root / f"{item_id}.queue.json"
    temporary = root / f"{item_id}.tmp"
    temporary.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def enqueue_result(result_path, experiment_id, run_id=None, terminal_after=None):
    if terminal_after not in {None, "finalize", "cancel", "fail"}:
        raise ValueError(f"Unsupported Run transition: {terminal_after}")
    root = queue_root()
    source = Path(result_path)
    payload_name = f"{uuid.uuid4().hex}.payload"
    payload_path = root / payload_name
    temporary_payload = payload_path.with_suffix(".part")
    shutil.copy2(source, temporary_payload)
    temporary_payload.replace(payload_path)
    return _write_item({
        "kind": "result", "created_at": time.time(),
        "payload": payload_name, "experiment_id": str(experiment_id),
        "run_id": str(run_id) if run_id else None,
        "terminal_after": terminal_after,
    })


def enqueue_finalize(run_id):
    return enqueue_transition(run_id, "finalize")


def enqueue_transition(run_id, transition):
    if transition not in {"finalize", "cancel", "fail"}:
        raise ValueError(f"Unsupported Run transition: {transition}")
    return _write_item({
        "kind": transition, "created_at": time.time(), "run_id": str(run_id)
    })


class QueueBusyError(RuntimeError):
    pass


@contextmanager
def _queue_lock(root):
    lock_path = root / ".drain.lock"
    lock_file = lock_path.open("a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise QueueBusyError("The result upload queue is already being drained.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise QueueBusyError("The result upload queue is already being drained.") from exc
        locked = True
        yield
    finally:
        if locked:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _quarantine(root, item_path, item, reason):
    quarantine = ensure_dir(root / "quarantine")
    suffix = uuid.uuid4().hex[:8]
    destination = quarantine / f"{item_path.stem}.{suffix}.queue.json"
    try:
        item_path.replace(destination)
    except OSError:
        return
    payload_name = item.get("payload") if isinstance(item, dict) else None
    if payload_name:
        payload_path = root / payload_name
        if payload_path.is_file():
            try:
                payload_path.replace(quarantine / f"{payload_path.name}.{suffix}")
            except OSError:
                pass
    try:
        (quarantine / f"{destination.name}.error.txt").write_text(
            str(reason), encoding="utf-8"
        )
    except OSError:
        pass


def _load_entries(root, errors):
    entries = []
    for item_path in root.glob("*.queue.json"):
        try:
            item = json.loads(item_path.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise ValueError("Queue item must be a JSON object.")
            entries.append((float(item.get("created_at", 0)), item_path.name, item_path, item))
        except (OSError, ValueError) as exc:
            _quarantine(root, item_path, {}, exc)
            errors.append(f"Quarantined invalid queue item {item_path.name}: {exc}")
    return entries


def _is_permanent_error(exc):
    return getattr(exc, "status_code", None) in {400, 404, 409, 413, 415, 422}


def _drain_locked(api, root):
    uploaded = 0
    errors = []
    entries = _load_entries(root, errors)
    for _created_at, _name, item_path, item in sorted(entries):
        try:
            if item.get("kind") == "result":
                payload_name = item.get("payload")
                if not payload_name:
                    raise ValueError("Result queue item has no payload.")
                payload_path = root / payload_name
                if not payload_path.is_file():
                    raise ValueError(f"Queued result payload is missing: {payload_name}")
                if item.get("run_id"):
                    uploaded_result = api.upload_run_result(item["run_id"], payload_path)
                else:
                    uploaded_result = api.upload_result(item["experiment_id"], payload_path)
                terminal_after = item.get("terminal_after")
                if terminal_after:
                    target_run_id = item.get("run_id") or (
                        uploaded_result.get("run_id")
                        if isinstance(uploaded_result, dict)
                        else None
                    )
                    if not target_run_id:
                        raise ValueError(
                            "Uploaded result did not return the Run ID required "
                            "for its terminal transition."
                        )
                    if terminal_after == "finalize":
                        api.finalize_run(target_run_id)
                    elif terminal_after == "cancel":
                        api.cancel_run(target_run_id)
                    else:
                        api.fail_run(target_run_id)
                payload_path.unlink(missing_ok=True)
                uploaded += 1
            elif item.get("kind") == "finalize":
                api.finalize_run(item["run_id"])
            elif item.get("kind") == "cancel":
                api.cancel_run(item["run_id"])
            elif item.get("kind") == "fail":
                api.fail_run(item["run_id"])
            else:
                raise ValueError(f"Unknown queue item kind: {item.get('kind')!r}")
            item_path.unlink(missing_ok=True)
        except (KeyError, TypeError, ValueError) as exc:
            _quarantine(root, item_path, item, exc)
            errors.append(f"Quarantined invalid queue item {item_path.name}: {exc}")
        except Exception as exc:
            if _is_permanent_error(exc):
                _quarantine(root, item_path, item, exc)
                errors.append(f"Quarantined rejected queue item {item_path.name}: {exc}")
                continue
            errors.append(str(exc))
            break
    return uploaded, errors


def drain_upload_queue(api):
    root = queue_root()
    try:
        with _queue_lock(root):
            return _drain_locked(api, root)
    except QueueBusyError as exc:
        return 0, [str(exc)]
