import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analyzer_refactored import (
    ParticipantData,
    PenDataPlayer,
    apply_analysis_state,
    build_analysis_state,
    build_trainable_payload,
    write_analysis_csv,
)
from experiment_results import run_status


class ExperimentResultStatusTests(unittest.TestCase):
    def test_incomplete_data_is_red_even_if_analysis_completed(self):
        status, color, _background, data, analysis = run_status(
            {"complete": False, "analysis_completed": True}
        )
        self.assertEqual(status, "incomplete")
        self.assertEqual(color, "#c62828")
        self.assertEqual(data, "Data incomplete")
        self.assertEqual(analysis, "Analysis completed")

    def test_unstarted_and_partial_analysis_are_orange_and_explicit(self):
        for value, expected in (
            (None, "Analysis not started"),
            (False, "Analysis not completed"),
        ):
            with self.subTest(value=value):
                status, color, _background, data, analysis = run_status(
                    {"complete": True, "analysis_completed": value}
                )
                self.assertEqual(status, "analysis_pending")
                self.assertEqual(color, "#b26a00")
                self.assertEqual(data, "Data complete")
                self.assertEqual(analysis, expected)


class AnalyzerArtifactTests(unittest.TestCase):
    def participant(self):
        events = [
            {
                "type": "press",
                "x": 1.0,
                "y": 2.0,
                "pressure": 0.5,
                "timestamp": 0.0,
                "absolute_time": 100.0,
                "speed": 0.0,
            },
            {
                "type": "release",
                "x": 2.0,
                "y": 3.0,
                "pressure": 0.0,
                "timestamp": 0.1,
                "absolute_time": 100.1,
                "speed": 0.0,
            },
        ]
        return ParticipantData(
            file_path="result.json",
            participant_number=7,
            timestamp="20260807_120000",
            words=[
                {
                    "word": "a",
                    "group": "g",
                    "cell": 0,
                    "audio_start_time": 99.0,
                    "audio_end_time": 99.5,
                    "pen_events": events,
                    "assigned_letters": {"0": "a"},
                    "letters": [{"char": "a", "stroke_ids": [0]}],
                }
            ],
            experiment_name="Study",
            experiment_id="experiment-1",
            block_name="Task",
            block_id="block-1",
            block_index=1,
            block_count=1,
            session_id="7_20260807_120000_abcdef",
        )

    def test_context_exports_are_scoped_and_keep_run_identity(self):
        participant = self.participant()
        payload = build_trainable_payload(
            [participant], [0], {}, {}, {0: "trainable"}
        )
        self.assertEqual(payload["session_id"], participant.session_id)
        self.assertEqual(payload["words"][0]["written_word"], "a")

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "analysis.csv"
            write_analysis_csv([participant], [0], csv_path)
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0][7], "Session ID")
        self.assertEqual(rows[1][7], participant.session_id)
        self.assertEqual(rows[1][15], "a")

    def test_analysis_edit_state_round_trips_all_editable_word_fields(self):
        participant = self.participant()
        state = build_analysis_state(
            [participant], [0], {0: "a"}, {0: True}, {0: "low-quality"}
        )
        restored = self.participant()
        restored.words[0].pop("letters")
        restored.words[0].pop("assigned_letters")
        restored.words[0]["stroke_slices"] = []
        written, correctness, train_mode = {}, {}, {}
        count = apply_analysis_state(
            [restored], state, written, correctness, train_mode
        )
        self.assertEqual(count, 1)
        self.assertEqual(restored.words[0]["assigned_letters"], {"0": "a"})
        self.assertEqual(restored.words[0]["letters"][0]["char"], "a")
        self.assertEqual(written[0], "a")
        self.assertTrue(correctness[0])
        self.assertEqual(train_mode[0], "low-quality")

    def test_cloud_finalize_uploads_edit_state_and_exports_before_status(self):
        participant = self.participant()
        calls = []

        class FakeAPI:
            def __init__(self, base_url=None, timeout=0):
                calls.append(("connect", base_url, timeout))

            def upload_run_artifact(self, run_id, kind, path):
                self_path = Path(path)
                if not self_path.is_file():
                    raise AssertionError("Expected generated artifact file.")
                calls.append(("artifact", run_id, kind, self_path.suffix))

            def update_run_analysis(self, run_id, completed):
                calls.append(("status", run_id, completed))

        with tempfile.TemporaryDirectory() as temp_dir:
            player = PenDataPlayer.__new__(PenDataPlayer)
            player.analysis_context = {
                "api_url": "http://127.0.0.1:8000",
                "output_dir": temp_dir,
                "runs": [{"id": "run-1", "session_id": participant.session_id}],
            }
            player.current_word_index = -1
            player.participants = [participant]
            player.written_words = {}
            player.word_correctness = {}
            player.train_mode = {}
            with patch("autoscript_api.AutoScriptAPI", FakeAPI):
                player._finalize_cloud_analysis(True)

        self.assertEqual(
            [call[2] for call in calls if call[0] == "artifact"],
            ["analysis_state", "analysis_csv", "trainable_json"],
        )
        self.assertEqual(calls[-1], ("status", "run-1", True))


if __name__ == "__main__":
    unittest.main()
