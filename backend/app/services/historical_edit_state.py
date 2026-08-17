"""Build current Analyzer edit state from legacy Trainable JSON artifacts.

This service is intentionally strict and idempotent.  It is meant for audited
one-off migrations of historical data, not for accepting untrusted uploads.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..models import (
    Experiment,
    ExperimentRun,
    RunAnalysisOperation,
    RunAnalysisRevision,
    RunArtifact,
    User,
)
from .analysis import AnalysisValidationError, analysis_source_fingerprint, validate_analysis_state


MAX_TRAINABLE_BYTES = 100 * 1024 * 1024
VALID_TRAINABILITY = {"trainable", "low-quality", "untrainable"}


class HistoricalEditStateError(ValueError):
    pass


def _read_artifact(storage, artifact) -> bytes:
    if artifact.size_bytes <= 0 or artifact.size_bytes > MAX_TRAINABLE_BYTES:
        raise HistoricalEditStateError(
            f"Trainable JSON for Run {artifact.run_id} has an invalid size."
        )
    data = bytearray()
    for chunk in storage.iter_object(artifact.storage_key):
        data.extend(chunk)
        if len(data) > MAX_TRAINABLE_BYTES:
            raise HistoricalEditStateError("Trainable JSON exceeds the migration size limit.")
    raw = bytes(data)
    if len(raw) != artifact.size_bytes:
        raise HistoricalEditStateError(
            f"Trainable JSON size mismatch for Run {artifact.run_id}."
        )
    if hashlib.sha256(raw).hexdigest() != artifact.sha256.lower():
        raise HistoricalEditStateError(
            f"Trainable JSON checksum mismatch for Run {artifact.run_id}."
        )
    return raw


def _load_trainable(raw: bytes, run: ExperimentRun) -> list[dict]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoricalEditStateError(
            f"Trainable JSON for participant {run.participant_number} is invalid."
        ) from exc
    entries = payload if isinstance(payload, list) else [payload]
    if not entries or any(not isinstance(entry, dict) for entry in entries):
        raise HistoricalEditStateError("Trainable JSON must contain one object per Block.")
    if len(entries) != len(run.results):
        raise HistoricalEditStateError(
            f"Trainable JSON for participant {run.participant_number} does not cover every Block."
        )
    return entries


def _annotation(word: object, *, participant_number: int) -> dict:
    if not isinstance(word, dict):
        raise HistoricalEditStateError(
            f"Trainable JSON for participant {participant_number} contains an invalid word."
        )
    letters = word.get("letters")
    if not isinstance(letters, list):
        raise HistoricalEditStateError("Every trainable word must contain a letters array.")
    normalized_letters = []
    for letter in letters:
        if not isinstance(letter, dict) or not isinstance(letter.get("char"), str):
            raise HistoricalEditStateError("Every trainable letter must contain a string char.")
        stroke_ids = letter.get("stroke_ids")
        if (
            not isinstance(stroke_ids, list)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in stroke_ids
            )
        ):
            raise HistoricalEditStateError(
                "Every trainable letter must contain non-negative stroke IDs."
            )
        normalized_letters.append(
            {"char": letter["char"], "stroke_ids": list(stroke_ids)}
        )

    trainability = word.get("trainability")
    if trainability not in VALID_TRAINABILITY:
        raise HistoricalEditStateError("Every trainable word needs a valid trainability value.")
    assigned_letters = word.get("assigned_letters", {})
    stroke_slices = word.get("stroke_slices", [])
    annotation = {
        "letters": normalized_letters,
        # Old Trainable JSON stores the authoritative letter-to-stroke mapping in
        # ``letters`` and does not contain these newer editable-state fields.
        "assigned_letters": assigned_letters,
        "stroke_slices": stroke_slices,
        "trainability": trainability,
    }
    if "written_word" in word:
        annotation["written_word"] = word["written_word"]
    if "correct" in word:
        annotation["correct"] = word["correct"]
    return annotation


def _build_state(run: ExperimentRun, trainable_bytes: bytes) -> dict:
    entries = _load_trainable(trainable_bytes, run)
    by_timestamp: dict[str, dict] = {}
    for entry in entries:
        timestamp = entry.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp or timestamp in by_timestamp:
            raise HistoricalEditStateError(
                "Trainable JSON Block timestamps must be present and unique."
            )
        if (
            "participant_number" in entry
            and int(entry["participant_number"]) != run.participant_number
        ):
            raise HistoricalEditStateError("Trainable JSON participant identity is inconsistent.")
        by_timestamp[timestamp] = entry

    sources = []
    for result in sorted(run.results, key=lambda item: item.block_index):
        entry = by_timestamp.get(result.result_timestamp)
        if entry is None:
            raise HistoricalEditStateError(
                f"No Trainable JSON Block matches timestamp {result.result_timestamp}."
            )
        words = entry.get("words")
        if not isinstance(words, list) or len(words) != result.completed_word_count:
            raise HistoricalEditStateError(
                f"Trainable word count does not match Block {result.block_index}."
            )
        sources.append(
            {
                "result_id": str(result.id),
                "raw_sha256": result.sha256.lower(),
                "session_id": run.session_id,
                "block_id": str(result.block_id) if result.block_id is not None else None,
                "block_index": result.block_index,
                "block_name": result.block_name,
                "word_count": result.completed_word_count,
                "words": [
                    _annotation(word, participant_number=run.participant_number)
                    for word in words
                ],
            }
        )
    return {
        "schema_version": "1.1",
        "analyzer_version": "legacy-trainable-import",
        "run_id": str(run.id),
        "experiment_id": str(run.experiment_id),
        "session_id": run.session_id,
        "source_fingerprint": analysis_source_fingerprint(run.results),
        "sources": sources,
    }


def _state_bytes(state: dict) -> bytes:
    return json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _state_artifact(run: ExperimentRun):
    if run.current_analysis_revision is not None:
        matches = [
            artifact
            for artifact in run.current_analysis_revision.artifacts
            if artifact.kind == "analysis_state"
        ]
        if len(matches) != 1:
            raise HistoricalEditStateError(
                f"Run {run.id} has an invalid current analysis revision."
            )
        return matches[0]
    legacy = [
        artifact
        for artifact in run.artifacts
        if artifact.kind == "analysis_state" and artifact.analysis_revision_id is None
    ]
    if len(legacy) > 1:
        raise HistoricalEditStateError(f"Run {run.id} has ambiguous legacy edit states.")
    return legacy[0] if legacy else None


def _next_revision_number(run: ExperimentRun) -> int:
    revision_numbers = [revision.revision_number for revision in run.analysis_revisions]
    return max(revision_numbers, default=0) + 1


def seed_historical_edit_states(
    database,
    storage,
    *,
    experiment_name: str,
    actor_username: str = "admin",
    apply: bool = False,
):
    """Seed current draft states from flat historical Trainable JSON artifacts."""
    experiments = list(
        database.scalars(
            select(Experiment).where(
                func.lower(Experiment.name) == str(experiment_name).lower(),
                Experiment.archived_at.is_(None),
            )
        )
    )
    if len(experiments) != 1:
        raise HistoricalEditStateError(
            f"Expected exactly one active Experiment named {experiment_name!r}."
        )
    experiment = experiments[0]
    actor = database.scalar(
        select(User).where(
            User.username == actor_username,
            User.role == "admin",
            User.is_active.is_(True),
        )
    )
    if actor is None:
        raise HistoricalEditStateError("The migration actor must be an active administrator.")

    runs = list(
        database.scalars(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == experiment.id)
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
            .order_by(ExperimentRun.participant_number, ExperimentRun.id)
        )
    )

    prepared = []
    unchanged = []
    without_trainable = []
    with tempfile.TemporaryDirectory(prefix="autoscript-edit-state-") as temporary:
        temporary_root = Path(temporary)
        for run in runs:
            trainable = [
                artifact
                for artifact in run.artifacts
                if artifact.kind == "trainable_json"
                and artifact.analysis_revision_id is None
            ]
            if not trainable:
                without_trainable.append(run)
                continue
            if len(trainable) != 1:
                raise HistoricalEditStateError(
                    f"Participant {run.participant_number} has ambiguous Trainable JSON copies."
                )
            payload = _build_state(run, _read_artifact(storage, trainable[0]))
            raw_state = _state_bytes(payload)
            state_sha256 = hashlib.sha256(raw_state).hexdigest()
            existing = _state_artifact(run)
            if existing is not None:
                if existing.sha256 != state_sha256 or existing.size_bytes != len(raw_state):
                    raise HistoricalEditStateError(
                        f"Participant {run.participant_number} already has a different edit state."
                    )
                unchanged.append(run)
                continue

            state_path = temporary_root / f"{run.id}.json"
            state_path.write_bytes(raw_state)
            try:
                validated = validate_analysis_state(state_path, run)
            except AnalysisValidationError as exc:
                raise HistoricalEditStateError(str(exc)) from exc
            prepared.append((run, trainable[0], state_path, validated))

        summary = {
            "experiment_id": str(experiment.id),
            "experiment_name": experiment.name,
            "run_count": len(runs),
            "trainable_runs": len(prepared) + len(unchanged),
            "new_edit_states": len(prepared),
            "unchanged_edit_states": len(unchanged),
            "runs_without_trainable": len(without_trainable),
            "restored_words": sum(
                len(source["words"])
                for _run, _artifact, path, _validated in prepared
                for source in json.loads(path.read_text(encoding="utf-8"))["sources"]
            ),
            "applied": bool(apply),
        }
        if not apply:
            return summary

        created_storage_keys = []
        try:
            for run, trainable, state_path, validated in prepared:
                revision = RunAnalysisRevision(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    revision_number=_next_revision_number(run),
                    source_fingerprint=validated.source_fingerprint,
                    finalized=False,
                    completed=None,
                    finalized_at=None,
                    created_by=actor.id,
                )
                artifact_id = uuid.uuid4()
                storage_key = (
                    f"experiments/{run.experiment_id}/runs/{run.id}/analysis/"
                    f"{revision.revision_number}/{revision.id}/analysis_state/"
                    f"{artifact_id}/{validated.sha256}.json"
                )
                state_artifact = RunArtifact(
                    id=artifact_id,
                    run_id=run.id,
                    analysis_revision_id=revision.id,
                    kind="analysis_state",
                    storage_key=storage_key,
                    original_filename="analysis_state.json",
                    sha256=validated.sha256,
                    size_bytes=validated.size_bytes,
                    created_by=actor.id,
                )
                operation = RunAnalysisOperation(
                    id=uuid.uuid4(),
                    run_id=run.id,
                    request_id=f"legacy-state-{trainable.sha256[:32]}",
                    operation_kind="state",
                    request_sha256=validated.sha256,
                    revision_id=revision.id,
                    revision_number=revision.revision_number,
                )
                created_storage_keys.append(storage_key)
                storage.put_file(storage_key, state_path, "application/json")
                database.add_all([revision, state_artifact, operation])
                # Preserve the historical completed-analysis flag.  This is an edit
                # state migration, not a new user edit that invalidates old exports.
                run.current_analysis_revision = revision
            database.commit()
            return summary
        except Exception:
            database.rollback()
            for storage_key in reversed(created_storage_keys):
                try:
                    storage.remove_object(storage_key)
                except Exception:
                    pass
            raise
