"""Exercise ordered Block CRUD and bundle download against a running local API."""

import json
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


def build_result(experiment, block, block_index, block_count, session_id):
    return {
        "schema_version": "1.2",
        "app_version": "1.0.3.1",
        "experiment_name": experiment["name"],
        "experiment_id": experiment["id"],
        "experiment_version": 1,
        "block_name": block["name"],
        "block_id": block["id"],
        "block_index": block_index,
        "block_count": block_count,
        "block_completed": True,
        "experiment_completed": True,
        "completed_word_count": 0,
        "expected_word_count": 0,
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
        "words": [],
    }


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

            experiment = api.create_experiment(experiment_name)
            experiment_id = experiment["id"]
            uploaded = [
                api.upload_block(experiment_id, path, path.stem, position)
                for position, path in enumerate(package_paths)
            ]
            api.reorder_blocks(
                experiment_id,
                [uploaded[1]["id"], uploaded[0]["id"]],
                same_page_block_ids=[uploaded[0]["id"]],
            )

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
            result_files = []
            for index, block in enumerate(listed["blocks"], start=1):
                result_path = temp_root / f"result-{index}.json"
                result_path.write_text(
                    json.dumps(
                        build_result(listed, block, index, 2, session_id),
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                uploaded_result = api.upload_result(experiment_id, result_path)
                retried_result = api.upload_result(experiment_id, result_path)
                if uploaded_result["id"] != retried_result["id"]:
                    raise RuntimeError("Exact result retry was not idempotent.")
                result_files.append((result_path, uploaded_result))

            runs = api.list_experiment_runs(experiment_id)
            if len(runs) != 1 or not runs[0]["complete"]:
                raise RuntimeError("Block results were not grouped into one complete run.")
            downloaded_result = temp_root / "downloaded-result.json"
            api.download_run_result(result_files[0][1], downloaded_result)
            if downloaded_result.read_bytes() != result_files[0][0].read_bytes():
                raise RuntimeError("Raw result bytes changed during storage/download.")

            csv_path = temp_root / "analysis.csv"
            csv_path.write_text("Participant,Word\n7,test\n", encoding="utf-8")
            trainable_path = temp_root / "trainable.json"
            trainable_path.write_text('{"words": []}', encoding="utf-8")
            csv_artifact = api.upload_run_artifact(
                runs[0]["id"], "analysis_csv", csv_path
            )
            api.upload_run_artifact(
                runs[0]["id"], "trainable_json", trainable_path
            )
            api.update_run_analysis(runs[0]["id"], True)
            refreshed_run = api.list_experiment_runs(experiment_id)[0]
            if refreshed_run["analysis_completed"] is not True:
                raise RuntimeError("Analysis completion status was not persisted.")
            if len(refreshed_run["artifacts"]) != 2:
                raise RuntimeError("Analyzer artifacts were not associated with the run.")
            downloaded_csv = temp_root / "downloaded-analysis.csv"
            api.download_run_artifact(csv_artifact, downloaded_csv)
            if downloaded_csv.read_bytes() != csv_path.read_bytes():
                raise RuntimeError("Analyzer artifact bytes changed during download.")

            print(
                f"OK: experiment={experiment_id} blocks=2 duplicate={duplicate_id} "
                f"order=Task,Warmup same_page=Warmup run={runs[0]['id']} "
                f"results=2 artifacts=2 analysis=complete"
            )
    finally:
        if duplicate_id is not None:
            api.delete_experiment(duplicate_id)
        if experiment_id is not None:
            api.delete_experiment(experiment_id)


if __name__ == "__main__":
    main()
