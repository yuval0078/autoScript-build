"""Build a deterministic, checksummed historical-Run import archive.

The source raw ZIPs are read without extracting them. Encoded exports are kept
byte-exact, while screenshots are bundled into one ZIP artifact per participant.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo


FORMAT_VERSION = "1.0"
MAX_SOURCE_MEMBER_BYTES = 100 * 1024 * 1024
PARTICIPANT_PATTERN = re.compile(r"^[1-9][0-9]*$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip_info(name: str, *, stored: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _safe_source_members(archive: zipfile.ZipFile):
    names = set()
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or path.is_absolute()
            or "\\" in info.filename
            or any(part in {"", ".", ".."} for part in path.parts)
            or len(path.parts) != 2
            or path.suffix.lower() != ".json"
            or not PARTICIPANT_PATTERN.fullmatch(path.parts[0])
            or info.file_size > MAX_SOURCE_MEMBER_BYTES
            or info.filename in names
        ):
            raise ValueError(f"Unsafe or unsupported raw ZIP member: {info.filename!r}")
        names.add(info.filename)
        yield info, path


def _utc_timestamp(value: str) -> str:
    local = datetime.strptime(value, "%Y%m%d_%H%M%S").replace(
        tzinfo=ZoneInfo("Asia/Jerusalem")
    )
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_raw_archives(paths: list[Path]):
    grouped = defaultdict(list)
    seen_sha256 = set()
    for archive_path in sorted(paths, key=lambda item: item.name):
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ValueError(f"CRC validation failed for {archive_path.name}.")
            for info, member_path in _safe_source_members(archive):
                data = archive.read(info)
                digest = _sha256(data)
                if digest in seen_sha256:
                    raise ValueError(f"Duplicate raw JSON content: {info.filename}")
                seen_sha256.add(digest)
                try:
                    payload = json.loads(data.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"Invalid raw JSON: {info.filename}") from exc
                participant = int(member_path.parts[0])
                required = {
                    "schema_version",
                    "experiment_name",
                    "session_experiment_index",
                    "session_experiment_count",
                    "participant_number",
                    "participant_age",
                    "participant_gender",
                    "timestamp",
                    "words",
                }
                if not isinstance(payload, dict) or not required.issubset(payload):
                    raise ValueError(f"Raw JSON is missing required legacy fields: {info.filename}")
                if payload["participant_number"] != participant:
                    raise ValueError(f"Participant folder/content mismatch: {info.filename}")
                if not isinstance(payload["words"], list):
                    raise ValueError(f"Raw words must be an array: {info.filename}")
                grouped[participant].append(
                    {
                        "data": data,
                        "sha256": digest,
                        "source_archive": archive_path.name,
                        "source_name": info.filename,
                        "payload": payload,
                    }
                )
    return grouped


def _screenshot_archive(screenshot_dir: Path) -> tuple[bytes, int]:
    screenshots = sorted(
        (path for path in screenshot_dir.iterdir() if path.is_file()),
        key=lambda item: item.name.casefold(),
    )
    if not screenshots:
        raise ValueError(f"No screenshots found in {screenshot_dir}.")
    names = set()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", allowZip64=True) as archive:
        for screenshot in screenshots:
            if screenshot.name in names or screenshot.name in {".", ".."}:
                raise ValueError(f"Duplicate/unsafe screenshot name: {screenshot.name}")
            names.add(screenshot.name)
            archive.writestr(_zip_info(screenshot.name, stored=True), screenshot.read_bytes())
    return buffer.getvalue(), len(screenshots)


def build_archive(
    raw_zip_paths: list[Path],
    encoded_dir: Path,
    output_path: Path,
    *,
    experiment_name: str,
    source_id: str,
):
    grouped = _read_raw_archives(raw_zip_paths)
    encoded_participants = {
        int(path.name): path
        for path in encoded_dir.iterdir()
        if path.is_dir() and PARTICIPANT_PATTERN.fullmatch(path.name)
    }
    unknown_encoded = sorted(set(encoded_participants) - set(grouped))
    if unknown_encoded:
        raise ValueError(f"Encoded participants have no raw data: {unknown_encoded}")

    entries: dict[str, bytes] = {}
    runs = []
    word_counts_by_index = defaultdict(set)
    block_count = None
    for participant, raw_items in sorted(grouped.items()):
        raw_items.sort(key=lambda item: int(item["payload"]["session_experiment_index"]))
        indices = [int(item["payload"]["session_experiment_index"]) for item in raw_items]
        counts = {int(item["payload"]["session_experiment_count"]) for item in raw_items}
        if len(counts) != 1 or indices != list(range(1, next(iter(counts)) + 1)):
            raise ValueError(f"Participant {participant} does not contain one complete Block sequence.")
        current_block_count = next(iter(counts))
        if block_count is None:
            block_count = current_block_count
        elif block_count != current_block_count:
            raise ValueError("Participants disagree about the number of Blocks.")
        first = raw_items[0]["payload"]
        for item in raw_items:
            payload = item["payload"]
            if any(
                payload[field] != first[field]
                for field in ("participant_number", "participant_age", "participant_gender")
            ):
                raise ValueError(f"Participant metadata changes between Blocks for {participant}.")

        raw_manifest = []
        for item in raw_items:
            payload = item["payload"]
            index = int(payload["session_experiment_index"])
            word_count = len(payload["words"])
            word_counts_by_index[index].add(word_count)
            original_filename = PurePosixPath(item["source_name"]).name
            archive_path = f"runs/{participant}/raw/{index:04d}-{original_filename}"
            entries[archive_path] = item["data"]
            raw_manifest.append(
                {
                    "path": archive_path,
                    "original_filename": original_filename,
                    "source_archive": item["source_archive"],
                    "source_block_name": str(payload["experiment_name"]),
                    "block_index": index,
                    "schema_version": str(payload["schema_version"]),
                    "app_version": str(payload.get("app_version") or "legacy"),
                    "result_timestamp": str(payload["timestamp"]),
                    "completed_word_count": word_count,
                    "expected_word_count": word_count,
                    "sha256": item["sha256"],
                    "size_bytes": len(item["data"]),
                }
            )

        artifacts = []
        encoded_path = encoded_participants.get(participant)
        if encoded_path is not None:
            trainable = list(encoded_path.glob("*trainable*.json"))
            csv_files = list(encoded_path.glob("*.csv"))
            screenshot_dir = encoded_path / "screenshots"
            if len(trainable) != 1 or len(csv_files) != 1 or not screenshot_dir.is_dir():
                raise ValueError(f"Encoded exports are incomplete/ambiguous for {participant}.")
            artifact_sources = [
                ("analysis_csv", csv_files[0], csv_files[0].read_bytes()),
                ("trainable_json", trainable[0], trainable[0].read_bytes()),
            ]
            screenshots_bytes, screenshot_count = _screenshot_archive(screenshot_dir)
            artifact_sources.append(
                ("screenshots_zip", Path(f"participant_{participant}_screenshots.zip"), screenshots_bytes)
            )
            for kind, source_path, data in artifact_sources:
                suffix = {"analysis_csv": ".csv", "trainable_json": ".json", "screenshots_zip": ".zip"}[kind]
                archive_path = f"runs/{participant}/artifacts/{kind}{suffix}"
                entries[archive_path] = data
                artifact = {
                    "kind": kind,
                    "path": archive_path,
                    "original_filename": source_path.name,
                    "sha256": _sha256(data),
                    "size_bytes": len(data),
                }
                if kind == "screenshots_zip":
                    artifact["file_count"] = screenshot_count
                artifacts.append(artifact)

        timestamps = [item["payload"]["timestamp"] for item in raw_items]
        date = min(timestamps).split("_", 1)[0]
        runs.append(
            {
                "session_id": f"historical-{experiment_name.lower()}-p{participant}-{date}",
                "participant_number": participant,
                "participant_age": int(first["participant_age"]),
                "participant_gender": str(first["participant_gender"]),
                "started_at": _utc_timestamp(min(timestamps)),
                "finalized_at": _utc_timestamp(max(timestamps)),
                "raw_results": raw_manifest,
                "artifacts": artifacts,
            }
        )

    inconsistent = {
        index: sorted(counts)
        for index, counts in word_counts_by_index.items()
        if len(counts) != 1
    }
    if inconsistent:
        raise ValueError(f"Block word counts are inconsistent: {inconsistent}")
    expected_counts = [next(iter(word_counts_by_index[index])) for index in range(1, block_count + 1)]
    manifest = {
        "format_version": FORMAT_VERSION,
        "experiment_name": experiment_name,
        "source_id": source_id,
        "expected_block_word_counts": expected_counts,
        "run_count": len(runs),
        "runs": runs,
    }
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", allowZip64=True) as output:
        output.writestr(_zip_info("manifest.json"), manifest_bytes)
        for name, data in sorted(entries.items()):
            output.writestr(_zip_info(name), data)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-zip", action="append", required=True, type=Path)
    parser.add_argument("--encoded-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--experiment-name", default="pilot")
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    manifest = build_archive(
        args.raw_zip,
        args.encoded_dir,
        args.output,
        experiment_name=args.experiment_name,
        source_id=args.source_id,
    )
    analyzed = sum(bool(run["artifacts"]) for run in manifest["runs"])
    screenshots = sum(
        artifact.get("file_count", 0)
        for run in manifest["runs"]
        for artifact in run["artifacts"]
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "runs": manifest["run_count"],
                "raw_results": sum(len(run["raw_results"]) for run in manifest["runs"]),
                "analyzed_runs": analyzed,
                "screenshots": screenshots,
                "expected_block_word_counts": manifest["expected_block_word_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
