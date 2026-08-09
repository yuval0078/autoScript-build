import csv
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtWidgets import QApplication, QMessageBox

from autoscript_api import APIError
from analyzer_refactored import (
    ParticipantData,
    PenDataPlayer,
    apply_analysis_state,
    build_analysis_state,
    build_trainable_payload,
    write_analysis_finalize_bundle,
    write_analysis_csv,
)
from experiment_results import (
    ExperimentResultsPage,
    analysis_copy_count,
    cloud_file_count,
    format_run_datetime,
    run_cloud_file_tags,
)


class ExperimentResultCardTests(unittest.TestCase):
    def test_run_timestamp_is_rendered_for_the_results_row(self):
        rendered = format_run_datetime("2026-08-09T10:45:00+00:00")

        self.assertRegex(rendered, r"^2026-08-09 \d{2}:45:00$")

    def test_tags_are_driven_by_cloud_file_counts_not_editing_status(self):
        run = {
            "complete": False,
            "analysis_completed": None,
            "raw_data_count": 2,
            "analyzed_csv_count": 1,
            "trainable_json_count": 0,
        }
        self.assertEqual(
            run_cloud_file_tags(run),
            [("raw_data", "Raw data", 2), ("analysis_csv", "Analyzed CSV", 1)],
        )

    def test_tag_counts_fall_back_to_legacy_embedded_files(self):
        run = {
            "results": [{"id": "raw-1"}],
            "artifacts": [
                {"id": "csv-1", "kind": "analysis_csv"},
                {"id": "json-1", "kind": "trainable_json"},
            ],
        }
        self.assertEqual(cloud_file_count(run, "raw_data"), 1)
        self.assertEqual(
            [label for _kind, label, _count in run_cloud_file_tags(run)],
            ["Raw data", "Analyzed CSV", "Trainable Json"],
        )
        self.assertEqual(analysis_copy_count(run), 1)

    def test_versioned_copies_are_newest_first_and_do_not_duplicate_flat_artifacts(self):
        class API:
            def list_run_analysis_copies(self, run_id):
                self.requested_run_id = run_id
                return [{
                    "id": "revision-2",
                    "revision": 2,
                    "created_at": "2026-08-09T12:00:00Z",
                    "is_current_editable": True,
                    "analyzed_csv": {"id": "csv-2", "kind": "analysis_csv"},
                    "trainable_json": {"id": "json-2", "kind": "trainable_json"},
                }]

        page = ExperimentResultsPage.__new__(ExperimentResultsPage)
        page.api = API()
        copies = page._analysis_copies({
            "id": "run-1",
            "analysis_completed": True,
            "artifacts": [
                {
                    "id": "csv-2",
                    "kind": "analysis_csv",
                    "created_at": "2026-08-09T12:00:00Z",
                },
                {
                    "id": "legacy-csv",
                    "kind": "analysis_csv",
                    "created_at": "2026-08-08T12:00:00Z",
                },
            ],
        }, "analysis_csv")

        self.assertEqual(page.api.requested_run_id, "run-1")
        self.assertEqual(
            [copy["artifact"]["id"] for copy in copies],
            ["csv-2", "legacy-csv"],
        )
        self.assertTrue(copies[0]["is_current_editable"])
        self.assertTrue(copies[1]["legacy"])


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

    def test_cloud_csv_keeps_words_without_pen_events_for_source_coverage(self):
        participant = self.participant()
        participant.words.append({
            "word": "empty", "group": "g", "cell": 1,
            "audio_start_time": None, "audio_end_time": None, "pen_events": [],
        })
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "analysis.csv"
            write_analysis_csv([participant], [0], csv_path)
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2][11], "empty")
        self.assertEqual(rows[2][19], "0")

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

    def test_analysis_edit_state_refuses_wrong_exact_source_sha(self):
        participant = self.participant()
        participant.source_result_id = "result-1"
        participant.source_sha256 = "a" * 64
        state = build_analysis_state(
            [participant], [0], {0: "a"}, {0: True}, {0: "trainable"},
            run_id="run-1",
        )
        restored = self.participant()
        restored.source_result_id = "result-1"
        restored.source_sha256 = "b" * 64
        count = apply_analysis_state([restored], state, {}, {}, {})
        self.assertEqual(count, 0)

    def test_two_legacy_arrays_restore_by_exact_sha_not_shared_legacy_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_path = root / "first.json"
            second_path = root / "second.json"
            first_path.write_text('[{"word":"one","pen_events":[]}]', encoding="utf-8")
            second_path.write_text('[{"word":"two","pen_events":[]}]', encoding="utf-8")
            first = ParticipantData.from_file(str(first_path))
            second = ParticipantData.from_file(str(second_path))
            first.words[0]["assigned_letters"] = {"0": "a"}
            second.words[0]["assigned_letters"] = {"0": "b"}
            state = build_analysis_state([first, second], [0, 1], {}, {}, {})
            restored = [
                ParticipantData.from_file(str(first_path)),
                ParticipantData.from_file(str(second_path)),
            ]
            count = apply_analysis_state(restored, state, {}, {}, {})
        self.assertEqual(count, 2)
        self.assertEqual(restored[0].words[0]["assigned_letters"], {"0": "a"})
        self.assertEqual(restored[1].words[0]["assigned_letters"], {"0": "b"})

    def test_local_draft_survives_later_exact_cloud_association_without_cloud_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "legacy.json"
            source.write_text('[{"word":"one","pen_events":[]}]', encoding="utf-8")
            participant = ParticipantData.from_file(str(source))
            state = build_analysis_state([participant], [0], {0: "edited"}, {0: False}, {})
            from analysis_local_state import save_local_analysis_state

            data_root = root / "data"
            with patch("analysis_local_state.user_data_dir", return_value=data_root):
                save_local_analysis_state([source], state)
                player = PenDataPlayer.__new__(PenDataPlayer)
                player.analysis_context = None
                player.participants = [ParticipantData.from_file(str(source))]
                player.written_words = {}
                player.word_correctness = {}
                player.train_mode = {}
                player._pending_analysis_states = []
                restored = player._restore_local_analysis_state()

            class API:
                base_url = "http://127.0.0.1:8000"

                def __init__(self, timeout=0):
                    pass

                def resolve_run_results_by_sha(self, values):
                    return {"results": [{
                        "id": "result-1", "run_id": "run-1",
                        "sha256": values[0], "block_index": 1,
                    }], "missing_sha256": []}

                def get_experiment_run(self, run_id):
                    return {
                        "id": run_id, "experiment_id": "experiment-1",
                        "session_id": None, "source_experiment_name": "Legacy",
                    }

                def get_run_analysis_state(self, run_id):
                    raise APIError("none", status_code=404)

            with patch("autoscript_api.AutoScriptAPI", API), patch(
                "app_paths.user_data_dir", return_value=data_root
            ):
                associated = player._try_associate_manual_sources()
        self.assertEqual(restored, 1)
        self.assertTrue(associated)
        self.assertEqual(player.written_words[0], "edited")

    def test_same_session_cloud_runs_are_partitioned_by_server_run_id(self):
        first = self.participant()
        second = self.participant()
        first.server_run_id = "run-1"
        second.server_run_id = "run-2"
        player = PenDataPlayer.__new__(PenDataPlayer)
        player.participants = [first, second]
        self.assertEqual(
            player._participant_indices_for_run({
                "id": "run-2", "session_id": first.session_id,
            }),
            [1],
        )

    def test_cloud_finalize_uploads_edit_state_and_exports_before_status(self):
        participant = self.participant()
        calls = []

        class FakeAPI:
            def __init__(self, base_url=None, timeout=0):
                calls.append(("connect", base_url, timeout))

            def finalize_run_analysis(self, run_id, path, **kwargs):
                self_path = Path(path)
                if not self_path.is_file():
                    raise AssertionError("Expected generated finalization bundle.")
                if not zipfile.is_zipfile(self_path):
                    raise AssertionError("Expected a ZIP finalization bundle.")
                calls.append(("finalize", run_id, ".zip", kwargs))
                return {"etag": '"r1"', "revision": 1}

        with tempfile.TemporaryDirectory() as temp_dir:
            player = PenDataPlayer.__new__(PenDataPlayer)
            player.analysis_context = {
                "api_url": "http://127.0.0.1:8000",
                "output_dir": temp_dir,
                "runs": [{
                    "id": "run-1", "session_id": participant.session_id,
                    "analysis_etag": None,
                }],
            }
            player.current_word_index = -1
            player.participants = [participant]
            player.written_words = {}
            player.word_correctness = {}
            player.train_mode = {}
            queue_root = Path(temp_dir) / "queue"
            queue_root.mkdir()
            with patch("autoscript_api.AutoScriptAPI", FakeAPI), patch(
                "analysis_sync_queue.queue_root", return_value=queue_root
            ):
                player._finalize_cloud_analysis(
                    True, existing_policy="replace"
                )

        self.assertEqual(len([call for call in calls if call[0] == "finalize"]), 1)
        self.assertEqual(calls[-1][1:3], ("run-1", ".zip"))
        self.assertEqual(calls[-1][3]["existing_policy"], "replace")

    def test_close_without_exports_saves_only_editable_state(self):
        class Event:
            accepted = False
            ignored = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.ignored = True

        player = PenDataPlayer.__new__(PenDataPlayer)
        player.analysis_context = {"runs": [{"id": "run-1"}]}
        player.analysis_context_saved = False
        event = Event()
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.No
        ), patch.object(
            player, "_save_cloud_analysis_state", return_value=[]
        ) as save_state, patch.object(
            player, "_finalize_cloud_analysis"
        ) as finalize, patch.object(
            QApplication, "setOverrideCursor"
        ), patch.object(
            QApplication, "restoreOverrideCursor"
        ):
            player.closeEvent(event)
        save_state.assert_called_once_with()
        finalize.assert_not_called()
        self.assertTrue(event.accepted)

    def test_close_with_exports_uses_selected_duplicate_policy(self):
        class Event:
            accepted = False

            def accept(self):
                self.accepted = True

            def ignore(self):
                raise AssertionError("Close should not be cancelled")

        player = PenDataPlayer.__new__(PenDataPlayer)
        player.analysis_context = {
            "runs": [{"id": "run-1", "analysis_copy_count": 2}]
        }
        player.analysis_context_saved = False
        event = Event()
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.Yes
        ), patch.object(
            player, "_choose_existing_analysis_policy", return_value="keep"
        ), patch.object(
            player, "_finalize_cloud_analysis", return_value=(1, [], {})
        ) as finalize, patch.object(
            QApplication, "setOverrideCursor"
        ), patch.object(
            QApplication, "restoreOverrideCursor"
        ):
            player.closeEvent(event)
        finalize.assert_called_once_with(completed=True, existing_policy="keep")
        self.assertTrue(event.accepted)

    def test_finalize_bundle_contains_only_manifest_and_three_checksummed_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = root / "state.json"
            csv_path = root / "analysis.csv"
            trainable = root / "trainable.json"
            bundle = root / "final.zip"
            state.write_text('{"schema_version":"1.1"}', encoding="utf-8")
            csv_path.write_text("Session ID\nabc\n", encoding="utf-8")
            trainable.write_text("{}", encoding="utf-8")
            manifest = write_analysis_finalize_bundle(
                bundle,
                run_id="run-1",
                session_id="session-1",
                completed=False,
                source_fingerprint="a" * 64,
                state_path=state,
                csv_path=csv_path,
                trainable_path=trainable,
            )
            with zipfile.ZipFile(bundle) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"manifest.json", "analysis_state.json", "analysis.csv", "trainable.json"},
                )
                stored = json.loads(archive.read("manifest.json"))
        self.assertEqual(stored, manifest)
        self.assertFalse(stored["completed"])


if __name__ == "__main__":
    unittest.main()
