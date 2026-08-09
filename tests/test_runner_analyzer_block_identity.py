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
    initialize_cloud_run,
    get_block_run_identity,
    queue_cloud_run_failure,
)


class RunnerBlockIdentityTests(unittest.TestCase):
    def test_uncaught_runner_failure_is_queued_and_reported(self):
        run_id = str(uuid.uuid4())
        calls = []

        class API:
            def fail_run(self, target_run_id):
                calls.append(target_run_id)

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "result_upload_queue.queue_root", return_value=Path(temp_dir)
        ):
            outcome = queue_cloud_run_failure(
                [{"__server_run_id__": run_id}], api_factory=API
            )

        self.assertEqual(outcome, (0, []))
        self.assertEqual(calls, [run_id])

    def test_cloud_run_is_created_once_and_shared_by_all_blocks(self):
        experiment_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        configs = [
            {"experiment_id": experiment_id, "experiment_revision_id": revision_id},
            {"experiment_id": experiment_id, "experiment_revision_id": revision_id},
        ]
        calls = []

        class FakeAPI:
            def create_run(self, revision, session, number, age, gender):
                calls.append(("create", revision, session, number, age, gender))
                return {"id": "run-1"}

            def start_run(self, run_id):
                calls.append(("start", run_id))

        with patch("autoscript_api.AutoScriptAPI", return_value=FakeAPI()):
            first = initialize_cloud_run(configs, 12, 30, "Other")
            second = initialize_cloud_run(configs, 12, 30, "Other")
        self.assertEqual(first, "run-1")
        self.assertEqual(second, "run-1")
        self.assertEqual([call[0] for call in calls], ["create", "start"])
        self.assertTrue(all(config["__server_run_id__"] == "run-1" for config in configs))

    def test_test_mode_also_creates_a_cloud_run(self):
        experiment_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        configs = [
            {"experiment_id": experiment_id, "experiment_revision_id": revision_id}
        ]
        calls = []

        class FakeAPI:
            def create_run(self, revision, session, number, age, gender):
                calls.append(("create", revision, session, number, age, gender))
                return {"id": "test-run-1"}

            def start_run(self, run_id):
                calls.append(("start", run_id))

        with patch("autoscript_api.AutoScriptAPI", return_value=FakeAPI()):
            run_id = initialize_cloud_run(
                configs,
                12,
                30,
                "Other",
                test_mode=True,
            )

        self.assertEqual(run_id, "test-run-1")
        self.assertEqual([call[0] for call in calls], ["create", "start"])

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
            "experiment_revision_id": "revision-9",
            "experiment_revision_number": 3,
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
            test_mode=False,
            calibration_data={"corners": [[0, 0], [1, 0], [0, 1], [1, 1]]},
            words=[],
            pen_recorder=recorder,
            _is_time_mode=lambda: False,
        )

        result = ExperimentCanvas.collect_experiment_data(canvas)

        self.assertEqual(result["schema_version"], "1.3")
        self.assertEqual(result["experiment_revision_id"], "revision-9")
        self.assertEqual(result["experiment_revision_number"], 3)
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
        self.assertFalse(result["test_mode"])
        self.assertIn("+", result["recorded_at"])

    def test_cloud_result_upload_uses_parent_uuid_and_preserves_local_file(self):
        canvas = ExperimentCanvas.__new__(ExperimentCanvas)
        canvas.test_mode = False
        experiment_id = str(uuid.uuid4())
        uploaded = []

        class FakeAPI:
            def upload_result(self, target_experiment_id, result_path):
                uploaded.append((target_experiment_id, Path(result_path).read_text(encoding="utf-8")))
                return {"run_id": "run-1"}

            def finalize_run(self, run_id):
                uploaded.append(("finalize", run_id))

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            result_path.write_text("{}", encoding="utf-8")
            queue_dir = Path(temp_dir) / "queue"
            queue_dir.mkdir()
            with patch("autoscript_api.AutoScriptAPI", return_value=FakeAPI()), patch(
                "result_upload_queue.queue_root", return_value=queue_dir
            ):
                count, errors, skipped = canvas._upload_saved_results(
                    [(result_path, {"experiment_id": experiment_id})]
                )

            self.assertTrue(result_path.exists())
            self.assertEqual(uploaded[0][0], experiment_id)
            self.assertEqual(uploaded[0][1], "{}")
            self.assertEqual(uploaded[-1], ("finalize", "run-1"))
            self.assertEqual(list(queue_dir.glob("*.queue.json")), [])
            self.assertEqual((count, errors, skipped), (1, [], 0))

    def test_cloud_result_is_queued_before_optional_local_export(self):
        canvas = ExperimentCanvas.__new__(ExperimentCanvas)
        canvas.test_mode = False
        canvas._cloud_upload_outcome = None
        captured = []
        result = {
            "experiment_id": str(uuid.uuid4()),
            "server_run_id": str(uuid.uuid4()),
            "block_index": 1,
        }

        def upload_saved(staged, transition="finalize"):
            captured.append(
                (json.loads(Path(staged[0][0]).read_text(encoding="utf-8")), transition)
            )
            return 0, ["offline"], 0

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "app_paths.user_data_dir", return_value=Path(temp_dir)
        ), patch.object(canvas, "_upload_saved_results", side_effect=upload_saved):
            first = canvas._upload_results_before_export([result])
            second = canvas._upload_results_before_export([result])

        self.assertEqual(first, (0, ["offline"], 0))
        self.assertEqual(second, first)
        self.assertEqual(captured, [(result, "finalize")])

    def test_offline_api_creation_keeps_durable_queue_item(self):
        canvas = ExperimentCanvas.__new__(ExperimentCanvas)
        canvas.test_mode = False
        experiment_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_path = root / "result.json"
            result_path.write_text("{}", encoding="utf-8")
            queue_dir = root / "queue"
            queue_dir.mkdir()
            with patch(
                "autoscript_api.AutoScriptAPI", side_effect=RuntimeError("offline")
            ), patch("result_upload_queue.queue_root", return_value=queue_dir):
                uploaded, errors, skipped = canvas._upload_saved_results(
                    [(
                        result_path,
                        {"experiment_id": experiment_id, "server_run_id": run_id},
                    )]
                )

            self.assertEqual(uploaded, 0)
            self.assertTrue(errors)
            self.assertEqual(skipped, 0)
            self.assertEqual(len(list(queue_dir.glob("*.queue.json"))), 2)

    def test_local_legacy_is_skipped_but_test_run_is_uploaded(self):
        canvas = ExperimentCanvas.__new__(ExperimentCanvas)
        canvas.test_mode = False
        count, errors, skipped = canvas._upload_saved_results(
            [(Path("legacy.json"), {"experiment_id": "Legacy Study"})]
        )
        self.assertEqual((count, errors, skipped), (0, [], 1))

        calls = []

        class FakeAPI:
            def upload_result(self, experiment_id, result_path):
                calls.append(("result", experiment_id))
                return {"run_id": "test-run-1"}

            def finalize_run(self, run_id):
                calls.append(("finalize", run_id))

        canvas.test_mode = True
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result_path = root / "test.json"
            result_path.write_text("{}", encoding="utf-8")
            queue_dir = root / "queue"
            queue_dir.mkdir()
            experiment_id = str(uuid.uuid4())
            with patch("autoscript_api.AutoScriptAPI", return_value=FakeAPI()), patch(
                "result_upload_queue.queue_root", return_value=queue_dir
            ):
                count, errors, skipped = canvas._upload_saved_results(
                    [(result_path, {"experiment_id": experiment_id})]
                )

        self.assertEqual((count, errors, skipped), (1, [], 0))
        self.assertEqual(calls, [("result", experiment_id), ("finalize", "test-run-1")])


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
