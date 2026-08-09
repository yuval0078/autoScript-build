import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = REPO_ROOT / "schemas" / "data-contracts"


def load_validator(schema_name):
    registry = Registry()
    schemas = {}
    for schema_path in SCHEMA_ROOT.glob("*.schema.json"):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schemas[schema_path.name] = schema
        registry = registry.with_resource(
            schema["$id"],
            Resource.from_contents(schema),
        )
    return Draft202012Validator(schemas[schema_name], registry=registry)


def pen_event(event_type, timestamp, pressure):
    return {
        "type": event_type,
        "x": 100.0 + timestamp,
        "y": 200.0 + timestamp,
        "pressure": pressure,
        "timestamp": timestamp,
        "absolute_time": 1_700_000_000.0 + timestamp,
        "speed": 0.0,
    }


def calibration_fixture():
    return {
        "corners": [
            [10.0, 10.0],
            [1010.0, 10.0],
            [1010.0, 710.0],
            [10.0, 710.0],
        ]
    }


def experiment_fixture():
    return {
        "app_version": "1.0.3.1",
        "name": "contract_fixture",
        "grid": {"rows": 1, "cols": 1},
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


def raw_run_fixture():
    return {
        "schema_version": "1.2",
        "app_version": "1.0.3.1",
        "experiment_name": "contract_fixture",
        "experiment_id": "contract_fixture",
        "experiment_version": 1,
        "block_name": "practice",
        "block_id": "block-1",
        "block_index": 1,
        "block_count": 1,
        "block_completed": True,
        "experiment_completed": True,
        "completed_word_count": 1,
        "expected_word_count": 1,
        "session_experiment_index": 1,
        "session_experiment_count": 1,
        "participant_number": 7,
        "participant_age": 25,
        "participant_gender": "Other",
        "session_id": "7_20260807_120000_abcdef",
        "timestamp": "20260807_120000",
        "calibration": calibration_fixture(),
        "config": experiment_fixture(),
        "words": [
            {
                "word": "test",
                "cell": 0,
                "group": "group-a",
                "start_time": 1_700_000_000.0,
                "end_time": 1_700_000_001.0,
                "audio_start_time": 1_700_000_000.0,
                "audio_end_time": 1_700_000_000.25,
                "pen_events": [
                    pen_event("press", 0.0, 0.5),
                    pen_event("release", 0.1, 0.0),
                ],
            }
        ],
    }


def trainable_export_fixture():
    return {
        "participant_number": 7,
        "participant_age": 25,
        "participant_gender": "Other",
        "timestamp": "20260807_120000",
        "calibration": calibration_fixture(),
        "group": "group-a",
        "words": [
            {
                "written_word": "test",
                "trainability": "trainable",
                "audio_start_time": 1_700_000_000.0,
                "audio_end_time": 1_700_000_000.25,
                "strokes": [
                    {
                        "stroke_id": 0,
                        "events": [
                            pen_event("press", 0.0, 0.5),
                            pen_event("release", 0.1, 0.0),
                        ],
                    }
                ],
                "letters": [{"char": "t", "stroke_ids": [0]}],
            }
        ],
    }


class DataContractFixtureTests(unittest.TestCase):
    def test_experiment_zip_layout_and_embedded_json_are_valid(self):
        config = experiment_fixture()
        validator = load_validator("experiment-package.schema.json")

        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = Path(temp_dir) / "contract_fixture.zip"
            with zipfile.ZipFile(zip_path, "w") as package:
                package.writestr(
                    "contract_fixture.json",
                    json.dumps(config, ensure_ascii=False),
                )
                package.writestr("media/word.wav", b"RIFF-fixture")

            with zipfile.ZipFile(zip_path) as package:
                members = package.namelist()
                json_members = [name for name in members if name.endswith(".json")]
                self.assertEqual(json_members, ["contract_fixture.json"])
                loaded_config = json.loads(package.read(json_members[0]))
                validator.validate(loaded_config)
                for media in loaded_config["files"]:
                    self.assertIn(media["path"], members)

    def test_raw_run_file_is_valid(self):
        validator = load_validator("raw-run.schema.json")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "raw-run.json"
            path.write_text(
                json.dumps(raw_run_fixture(), ensure_ascii=False),
                encoding="utf-8",
            )
            validator.validate(json.loads(path.read_text(encoding="utf-8")))

    def test_legacy_raw_run_without_block_identity_remains_valid(self):
        validator = load_validator("raw-run.schema.json")
        payload = raw_run_fixture()
        payload["schema_version"] = "1.0"
        for key in (
            "block_name",
            "block_id",
            "block_index",
            "block_count",
            "block_completed",
            "experiment_completed",
            "completed_word_count",
            "expected_word_count",
        ):
            payload.pop(key)
        validator.validate(payload)

    def test_current_raw_run_requires_revision_and_server_run_identity(self):
        validator = load_validator("raw-run.schema.json")
        payload = raw_run_fixture()
        payload.update(
            {
                "schema_version": "1.3",
                "experiment_revision_id": "revision-1",
                "experiment_revision_number": 1,
                "server_run_id": "run-1",
            }
        )
        validator.validate(payload)
        for field in (
            "block_name",
            "block_id",
            "block_index",
            "block_count",
            "block_completed",
            "experiment_completed",
            "completed_word_count",
            "expected_word_count",
            "experiment_revision_id",
            "experiment_revision_number",
            "server_run_id",
        ):
            with self.subTest(field=field):
                candidate = dict(payload)
                candidate.pop(field)
                self.assertTrue(list(validator.iter_errors(candidate)))

    def test_single_and_multi_participant_trainable_files_are_valid(self):
        validator = load_validator("trainable-export.schema.json")
        participant = trainable_export_fixture()
        with tempfile.TemporaryDirectory() as temp_dir:
            for name, payload in (
                ("single.json", participant),
                ("multiple.json", [participant, participant]),
            ):
                with self.subTest(name=name):
                    path = Path(temp_dir) / name
                    path.write_text(
                        json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    validator.validate(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
