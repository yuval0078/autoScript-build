"""Exercise ordered Block CRUD and bundle download against a running local API."""

import json
import csv
import hashlib
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from autoscript_api import AutoScriptAPI
from experiment_packages import unpack_experiment_package


def write_member(archive, name, content):
    info = zipfile.ZipInfo(name, date_time=(2026, 8, 7, 12, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, content)


def build_block(package_path, block_name):
    config = {
        "schema_version": "2.0",
        "package_type": "block",
        "app_version": "1.0.3.1",
        "name": block_name,
        "block_name": block_name,
        "grid": {"rows": 1, "cols": 2},
        "order": "stiff",
        "sequence": ["word-1"],
        "repetitions": {"group-a": 1},
        "active_block_sequence": [["group-a", 1]],
        "proceed_condition": {"type": "key", "delay_ms": 0},
        "beeps": {
            "before": {"enabled": False, "delay_ms": 0},
            "after": {"enabled": False, "delay_ms": 0},
        },
        "files": [
            {
                "file_name": "word.wav",
                "path": "media/word.wav",
                "original_name": "word.wav",
                "owner_group": "group-a",
                "auto_slice_word": True,
            }
        ],
        "groups": [
            {
                "name": "group-a",
                "words": [
                    {
                        "id": "word-1",
                        "text": "test",
                        "source_file": "word.wav",
                        "start_ms": 0,
                        "end_ms": 250,
                    }
                ],
            }
        ],
    }
    with zipfile.ZipFile(package_path, "w") as archive:
        write_member(
            archive,
            f"{block_name}.json",
            json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        write_member(archive, "media/word.wav", b"RIFF-autoscript-smoke")


def build_result(experiment, block, block_index, block_count, session_id, run_id):
    return {
        "schema_version": "1.3",
        "app_version": "1.0.3.1",
        "experiment_name": experiment["name"],
        "experiment_id": experiment["id"],
        "experiment_version": 1,
        "experiment_revision_id": experiment["current_revision"]["id"],
        "experiment_revision_number": experiment["current_revision"]["revision_number"],
        "server_run_id": run_id,
        "block_name": block["name"],
        "block_id": block["id"],
        "block_index": block_index,
        "block_count": block_count,
        "block_completed": True,
        "experiment_completed": True,
        "completed_word_count": 1,
        "expected_word_count": 1,
        "session_experiment_index": block_index,
        "session_experiment_count": block_count,
        "participant_number": 7,
        "participant_age": 25,
        "participant_gender": "Other",
        "session_id": session_id,
        "timestamp": "20260807_120000",
        "calibration": {
            "corners": [[10.0, 10.0], [100.0, 10.0], [100.0, 70.0], [10.0, 70.0]]
        },
        "config": {},
        "words": [
            {
                "word": "test",
                "cell": 0,
                "group": "group-a",
                "start_time": 1.0,
                "end_time": 2.0,
                "audio_start_time": 1.0,
                "audio_end_time": 1.5,
                "pen_events": [],
            }
        ],
    }


def build_analysis_bundle(bundle_path, experiment, run, result_files):
    ordered = sorted(result_files, key=lambda item: item[1]["block_index"])
    source_rows = [
        {
            "id": result["id"],
            "sha256": result["sha256"],
            "block_index": result["block_index"],
        }
        for _path, result in ordered
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            source_rows,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    state = {
        "schema_version": "1.1",
        "analyzer_version": "0.0",
        "run_id": run["id"],
        "experiment_id": experiment["id"],
        "session_id": run["session_id"],
        "source_fingerprint": fingerprint,
        "sources": [
            {
                "result_id": result["id"],
                "raw_sha256": result["sha256"],
                "session_id": run["session_id"],
                "block_id": result["block_id"],
                "block_index": result["block_index"],
                "block_name": result["block_name"],
                "word_count": result["completed_word_count"],
                "words": [
                    {
                        "letters": [],
                        "assigned_letters": {},
                        "stroke_slices": [],
                        "trainability": "trainable",
                        "written_word": "test",
                        "correct": True,
                    }
                    for _ in range(result["completed_word_count"])
                ],
            }
            for _path, result in ordered
        ],
    }
    trainable = [
        {
            "analyzer_version": "0.0",
            "run_id": run["id"],
            "source_result_id": result["id"],
            "source_sha256": result["sha256"],
            "experiment_name": experiment["name"],
            "experiment_id": experiment["id"],
            "block_name": result["block_name"],
            "block_id": result["block_id"],
            "block_index": result["block_index"],
            "block_count": run["block_count"],
            "session_id": run["session_id"],
            "participant_number": run["participant_number"],
            "participant_age": run["participant_age"],
            "participant_gender": run["participant_gender"],
            "timestamp": "20260807_120000",
            "calibration": {
                "corners": [
                    [10.0, 10.0],
                    [100.0, 10.0],
                    [100.0, 70.0],
                    [10.0, 70.0],
                ]
            },
            "group": "group-a",
            "words": [
                {
                    "written_word": "test",
                    "trainability": "trainable",
                    "audio_start_time": 1.0,
                    "audio_end_time": 1.5,
                    "strokes": [],
                    "letters": [],
                }
                for _ in range(result["completed_word_count"])
            ],
        }
        for _path, result in ordered
    ]
    with tempfile.TemporaryDirectory(prefix="autoscript-analysis-smoke-") as temp_dir:
        root = Path(temp_dir)
        state_path = root / "analysis_state.json"
        csv_path = root / "analysis.csv"
        trainable_path = root / "trainable.json"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        trainable_path.write_text(
            json.dumps(trainable, ensure_ascii=False), encoding="utf-8"
        )
        header = [
            "Exp Step", "Experiment", "Experiment ID", "Block", "Block ID",
            "Block Index", "Block Count", "Session ID", "Participant", "Age",
            "Gender", "Word", "Group", "Cell", "Correct", "Written Word",
            "Reading End", "Writing Start", "Writing End", "Strokes",
            "Avg Interval", "Screenshot File",
        ]
        with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(header)
            for _path, result in ordered:
                for word_index in range(result["completed_word_count"]):
                    writer.writerow(
                        [
                            word_index + 1,
                            experiment["name"],
                            experiment["id"],
                            result["block_name"],
                            result["block_id"],
                            result["block_index"],
                            run["block_count"],
                            run["session_id"],
                            run["participant_number"],
                            run["participant_age"],
                            run["participant_gender"],
                            "test",
                            "group-a",
                            0,
                            True,
                            "test",
                            1.0,
                            1.1,
                            2.0,
                            0,
                            0,
                            "",
                        ]
                    )
        sources = {
            "analysis_state.json": state_path,
            "analysis.csv": csv_path,
            "trainable.json": trainable_path,
        }
        manifest = {
            "schema_version": "1.0",
            "analyzer_version": "0.0",
            "run_id": run["id"],
            "session_id": run["session_id"],
            "completed": True,
            "source_fingerprint": fingerprint,
            "files": {
                name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in sources.items()
            },
        }
        with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
            for name, path in sources.items():
                archive.write(path, name)


def main():
    api = AutoScriptAPI()
    experiment_id = None
    duplicate_id = None
    experiment_name = f"structure-smoke-{uuid.uuid4().hex[:8]}"
    try:
        with tempfile.TemporaryDirectory(prefix="autoscript-api-smoke-") as temp_dir:
            temp_root = Path(temp_dir)
            package_paths = []
            for block_name in ("Warmup", "Task"):
                package_path = temp_root / f"{block_name}.zip"
                build_block(package_path, block_name)
                package_paths.append(package_path)

            staged = [
                api.stage_block(path, path.stem, request_id=uuid.uuid4())
                for path in package_paths
            ]
            experiment = api.publish_experiment(
                experiment_name,
                [
                    {
                        "source": "staged",
                        "id": staged[1]["id"],
                        "same_page_as_previous": False,
                    },
                    {
                        "source": "staged",
                        "id": staged[0]["id"],
                        "same_page_as_previous": True,
                    },
                ],
                uuid.uuid4(),
            )
            experiment_id = experiment["id"]
            revision = experiment["current_revision"]

            duplicate = api.duplicate_experiment(experiment_id)
            duplicate_id = duplicate["id"]
            if duplicate["name"] != f"{experiment_name} copy":
                raise RuntimeError("Duplicate naming did not follow the copy convention.")
            if [
                block["same_page_as_previous"] for block in duplicate["blocks"]
            ] != [False, True]:
                raise RuntimeError("Duplicate lost the saved same-page layout.")

            listed = next(
                item for item in api.list_experiments() if item["id"] == experiment_id
            )
            bundle_path = temp_root / "experiment.zip"
            api.download_experiment(listed, bundle_path)
            resolved = unpack_experiment_package(bundle_path, temp_root / "resolved")
            if resolved.name != experiment_name:
                raise RuntimeError("Experiment bundle lost its parent name.")
            if [block.name for block in resolved.blocks] != ["Task", "Warmup"]:
                raise RuntimeError("Experiment bundle did not preserve Block order.")
            if [
                block.same_page_as_previous for block in resolved.blocks
            ] != [False, True]:
                raise RuntimeError("Experiment bundle lost the saved same-page layout.")
            if resolved.blocks[0].archive_path.read_bytes() != package_paths[1].read_bytes():
                raise RuntimeError("Nested Block ZIP bytes changed during storage/download.")

            session_id = f"7_20260807_120000_{uuid.uuid4().hex[:6]}"
            run = api.create_run(revision["id"], session_id, 7, 25, "Other")
            api.start_run(run["id"])
            result_files = []
            for index, block in enumerate(listed["blocks"], start=1):
                result_path = temp_root / f"result-{index}.json"
                result_path.write_text(
                    json.dumps(
                        build_result(listed, block, index, 2, session_id, run["id"]),
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                uploaded_result = api.upload_run_result(run["id"], result_path)
                retried_result = api.upload_run_result(run["id"], result_path)
                if uploaded_result["id"] != retried_result["id"]:
                    raise RuntimeError("Exact result retry was not idempotent.")
                result_files.append((result_path, uploaded_result))

            finalized = api.finalize_run(run["id"])
            if finalized["status"] != "completed":
                raise RuntimeError("Explicit Run did not finalize as completed.")

            runs = api.list_experiment_runs(experiment_id)
            if len(runs) != 1 or not runs[0]["complete"]:
                raise RuntimeError("Block results were not grouped into one complete run.")
            downloaded_result = temp_root / "downloaded-result.json"
            api.download_run_result(result_files[0][1], downloaded_result)
            if downloaded_result.read_bytes() != result_files[0][0].read_bytes():
                raise RuntimeError("Raw result bytes changed during storage/download.")

            analysis_bundle = temp_root / "analysis-finalize.zip"
            build_analysis_bundle(analysis_bundle, listed, runs[0], result_files)
            draft_state = temp_root / "analysis-state.json"
            with zipfile.ZipFile(analysis_bundle, "r") as archive:
                draft_state.write_bytes(archive.read("analysis_state.json"))
            draft_revision = api.put_run_analysis_state(
                runs[0]["id"], draft_state, request_id=uuid.uuid4()
            )
            if draft_revision["revision"] != 1 or draft_revision["finalized"]:
                raise RuntimeError("Transactional Analyzer draft was not persisted.")
            analysis_revision = api.finalize_run_analysis(
                runs[0]["id"],
                analysis_bundle,
                base_etag=draft_revision["etag"],
                request_id=uuid.uuid4(),
            )
            if analysis_revision["revision"] != 2 or analysis_revision["completed"] is not True:
                raise RuntimeError("Atomic analysis finalization lost completion status.")
            refreshed_run = api.list_experiment_runs(experiment_id)[0]
            if refreshed_run["analysis_completed"] is not True:
                raise RuntimeError("Analysis completion status was not persisted.")
            if len(analysis_revision["artifacts"]) != 3:
                raise RuntimeError("Atomic Analyzer artifacts were not associated with one revision.")
            csv_artifact = next(
                artifact
                for artifact in analysis_revision["artifacts"]
                if artifact["kind"] == "analysis_csv"
            )
            downloaded_csv = temp_root / "downloaded-analysis.csv"
            api.download_run_artifact(csv_artifact, downloaded_csv)
            if not downloaded_csv.read_bytes().startswith(b"\xef\xbb\xbfExp Step"):
                raise RuntimeError("Analyzer CSV artifact changed during download.")

            print(
                f"OK: experiment={experiment_id} blocks=2 duplicate={duplicate_id} "
                f"order=Task,Warmup same_page=Warmup run={runs[0]['id']} "
                f"results=2 lifecycle=completed artifacts=3 analysis=draft+atomic-complete"
            )
    finally:
        if duplicate_id is not None:
            api.delete_experiment(duplicate_id)
        if experiment_id is not None:
            api.delete_experiment(experiment_id)


if __name__ == "__main__":
    main()
