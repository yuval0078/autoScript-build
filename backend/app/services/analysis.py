"""Validation and identity helpers for transactional Analyzer persistence."""

from __future__ import annotations

import csv
import hashlib
import json
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator


ANALYSIS_BUNDLE_FILES = {
    "manifest.json",
    "analysis_state.json",
    "analysis.csv",
    "trainable.json",
}
ANALYSIS_OUTPUT_FILES = (
    "analysis_state.json",
    "analysis.csv",
    "trainable.json",
)
ANALYSIS_CSV_IDENTITY_COLUMNS = [
    "Exp Step",
    "Experiment",
    "Experiment ID",
    "Block",
    "Block ID",
    "Block Index",
    "Block Count",
    "Session ID",
    "Participant",
    "Age",
    "Gender",
    "Word",
    "Group",
    "Cell",
    "Correct",
    "Written Word",
    "Reading End",
    "Writing Start",
    "Writing End",
    "Strokes",
    "Avg Interval",
]


class AnalysisValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedAnalysisState:
    payload: Mapping[str, Any]
    sha256: str
    size_bytes: int
    source_fingerprint: str


@dataclass(frozen=True)
class ValidatedAnalysisBundle:
    manifest: Mapping[str, Any]
    state: ValidatedAnalysisState
    paths: Mapping[str, Path]
    sha256: Mapping[str, str]
    size_bytes: Mapping[str, int]
    completed: bool
    source_fingerprint: str


def analysis_source_rows(results: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(result.id),
            "sha256": str(result.sha256).lower(),
            "block_index": int(result.block_index),
        }
        for result in sorted(results, key=lambda item: item.block_index)
    ]


