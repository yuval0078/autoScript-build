"""Durable FIFO queue for Runner result uploads and Run finalization."""

import json
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


def enqueue_result(result_path, experiment_id, run_id=None):
    root = queue_root()
    source = Path(result_path)
    payload_name = f"{uuid.uuid4().hex}.payload"
    payload_path = root / payload_name
    shutil.copy2(source, payload_path)
    return _write_item({
        "kind": "result", "created_at": time.time(),
        "payload": payload_name, "experiment_id": str(experiment_id),
        "run_id": str(run_id) if run_id else None,
    })


def enqueue_finalize(run_id):
    return _write_item({
        "kind": "finalize", "created_at": time.time(), "run_id": str(run_id)
    })


def drain_upload_queue(api):
    root = queue_root()
    entries = []
    for item_path in root.glob("*.queue.json"):
        try:
            item = json.loads(item_path.read_text(encoding="utf-8"))
            entries.append((float(item.get("created_at", 0)), item_path, item))
        except (OSError, ValueError):
            continue
    uploaded = 0
    errors = []
    for _created_at, item_path, item in sorted(entries, key=lambda value: value[0]):
        try:
            if item.get("kind") == "result":
                payload_path = root / item["payload"]
                if item.get("run_id"):
                    api.upload_run_result(item["run_id"], payload_path)
                else:
                    api.upload_result(item["experiment_id"], payload_path)
                payload_path.unlink(missing_ok=True)
                uploaded += 1
            elif item.get("kind") == "finalize":
                api.finalize_run(item["run_id"])
            item_path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(str(exc))
            break
    return uploaded, errors
