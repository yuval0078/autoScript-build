import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analyzer_refactored import ParticipantData, _result_identity
from tablet_experiment import (
    ExperimentCanvas,
    ensure_shared_run_session_id,
    get_block_run_identity,
)


class RunnerBlockIdentityTests(unittest.TestCase):
    def test_legacy_config_is_one_block_experiment(self):
        identity = get_block_run_identity(
            {"name": "Legacy package"},
            session_index=0,
            session_total=1,
        )

        self.assertEqual(identity["experiment_name"], "Legacy package")
        self.assertEqual(identity["experiment_id"], "Legacy package")
        self.assertEqual(identity["block_name"], "Legacy package")
        self.assertIsNone(identity["block_id"])
        self.assertEqual(identity["block_index"], 1)
        self.assertEqual(identity["block_count"], 1)

    def test_new_metadata_separates_parent_experiment_and_block(self):
        identity = get_block_run_identity(
            {
                "name": "Old embedded block label",
                "experiment_name": "Reading study",
                "experiment_id": "experiment-7",
                "block_name": "Practice",
                "block_id": "block-3",
                "block_index": 2,
                "block_count": 4,
            },
            session_index=0,
            session_total=1,
        )

        self.assertEqual(
            identity,
            {
                "experiment_name": "Reading study",
                "experiment_id": "experiment-7",
                "block_name": "Practice",
                "block_id": "block-3",
                "block_index": 2,
                "block_count": 4,
            },
        )

    def test_all_blocks_share_one_run_session_id(self):
        configs = [{"name": "A"}, {"name": "B"}]

        session_id = ensure_shared_run_session_id(configs, participant_number=12)

        self.assertRegex(session_id, r"^12_\d{8}_\d{6}_[0-9a-f]{6}$")
        self.assertEqual(configs[0]["__run_session_id__"], session_id)
        self.assertEqual(configs[1]["__run_session_id__"], session_id)

    def test_collected_result_uses_parent_identity_and_block_compatibility_fields(self):
        config = {
            "experiment_name": "Reading study",
            "experiment_id": "experiment-7",
            "block_name": "Main task",
            "block_id": "block-4",
            "block_index": 2,
            "block_count": 3,
            "experiment_version": 5,
            "__run_session_id__": "12_20260807_120000_abcdef",
        }
        recorder = SimpleNamespace(current_word_data=None, all_word_data=[])
        canvas = SimpleNamespace(
            completed_data=None,
            config=config,
            session_index=0,
            session_total=1,
            participant_number=12,
            participant_age=30,
            participant_gender="Other",
            calibration_data={"corners": [[0, 0], [1, 0], [0, 1], [1, 1]]},
            words=[],
            pen_recorder=recorder,
            _is_time_mode=lambda: False,
        )

        result = ExperimentCanvas.collect_experiment_data(canvas)

        self.assertEqual(result["schema_version"], "1.2")
        self.assertTrue(result["block_completed"])
        self.assertEqual(result["completed_word_count"], 0)
        self.assertEqual(result["expected_word_count"], 0)
        self.assertEqual(result["experiment_name"], "Reading study")
        self.assertEqual(result["experiment_id"], "experiment-7")
        self.assertEqual(result["block_name"], "Main task")
        self.assertEqual(result["block_id"], "block-4")
        self.assertEqual(result["block_index"], 2)
        self.assertEqual(result["block_count"], 3)
        self.assertEqual(result["session_experiment_index"], 2)
        self.assertEqual(result["session_experiment_count"], 3)
        self.assertEqual(result["session_id"], "12_20260807_120000_abcdef")

    def test_cloud_result_upload_uses_parent_uuid_and_preserves_local_file(self):
        canvas = ExperimentCanvas.__new__(ExperimentCanvas)
        canvas.test_mode = False
        experiment_id = str(uuid.uuid4())
        uploaded = []

        class FakeAPI:
            def upload_result(self, target_experiment_id, result_path):
                uploaded.append((target_experiment_id, Path(result_path)))

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            result_path.write_text("{}", encoding="utf-8")
            with patch("autoscript_api.AutoScriptAPI", return_value=FakeAPI()):
                count, errors, skipped = canvas._upload_saved_results(
                    [(result_path, {"experiment_id": experiment_id})]
                )

            self.assertTrue(result_path.exists())
            self.assertEqual(uploaded, [(experiment_id, result_path)])
            self.assertEqual((count, errors, skipped), (1, [], 0))

    def test_local_legacy_and_test_runs_are_not_uploaded(self):
        canvas = ExperimentCanvas.__new__(ExperimentCanvas)
        canvas.test_mode = False
        count, errors, skipped = canvas._upload_saved_results(
            [(Path("legacy.json"), {"experiment_id": "Legacy Study"})]
        )
        self.assertEqual((count, errors, skipped), (0, [], 1))

        canvas.test_mode = True
        count, errors, skipped = canvas._upload_saved_results(
            [(Path("test.json"), {"experiment_id": str(uuid.uuid4())})]
        )
        self.assertEqual((count, errors, skipped), (0, [], 1))


class AnalyzerBlockIdentityTests(unittest.TestCase):
    def _participant_from_payload(self, payload):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "result.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return ParticipantData.from_file(str(path))

    def test_current_result_preserves_parent_and_block_identity(self):
        participant = self._participant_from_payload(
            {
                "participant_number": 9,
                "timestamp": "20260807_120000",
                "experiment_name": "Reading study",
                "experiment_id": "experiment-7",
                "block_name": "Practice",
                "block_id": "block-3",
                "block_index": 2,
                "block_count": 4,
                "session_id": "9_20260807_120000_abcdef",
                "words": [],
            }
        )

        self.assertEqual(participant.experiment_name, "Reading study")
        self.assertEqual(participant.experiment_id, "experiment-7")
        self.assertEqual(participant.block_name, "Practice")
        self.assertEqual(participant.block_id, "block-3")
        self.assertEqual(participant.block_index, 2)
        self.assertEqual(participant.block_count, 4)
        self.assertEqual(participant.session_id, "9_20260807_120000_abcdef")

    def test_legacy_result_uses_old_experiment_name_for_both_identities(self):
        identity = _result_identity(
            {
                "experiment_name": "Legacy package",
                "session_experiment_index": 2,
                "session_experiment_count": 3,
                "config": {"name": "Legacy package"},
            }
        )

        self.assertEqual(identity["experiment_name"], "Legacy package")
        self.assertEqual(identity["block_name"], "Legacy package")
        self.assertEqual(identity["block_index"], 2)
        self.assertEqual(identity["block_count"], 3)

    def test_old_array_only_analyzer_file_still_loads(self):
        participant = self._participant_from_payload([{"word": "old", "pen_events": []}])

        self.assertEqual(participant.participant_number, "Unknown")
        self.assertEqual(participant.experiment_name, "Unknown")
        self.assertEqual(participant.block_name, "Legacy data")
        self.assertEqual(len(participant.words), 1)


if __name__ == "__main__":
    unittest.main()