def analysis_source_fingerprint(results: Sequence[Any]) -> str:
    encoded = json.dumps(
        analysis_source_rows(results),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_bytes(raw_bytes: bytes, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisValidationError(f"{label} must be valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise AnalysisValidationError(f"{label} must be a JSON object.")
    return payload


def _schema_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "schemas" / "data-contracts"
        if (candidate / "analysis-state.schema.json").is_file():
            return candidate
    raise RuntimeError("Analyzer data-contract schemas are not available to the API.")


@lru_cache
def _schema_validator(filename: str) -> Draft202012Validator:
    with (_schema_root() / filename).open("r", encoding="utf-8") as schema_file:
        return Draft202012Validator(json.load(schema_file))


def _validate_schema(payload, filename, label):
    errors = sorted(
        _schema_validator(filename).iter_errors(payload),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "root"
        raise AnalysisValidationError(
            f"{label} does not match the data contract at {location}: {first.message}"
        )


def _is_sha256(value: Any) -> bool:
    value = str(value or "").lower()
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def validate_analysis_state(state_path: Path, run: Any) -> ValidatedAnalysisState:
    try:
        raw_bytes = Path(state_path).read_bytes()
    except OSError as exc:
        raise AnalysisValidationError("Analysis state could not be read.") from exc
    payload = _load_json_bytes(raw_bytes, "Analysis state")
    _validate_schema(
        payload,
        "analysis-state.schema.json",
        "Analysis state",
    )
    if payload.get("schema_version") != "1.1":
        raise AnalysisValidationError("Analysis state schema_version must be 1.1.")
    if "analyzer_version" in payload and not isinstance(payload["analyzer_version"], str):
        raise AnalysisValidationError("Analysis state analyzer_version must be a string.")
    if payload.get("run_id") != str(run.id):
        raise AnalysisValidationError("Analysis state run_id does not match the target Run.")
    if str(payload.get("experiment_id") or "") != str(run.experiment_id):
        raise AnalysisValidationError(
            "Analysis state experiment_id does not match the target Run."
        )
    if payload.get("session_id") != run.session_id:
        raise AnalysisValidationError(
            "Analysis state session_id does not match the target Run."
        )

    expected_fingerprint = analysis_source_fingerprint(run.results)
    if payload.get("source_fingerprint") != expected_fingerprint:
        raise AnalysisValidationError(
            "Analysis state source_fingerprint does not match the Run results."
        )
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise AnalysisValidationError("Analysis state sources must be an array.")
    expected_by_index = {result.block_index: result for result in run.results}
    if len(sources) != len(expected_by_index):
        raise AnalysisValidationError(
            "Analysis state must identify every immutable result in the Run exactly once."
        )
    seen_indexes = set()
    for source in sources:
        if not isinstance(source, dict):
            raise AnalysisValidationError("Every analysis state source must be an object.")
        block_index = source.get("block_index")
        result = expected_by_index.get(block_index)
        if result is None or block_index in seen_indexes:
            raise AnalysisValidationError(
                "Analysis state contains an unknown or duplicate Block index."
            )
        seen_indexes.add(block_index)
        if source.get("result_id") != str(result.id):
            raise AnalysisValidationError(
                "Analysis state result_id does not match the immutable Run result."
            )
        if str(source.get("raw_sha256") or "").lower() != result.sha256.lower():
            raise AnalysisValidationError(
                "Analysis state raw_sha256 does not match the immutable Run result."
            )
        if source.get("session_id") != run.session_id:
            raise AnalysisValidationError(
                "Analysis state source session_id does not match the target Run."
            )
        if source.get("block_name") != result.block_name:
            raise AnalysisValidationError(
                "Analysis state Block name does not match the immutable Run result."
            )
        if result.block_id is not None and str(source.get("block_id") or "") != str(result.block_id):
            raise AnalysisValidationError(
                "Analysis state Block ID does not match the immutable Run result."
            )
        words = source.get("words")
        if (
            source.get("word_count") != result.completed_word_count
            or not isinstance(words, list)
            or len(words) != result.completed_word_count
        ):
            raise AnalysisValidationError(
                "Analysis state word annotations do not match the immutable Run result."
            )
        if any(not isinstance(annotation, dict) for annotation in words):
            raise AnalysisValidationError("Every word annotation must be an object.")
        for annotation in words:
            letters = annotation.get("letters")
            assigned_letters = annotation.get("assigned_letters")
            stroke_slices = annotation.get("stroke_slices")
            if not isinstance(letters, list) or any(not isinstance(letter, dict) for letter in letters):
                raise AnalysisValidationError("Word annotation letters must be an array of objects.")
            for letter in letters:
                if not isinstance(letter.get("char"), str):
                    raise AnalysisValidationError("Every letter annotation needs a string char.")
                stroke_ids = letter.get("stroke_ids")
                if (
                    not isinstance(stroke_ids, list)
                    or any(not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in stroke_ids)
                ):
                    raise AnalysisValidationError(
                        "Letter stroke_ids must contain non-negative integers."
                    )
            if not isinstance(assigned_letters, dict) or any(
                not str(index).isdigit() or int(index) < 0 or not isinstance(character, str)
                for index, character in assigned_letters.items()
            ):
                raise AnalysisValidationError(
                    "Word annotation assigned_letters must map non-negative indices to strings."
                )
            if (
                not isinstance(stroke_slices, list)
                or any(not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in stroke_slices)
            ):
                raise AnalysisValidationError(
                    "Word annotation stroke_slices must contain non-negative integers."
                )
            if annotation.get("trainability") not in {
                "trainable",
                "low-quality",
                "untrainable",
            }:
                raise AnalysisValidationError("Word annotation trainability is invalid.")
            if "written_word" in annotation and not isinstance(annotation["written_word"], str):
                raise AnalysisValidationError("Word annotation written_word must be a string.")
            if "correct" in annotation and not isinstance(annotation["correct"], bool):
                raise AnalysisValidationError("Word annotation correct must be a boolean.")

    return ValidatedAnalysisState(
        payload=payload,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        size_bytes=len(raw_bytes),
        source_fingerprint=expected_fingerprint,
    )


def _validate_csv(csv_path: Path, run: Any) -> None:
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            rows = list(csv.reader(csv_file))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise AnalysisValidationError("analysis.csv must be valid UTF-8 CSV.") from exc
    header = rows[0] if rows else None
    if header is None or header[: len(ANALYSIS_CSV_IDENTITY_COLUMNS)] != ANALYSIS_CSV_IDENTITY_COLUMNS:
        raise AnalysisValidationError(
            "analysis.csv does not contain the required identity columns."
        )
    if header[-1:] != ["Screenshot File"]:
        raise AnalysisValidationError("analysis.csv must end with Screenshot File.")
    letter_columns = header[len(ANALYSIS_CSV_IDENTITY_COLUMNS) : -1]
    if len(letter_columns) % 3:
        raise AnalysisValidationError("analysis.csv letter columns must be complete triplets.")
    for offset in range(0, len(letter_columns), 3):
        number = offset // 3 + 1
        if letter_columns[offset : offset + 3] != [
            f"Written Letter {number}",
            f"Letter {number} Start",
            f"Letter {number} End",
        ]:
            raise AnalysisValidationError("analysis.csv letter columns are not canonical.")
    if len(rows) == 1:
        raise AnalysisValidationError("analysis.csv must contain analyzed word rows.")
    expected_by_index = {result.block_index: result for result in run.results}
    row_count_by_index = {block_index: 0 for block_index in expected_by_index}
    for row in rows[1:]:
        if len(row) != len(header):
            raise AnalysisValidationError("analysis.csv rows must have a consistent width.")
        try:
            block_index = int(row[5])
            block_count = int(row[6])
            participant_number = int(row[8])
            participant_age = int(row[9])
        except (TypeError, ValueError) as exc:
            raise AnalysisValidationError("analysis.csv contains invalid numeric identity fields.") from exc
        result = expected_by_index.get(block_index)
        if result is None:
            raise AnalysisValidationError("analysis.csv contains an unknown Block index.")
        row_count_by_index[block_index] += 1
        required_identity = (row[1], row[3], row[7], row[8])
        if any(not value for value in required_identity):
            raise AnalysisValidationError("analysis.csv contains an empty required identity field.")
        if (
            row[1] != run.source_experiment_name
            or row[3] != result.block_name
            or row[7] != run.session_id
            or participant_number != run.participant_number
            or participant_age != run.participant_age
            or row[10] != run.participant_gender
            or block_count != run.block_count
        ):
            raise AnalysisValidationError("analysis.csv identity conflicts with the Run.")
        if row[2] != str(run.experiment_id):
            raise AnalysisValidationError("analysis.csv Experiment ID conflicts with the Run.")
        if result.block_id is not None and row[4] != str(result.block_id):
            raise AnalysisValidationError("analysis.csv Block ID conflicts with the Run result.")
    for block_index, result in expected_by_index.items():
        if row_count_by_index[block_index] != result.completed_word_count:
            raise AnalysisValidationError(
                "analysis.csv must contain exactly one row per stored word in every Block."
            )


def _validate_trainable_json(trainable_path: Path, run: Any) -> None:
    try:
        raw_bytes = trainable_path.read_bytes()
    except OSError as exc:
        raise AnalysisValidationError("trainable.json could not be read.") from exc
    try:
        payload = json.loads(raw_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalysisValidationError("trainable.json must be valid UTF-8 JSON.") from exc
    records = payload if isinstance(payload, list) else [payload]
    if not records or any(not isinstance(record, dict) for record in records):
        raise AnalysisValidationError(
            "trainable.json must contain one object or a non-empty array of objects."
        )
    expected_by_index = {result.block_index: result for result in run.results}
    if len(records) != len(expected_by_index):
        raise AnalysisValidationError(
            "trainable.json must identify every immutable result in the Run exactly once."
        )
    seen_indexes = set()
    for record in records:
        block_index = record.get("block_index")
        result = expected_by_index.get(block_index)
        if result is None or block_index in seen_indexes:
            raise AnalysisValidationError(
                "trainable.json contains an unknown or duplicate Block index."
            )
        seen_indexes.add(block_index)
        if record.get("run_id") != str(run.id):
            raise AnalysisValidationError("trainable.json run_id does not match the Run.")
        if record.get("source_result_id") != str(result.id):
            raise AnalysisValidationError(
                "trainable.json source_result_id does not match the Run result."
            )
        if str(record.get("source_sha256") or "").lower() != result.sha256.lower():
            raise AnalysisValidationError(
                "trainable.json source_sha256 does not match the Run result."
            )
        if record.get("session_id") != run.session_id:
            raise AnalysisValidationError(
                "trainable.json session_id does not match the Run."
            )
        if "analyzer_version" in record and not isinstance(record["analyzer_version"], str):
            raise AnalysisValidationError("trainable.json analyzer_version must be a string.")
        if (
            record.get("experiment_name") != run.source_experiment_name
            or record.get("block_name") != result.block_name
            or record.get("block_count") != run.block_count
            or record.get("participant_number") != run.participant_number
            or record.get("participant_age") != run.participant_age
            or record.get("participant_gender") != run.participant_gender
            or record.get("timestamp") != result.result_timestamp
        ):
            raise AnalysisValidationError("trainable.json identity conflicts with the Run.")
        if str(record.get("experiment_id") or "") != str(run.experiment_id):
            raise AnalysisValidationError("trainable.json Experiment ID conflicts with the Run.")
        if result.block_id is not None and str(record.get("block_id") or "") != str(result.block_id):
            raise AnalysisValidationError("trainable.json Block ID conflicts with the Run result.")
        words = record.get("words")
        if not isinstance(words, list) or len(words) != result.completed_word_count:
            raise AnalysisValidationError(
                "trainable.json words do not match the immutable Run result."
            )


def validate_analysis_bundle(
    bundle_path: Path,
    output_dir: Path,
    run: Any,
    *,
    max_uncompressed_bytes: int,
) -> ValidatedAnalysisBundle:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(bundle_path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise AnalysisValidationError("Analysis finalization must be a valid ZIP file.") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(ANALYSIS_BUNDLE_FILES) or set(names) != ANALYSIS_BUNDLE_FILES:
            raise AnalysisValidationError(
                "Analysis ZIP must contain only manifest.json, analysis_state.json, "
                "analysis.csv, and trainable.json at its root."
            )
        if len(set(names)) != len(names):
            raise AnalysisValidationError("Analysis ZIP contains duplicate members.")
        if any(info.flag_bits & 0x1 for info in infos):
            raise AnalysisValidationError("Encrypted analysis ZIP members are not supported.")
        if sum(info.file_size for info in infos) > max_uncompressed_bytes:
            raise AnalysisValidationError("Analysis ZIP expands beyond the configured limit.")

        paths = {}
        sha256 = {}
        size_bytes = {}
        total_written = 0
        for info in infos:
            destination = output_dir / info.filename
            digest = hashlib.sha256()
            member_size = 0
            with archive.open(info, "r") as source, destination.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    member_size += len(chunk)
                    total_written += len(chunk)
                    if total_written > max_uncompressed_bytes:
                        raise AnalysisValidationError(
                            "Analysis ZIP expands beyond the configured limit."
                        )
                    digest.update(chunk)
                    output.write(chunk)
            paths[info.filename] = destination
            sha256[info.filename] = digest.hexdigest()
            size_bytes[info.filename] = member_size

    manifest = _load_json_bytes(paths["manifest.json"].read_bytes(), "manifest.json")
    _validate_schema(
        manifest,
        "analysis-finalize-manifest.schema.json",
        "Analysis manifest",
    )
    if manifest.get("schema_version") != "1.0":
        raise AnalysisValidationError("Analysis manifest schema_version must be 1.0.")
    if manifest.get("run_id") != str(run.id) or manifest.get("session_id") != run.session_id:
        raise AnalysisValidationError("Analysis manifest identity does not match the Run.")
    if not isinstance(manifest.get("completed"), bool):
        raise AnalysisValidationError("Analysis manifest completed must be a boolean.")
    expected_fingerprint = analysis_source_fingerprint(run.results)
    if manifest.get("source_fingerprint") != expected_fingerprint:
        raise AnalysisValidationError(
            "Analysis manifest source_fingerprint does not match the Run results."
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(ANALYSIS_OUTPUT_FILES):
        raise AnalysisValidationError("Analysis manifest files are incomplete.")
    for name in ANALYSIS_OUTPUT_FILES:
        descriptor = files.get(name)
        if not isinstance(descriptor, dict) or set(descriptor) != {"sha256"}:
            raise AnalysisValidationError(
                f"Analysis manifest entry {name} must contain only sha256."
            )
        declared_sha256 = str(descriptor.get("sha256") or "").lower()
        if not _is_sha256(declared_sha256) or declared_sha256 != sha256[name]:
            raise AnalysisValidationError(f"Analysis manifest checksum failed for {name}.")

    state = validate_analysis_state(paths["analysis_state.json"], run)
    if state.source_fingerprint != expected_fingerprint:
        raise AnalysisValidationError("Analysis state and manifest sources disagree.")
    _validate_csv(paths["analysis.csv"], run)
    _validate_trainable_json(paths["trainable.json"], run)
    return ValidatedAnalysisBundle(
        manifest=manifest,
        state=state,
        paths=paths,
        sha256=sha256,
        size_bytes=size_bytes,
        completed=manifest["completed"],
        source_fingerprint=expected_fingerprint,
    )
