"""Validation helpers for immutable Runner raw-result JSON artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


class RawResultValidationError(ValueError):
    pass


@dataclass(frozen=True)
class RawResultMetadata:
    schema_version: str
    app_version: str
    experiment_name: str
    source_experiment_id: str | None
    experiment_revision_id: str | None
    block_name: str
    source_block_id: str | None
    block_index: int
    block_count: int
    block_completed: bool
    experiment_completed: bool
    completed_word_count: int
    expected_word_count: int
    participant_number: int
    participant_age: int
    participant_gender: str
    session_id: str
    timestamp: str
    payload: Mapping[str, Any]


def _schema_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "schemas" / "data-contracts"
        if (candidate / "raw-run.schema.json").is_file():
            return candidate
    raise RuntimeError("Raw-result schemas are not available to the API.")


@lru_cache
def _validator() -> Draft202012Validator:
    registry = Registry()
    schemas = {}
    for schema_path in _schema_root().glob("*.schema.json"):
        with schema_path.open("r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        schemas[schema_path.name] = schema
        registry = registry.with_resource(
            schema["$id"],
            Resource.from_contents(schema),
        )
    return Draft202012Validator(schemas["raw-run.schema.json"], registry=registry)


def validate_raw_result(result_path) -> RawResultMetadata:
    try:
        raw_bytes = Path(result_path).read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RawResultValidationError(
            "The uploaded result is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise RawResultValidationError("The uploaded result must be a JSON object.")

    errors = sorted(
        _validator().iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "root"
        raise RawResultValidationError(
            f"Raw result does not match the data contract at {location}: {first.message}"
        )

    block_index = int(payload.get("block_index", payload["session_experiment_index"]))
    block_count = int(payload.get("block_count", payload["session_experiment_count"]))
    if block_index > block_count:
        raise RawResultValidationError("Block index cannot exceed Block count.")

    bounded_fields = {
        "Experiment name": (payload["experiment_name"], 200),
        "Block name": (payload.get("block_name") or payload["experiment_name"], 200),
        "Application version": (payload["app_version"], 32),
    }
    for label, (value, maximum) in bounded_fields.items():
        if len(str(value)) > maximum:
            raise RawResultValidationError(
                f"{label} cannot be longer than {maximum} characters."
            )

    completed_word_count = int(payload.get("completed_word_count", len(payload["words"])))
    expected_word_count = int(payload.get("expected_word_count", completed_word_count))
    if completed_word_count < 0 or expected_word_count < 0:
        raise RawResultValidationError("Word counts cannot be negative.")
    if completed_word_count > expected_word_count:
        raise RawResultValidationError(
            "Completed word count cannot exceed expected word count."
        )
    block_completed = bool(
        payload.get("block_completed", completed_word_count == expected_word_count)
    )
    if block_completed and completed_word_count != expected_word_count:
        raise RawResultValidationError(
            "A completed Block must contain every expected word."
        )

    source_experiment_id = payload.get("experiment_id")
    source_block_id = payload.get("block_id")
    return RawResultMetadata(
        schema_version=payload["schema_version"],
        app_version=payload["app_version"],
        experiment_name=payload["experiment_name"],
        source_experiment_id=(
            None if source_experiment_id is None else str(source_experiment_id)
        ),
        experiment_revision_id=(
            None if payload.get("experiment_revision_id") is None
            else str(payload["experiment_revision_id"])
        ),
        block_name=payload.get("block_name") or payload["experiment_name"],
        source_block_id=None if source_block_id is None else str(source_block_id),
        block_index=block_index,
        block_count=block_count,
        block_completed=block_completed,
        experiment_completed=bool(
            payload.get("experiment_completed", block_completed)
        ),
        completed_word_count=completed_word_count,
        expected_word_count=expected_word_count,
        participant_number=payload["participant_number"],
        participant_age=payload["participant_age"],
        participant_gender=payload["participant_gender"],
        session_id=payload["session_id"],
        timestamp=payload["timestamp"],
        payload=payload,
    )
