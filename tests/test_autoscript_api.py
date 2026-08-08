import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autoscript_api import AutoScriptAPI


class ApiHandler(BaseHTTPRequestHandler):
    package = b"server-package-bytes"
    uploaded = b""
    uploaded_filename = ""
    uploaded_block_name = ""
    result = b'{"result":"fixture"}'
    artifact = b"Participant,Word\n7,test\n"
    requests = []

    def log_message(self, format, *args):
        pass

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/v1/experiments":
            self.send_json(200, [{"id": "experiment-1", "name": "fixture", "versions": []}])
            return
        if self.path == "/api/v1/experiment-versions/version-1/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(self.package)))
            self.end_headers()
            self.wfile.write(self.package)
            return
        if self.path in {
            "/api/v1/blocks/block-1/download",
            "/api/v1/experiments/experiment-1/download",
        }:
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(self.package)))
            self.send_header("X-Checksum-SHA256", hashlib.sha256(self.package).hexdigest())
            self.end_headers()
            self.wfile.write(self.package)
            return
        if self.path == "/api/v1/experiments/experiment-1/runs":
            self.send_json(
                200,
                [
                    {
                        "id": "run-1",
                        "results": [
                            {
                                "id": "result-1",
                                "download_url": "/api/v1/run-results/result-1/download",
                                "sha256": hashlib.sha256(self.result).hexdigest(),
                            }
                        ],
                    }
                ],
            )
            return
        if self.path == "/api/v1/run-results/result-1/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(self.result)))
            self.send_header(
                "X-Checksum-SHA256", hashlib.sha256(self.result).hexdigest()
            )
            self.end_headers()
            self.wfile.write(self.result)
            return
        if self.path == "/api/v1/run-artifacts/artifact-1/download":
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(self.artifact)))
            self.send_header(
                "X-Checksum-SHA256", hashlib.sha256(self.artifact).hexdigest()
            )
            self.end_headers()
            self.wfile.write(self.artifact)
            return
        self.send_json(404, {"detail": "Not found"})

    def do_POST(self):
        if self.path == "/api/v1/experiments":
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_json(201, {"id": "created-1", "name": payload["name"], "versions": []})
            return
        if self.path == "/api/v1/experiments/experiment-1/versions":
            length = int(self.headers["Content-Length"])
            type(self).uploaded = self.rfile.read(length)
            type(self).uploaded_filename = self.headers["X-Filename"]
            self.send_json(
                201,
                {
                    "id": "version-1",
                    "version_number": 1,
                    "sha256": hashlib.sha256(self.uploaded).hexdigest(),
                },
            )
            return
        if self.path == "/api/v1/experiments/experiment-1/blocks":
            length = int(self.headers["Content-Length"])
            type(self).uploaded = self.rfile.read(length)
            type(self).uploaded_filename = self.headers["X-Filename"]
            type(self).uploaded_block_name = self.headers["X-Block-Name"]
            self.send_json(
                201,
                {
                    "id": "block-1",
                    "name": "Block α",
                    "position": int(self.headers["X-Position"]),
                    "sha256": hashlib.sha256(self.uploaded).hexdigest(),
                },
            )
            return
        if self.path == "/api/v1/experiments/experiment-1/duplicate":
            self.send_json(201, {"id": "copy-1", "name": "fixture copy", "blocks": []})
            return
        if self.path == "/api/v1/experiments/experiment-1/results":
            length = int(self.headers["Content-Length"])
            type(self).uploaded = self.rfile.read(length)
            type(self).uploaded_filename = self.headers["X-Filename"]
            self.send_json(
                201,
                {
                    "id": "result-1",
                    "sha256": hashlib.sha256(self.uploaded).hexdigest(),
                    "download_url": "/api/v1/run-results/result-1/download",
                },
            )
            return
        if self.path == "/api/v1/runs/run-1/artifacts/analysis_csv":
            length = int(self.headers["Content-Length"])
            type(self).uploaded = self.rfile.read(length)
            type(self).uploaded_filename = self.headers["X-Filename"]
            self.send_json(
                201,
                {
                    "id": "artifact-1",
                    "kind": "analysis_csv",
                    "sha256": hashlib.sha256(self.uploaded).hexdigest(),
                    "download_url": "/api/v1/run-artifacts/artifact-1/download",
                },
            )
            return
        self.send_json(404, {"detail": "Not found"})

    def do_PATCH(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(("PATCH", self.path, payload))
        if self.path == "/api/v1/runs/run-1/analysis":
            self.send_json(
                200,
                {"id": "run-1", "analysis_completed": payload["completed"]},
            )
            return
        self.send_json(200, {"id": "experiment-1", "name": payload["name"], "blocks": []})

    def do_PUT(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(("PUT", self.path, payload))
        self.send_json(200, {"id": "experiment-1", "name": "fixture", "blocks": []})

    def do_DELETE(self):
        type(self).requests.append(("DELETE", self.path, None))
        self.send_response(204)
        self.end_headers()


class AutoScriptApiClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.server.server_address
        cls.api = AutoScriptAPI(f"http://{host}:{port}")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_json_and_streaming_upload_requests(self):
        self.assertEqual(self.api.list_experiments()[0]["name"], "fixture")
        self.assertEqual(self.api.create_experiment("new")["id"], "created-1")

        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = Path(temp_dir) / "ניסוי.zip"
            package_path.write_bytes(b"uploaded-package")
            progress = []
            version = self.api.publish_package(
                "experiment-1",
                package_path,
                progress=lambda sent, total: progress.append((sent, total)),
            )

        self.assertEqual(ApiHandler.uploaded, b"uploaded-package")
        self.assertTrue(ApiHandler.uploaded_filename.isascii())
        self.assertEqual(version["version_number"], 1)
        self.assertEqual(progress[-1], (len(ApiHandler.uploaded), len(ApiHandler.uploaded)))

    def test_download_verifies_checksum_and_replaces_destination(self):
        version = {
            "download_url": "/api/v1/experiment-versions/version-1/download",
            "sha256": hashlib.sha256(ApiHandler.package).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "download.zip"
            result = self.api.download_version(version, destination)
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), ApiHandler.package)
            self.assertFalse(destination.with_name("download.zip.part").exists())

    def test_experiment_and_block_crud_requests(self):
        updated = self.api.update_experiment("experiment-1", "Renamed")
        self.assertEqual(updated["name"], "Renamed")
        duplicate = self.api.duplicate_experiment("experiment-1")
        self.assertEqual(duplicate["name"], "fixture copy")

        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = Path(temp_dir) / "block.zip"
            package_path.write_bytes(b"block-body")
            uploaded = self.api.upload_block(
                "experiment-1",
                package_path,
                "Block α",
                position=2,
            )
            self.assertEqual(uploaded["id"], "block-1")
            self.assertEqual(uploaded["position"], 2)
            self.assertTrue(ApiHandler.uploaded_block_name.isascii())

            block_destination = Path(temp_dir) / "block-download.zip"
            experiment_destination = Path(temp_dir) / "experiment-download.zip"
            self.api.download_block({"id": "block-1"}, block_destination)
            self.api.download_experiment(
                {"id": "experiment-1"},
                experiment_destination,
            )
            self.assertEqual(block_destination.read_bytes(), ApiHandler.package)
            self.assertEqual(experiment_destination.read_bytes(), ApiHandler.package)

        self.api.reorder_blocks(
            "experiment-1",
            ["block-2", "block-1"],
            same_page_block_ids=["block-1"],
        )
        self.api.delete_block("block-1")
        self.api.delete_experiment("experiment-1")
        self.assertIn(
            (
                "PUT",
                "/api/v1/experiments/experiment-1/blocks/order",
                {
                    "block_ids": ["block-2", "block-1"],
                    "same_page_block_ids": ["block-1"],
                },
            ),
            ApiHandler.requests,
        )

    def test_result_upload_list_and_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "participant result.json"
            result_path.write_bytes(ApiHandler.result)
            uploaded = self.api.upload_result("experiment-1", result_path)
            self.assertEqual(uploaded["id"], "result-1")
            self.assertEqual(ApiHandler.uploaded, ApiHandler.result)
            self.assertTrue(ApiHandler.uploaded_filename.isascii())

            runs = self.api.list_experiment_runs("experiment-1")
            self.assertEqual(runs[0]["id"], "run-1")
            destination = Path(temp_dir) / "downloaded-result.json"
            self.api.download_run_result(runs[0]["results"][0], destination)
            self.assertEqual(destination.read_bytes(), ApiHandler.result)

    def test_analysis_status_and_artifact_requests(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact_path = Path(temp_dir) / "analysis.csv"
            artifact_path.write_bytes(ApiHandler.artifact)
            artifact = self.api.upload_run_artifact(
                "run-1", "analysis_csv", artifact_path
            )
            self.assertEqual(artifact["id"], "artifact-1")
            self.assertEqual(ApiHandler.uploaded, ApiHandler.artifact)

            destination = Path(temp_dir) / "download.csv"
            self.api.download_run_artifact(artifact, destination)
            self.assertEqual(destination.read_bytes(), ApiHandler.artifact)

        updated = self.api.update_run_analysis("run-1", True)
        self.assertTrue(updated["analysis_completed"])
        self.api.delete_run("run-1")
        self.assertIn(("DELETE", "/api/v1/runs/run-1", None), ApiHandler.requests)


if __name__ == "__main__":
    unittest.main()
