import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from result_upload_queue import (
    drain_upload_queue,
    enqueue_finalize,
    enqueue_result,
    enqueue_transition,
)


class ResultUploadQueueTests(unittest.TestCase):
    def test_failed_upload_is_retained_and_retried_before_finalize(self):
        calls = []

        class API:
            fail = True

            def upload_run_result(self, run_id, path):
                calls.append(("result", run_id, Path(path).read_text(encoding="utf-8")))
                if self.fail:
                    raise OSError("offline")

            def finalize_run(self, run_id):
                calls.append(("finalize", run_id))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = root / "saved.json"
            result.write_text('{"ok":true}', encoding="utf-8")
            with patch("result_upload_queue.queue_root", return_value=root):
                enqueue_result(result, "experiment-1", run_id="run-1")
                enqueue_finalize("run-1")
                api = API()
                uploaded, errors = drain_upload_queue(api)
                self.assertEqual(uploaded, 0)
                self.assertTrue(errors)
                self.assertGreater(len(list(root.glob("*.queue.json"))), 0)
                api.fail = False
                uploaded, errors = drain_upload_queue(api)
                self.assertEqual((uploaded, errors), (1, []))
                self.assertEqual(calls[-2][0], "result")
                self.assertEqual(calls[-1], ("finalize", "run-1"))
                self.assertEqual(list(root.glob("*.queue.json")), [])

    def test_invalid_items_are_quarantined_and_transitions_continue(self):
        calls = []

        class API:
            def cancel_run(self, run_id):
                calls.append(("cancel", run_id))

            def fail_run(self, run_id):
                calls.append(("fail", run_id))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "broken.queue.json").write_text("not json", encoding="utf-8")
            with patch("result_upload_queue.queue_root", return_value=root):
                enqueue_transition("run-1", "cancel")
                enqueue_transition("run-2", "fail")
                uploaded, errors = drain_upload_queue(API())

            self.assertEqual(uploaded, 0)
            self.assertTrue(errors)
            self.assertEqual(calls, [("cancel", "run-1"), ("fail", "run-2")])
            self.assertEqual(list(root.glob("*.queue.json")), [])
            self.assertEqual(len(list((root / "quarantine").glob("*.queue.json"))), 1)

    def test_concurrent_drainers_do_not_upload_twice(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class API:
            def upload_run_result(self, run_id, path):
                calls.append((run_id, Path(path).read_text(encoding="utf-8")))
                entered.set()
                release.wait(timeout=5)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            result = root / "saved.json"
            result.write_text('{"ok":true}', encoding="utf-8")
            with patch("result_upload_queue.queue_root", return_value=root):
                enqueue_result(result, "experiment-1", run_id="run-1")
                first_result = []
                first = threading.Thread(
                    target=lambda: first_result.append(drain_upload_queue(API()))
                )
                first.start()
                self.assertTrue(entered.wait(timeout=5))
                second = drain_upload_queue(API())
                release.set()
                first.join(timeout=5)

            self.assertEqual(len(calls), 1)
            self.assertEqual(first_result, [(1, [])])
            self.assertEqual(second[0], 0)
            self.assertTrue(second[1])
