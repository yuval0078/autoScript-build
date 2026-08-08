import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from result_upload_queue import drain_upload_queue, enqueue_finalize, enqueue_result


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
