"""Content-addressed edit-state history for local and legacy Analyzer inputs."""

import hashlib
import json
import time
from pathlib import Path

from app_paths import ensure_dir, user_data_dir


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def workspace_identity(file_paths):
    """Return a path-independent identity while preserving selected Block order."""
    source_hashes = [file_sha256(path) for path in file_paths]
    canonical = json.dumps(source_hashes, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(canonical).hexdigest(), source_hashes


def state_root(workspace_fingerprint):
    return ensure_dir(
        user_data_dir() / "analysis_state" / "local" / workspace_fingerprint
    )


def save_local_analysis_state(file_paths, state, retention=20):
    fingerprint, source_hashes = workspace_identity(file_paths)
    root = state_root(fingerprint)
    existing = sorted(root.glob("*.state.json"))
    revision = 1
    if existing:
        try:
            revision = max(int(path.name.split(".", 1)[0]) for path in existing) + 1
        except ValueError:
            revision = len(existing) + 1
    record = {
        "workspace_fingerprint": fingerprint,
        "source_sha256": source_hashes,
        "revision": revision,
        "saved_at": time.time(),
        "state": state,
    }
    if existing:
        try:
            latest = json.loads(existing[-1].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            latest = None
        if isinstance(latest, dict) and latest.get("state") == state:
            return latest
    destination = root / f"{revision:08d}.state.json"
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(destination)
    versions = sorted(root.glob("*.state.json"))
    for old in versions[:-max(1, int(retention))]:
        old.unlink(missing_ok=True)
    return record


def load_local_analysis_state(file_paths):
    fingerprint, source_hashes = workspace_identity(file_paths)
    versions = sorted(state_root(fingerprint).glob("*.state.json"), reverse=True)
    for path in versions:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("source_sha256") == source_hashes:
            return record
    return None
