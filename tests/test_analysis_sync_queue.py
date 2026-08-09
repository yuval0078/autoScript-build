import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autoscript_api import APIError
from analysis_sync_queue import (
    drain_analysis_queue,
    enqueue_analysis_finalize,
    enqueue_analysis_state,
)


class AnalysisSyncQueueTests(unittest.TestCase):
    def test_transient_failure_retains_payload_then_retries(self):
        calls = []

        class API:
            fail = True

            def put_run_analysis_state(self, run_id, path, **kwargs):
                calls.append((run_id, Path(path).read_text(), kwargs))
                if self.fail:
                    raise APIError("offline")
                return {"etag": '"r1"', "revision": 1}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "queue"
            state = Path(temp_dir) / "state.json"
            state.write_text('{"schema_version":"1.1"}', encoding="utf-8")
            with patch("analysis_sync_queue.queue_root", return_value=root):
                root.mkdir()
                enqueue_analysis_state("run-1", state)
                api = API()
                count, errors, _ = drain_analysis_queue(api)
                self.assertEqual(count, 0)
                self.assertTrue(errors)
                self.assertEqual(len(list(root.glob("*.queue.json"))), 1)
                api.fail = False
                count, errors, outcomes = drain_analysis_queue(api)
            self.assertEqual((count, errors), (1, []))
            self.assertEqual(outcomes["run-1"]["revision"], 1)
            self.assertEqual(list(root.glob("*.queue.json")), [])

    def test_latest_state_is_coalesced_and_finalize_supersedes_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "queue"
            root.mkdir()
            first = Path(temp_dir) / "first.json"
            second = Path(temp_dir) / "second.json"
            final = Path(temp_dir) / "final.zip"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            final.write_bytes(b"zip")
            with patch("analysis_sync_queue.queue_root", return_value=root):
                enqueue_analysis_state("run-1", first, base_etag='"r0"')
                enqueue_analysis_state("run-1", second, base_etag='"r0"')
                items = list(root.glob("*.queue.json"))
                self.assertEqual(len(items), 1)
                item = json.loads(items[0].read_text(encoding="utf-8"))
                self.assertEqual((root / item["payload"]).read_text(), "second")
                enqueue_analysis_finalize(
                    "run-1",
                    final,
                    base_etag='"r0"',
                    existing_policy="replace",
                )
                item_paths = list(root.glob("*.queue.json"))
                self.assertEqual(len(item_paths), 1)
                item = json.loads(item_paths[0].read_text(encoding="utf-8"))
                self.assertEqual(item["kind"], "finalize")
                self.assertEqual(item["existing_policy"], "replace")

    def test_precondition_conflict_preserves_payload(self):
        class API:
            def finalize_run_analysis(self, *args, **kwargs):
                raise APIError("stale", status_code=412)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "queue"
            root.mkdir()
            bundle = Path(temp_dir) / "final.zip"
            bundle.write_bytes(b"important edits")
            with patch("analysis_sync_queue.queue_root", return_value=root):
                enqueue_analysis_finalize("run-1", bundle, base_etag='"old"')
                count, errors, _ = drain_analysis_queue(API())
            self.assertEqual(count, 0)
            self.assertTrue(errors)
            conflicts = root / "conflicts"
            self.assertEqual(len(list(conflicts.glob("*.queue.json"))), 1)
            payloads = [path for path in conflicts.iterdir() if ".payload." in path.name]
            self.assertEqual(payloads[0].read_bytes(), b"important edits")

    def test_lost_response_retry_is_kept_and_advances_following_etag(self):
        calls = []

        class API:
            lost = True

            def put_run_analysis_state(self, run_id, path, **kwargs):
                body = Path(path).read_text(encoding="utf-8")
                calls.append((body, kwargs["base_etag"], kwargs["request_id"]))
                if self.lost:
                    self.lost = False
                    raise APIError("response lost")
                revision = 1 if body == "first" else 2
                return {"etag": f'"r{revision}"', "revision": revision}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "queue"
            root.mkdir()
            first = Path(temp_dir) / "first.json"
            second = Path(temp_dir) / "second.json"
            first.write_text("first", encoding="utf-8")
            second.write_text("second", encoding="utf-8")
            api = API()
            with patch("analysis_sync_queue.queue_root", return_value=root):
                enqueue_analysis_state("run-1", first, base_etag='"r0"', request_id="one")
                drain_analysis_queue(api)
                enqueue_analysis_state("run-1", second, base_etag='"r0"', request_id="two")
                self.assertEqual(len(list(root.glob("*.queue.json"))), 2)
                count, errors, outcomes = drain_analysis_queue(api)
            self.assertEqual((count, errors), (2, []))
            self.assertEqual(calls[1], ("first", '"r0"', "one"))
            self.assertEqual(calls[2], ("second", '"r1"', "two"))
            self.assertEqual(outcomes["run-1"]["revision"], 2)

    def test_finalize_waits_for_attempted_state_and_uses_returned_etag(self):
        calls = []

        class API:
            fail_once = True

            def put_run_analysis_state(self, run_id, path, **kwargs):
                calls.append(("state", kwargs["base_etag"]))
                if self.fail_once:
                    self.fail_once = False
                    raise APIError("lost")
                return {"etag": '"r1"', "revision": 1}

            def finalize_run_analysis(self, run_id, path, **kwargs):
                calls.append((
                    "finalize",
                    kwargs["base_etag"],
                    kwargs["existing_policy"],
                ))
                return {"etag": '"r2"', "revision": 2}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "queue"
            root.mkdir()
            state = Path(temp_dir) / "state.json"
            bundle = Path(temp_dir) / "final.zip"
            state.write_text("state", encoding="utf-8")
            bundle.write_bytes(b"bundle")
            api = API()
            with patch("analysis_sync_queue.queue_root", return_value=root):
                enqueue_analysis_state("run-1", state, base_etag=None, request_id="state")
                drain_analysis_queue(api)
                enqueue_analysis_finalize(
                    "run-1",
                    bundle,
                    base_etag=None,
                    request_id="final",
                    existing_policy="replace",
                )
                self.assertEqual(len(list(root.glob("*.queue.json"))), 2)
                count, errors, _ = drain_analysis_queue(api)
            self.assertEqual((count, errors), (2, []))
            self.assertEqual(calls[-1], ("finalize", '"r1"', "replace"))

    def test_new_finalize_follows_exact_retry_of_attempted_finalize(self):
        calls = []

        class API:
            lose_once = True

            def finalize_run_analysis(self, run_id, path, **kwargs):
                body = Path(path).read_bytes()
                calls.append((body, kwargs["base_etag"], kwargs["request_id"]))
                if self.lose_once:
                    self.lose_once = False
                    raise APIError("response lost")
                revision = 1 if body == b"old-final" else 2
                return {"etag": f'"r{revision}"', "revision": revision}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "queue"
            root.mkdir()
            old = Path(temp_dir) / "old.zip"
            new = Path(temp_dir) / "new.zip"
            old.write_bytes(b"old-final")
            new.write_bytes(b"new-final")
            api = API()
            with patch("analysis_sync_queue.queue_root", return_value=root):
                enqueue_analysis_finalize("run-1", old, request_id="old")
                drain_analysis_queue(api)
                enqueue_analysis_finalize("run-1", new, request_id="new")
                self.assertEqual(len(list(root.glob("*.queue.json"))), 2)
                count, errors, _ = drain_analysis_queue(api)
            self.assertEqual((count, errors), (2, []))
            self.assertEqual(calls[1], (b"old-final", None, "old"))
            self.assertEqual(calls[2], (b"new-final", '"r1"', "new"))


if __name__ == "__main__":
    unittest.main()
