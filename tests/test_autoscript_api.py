import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from autoscript_api import APICancelled, APIError, AutoScriptAPI, PaginatedList


class ApiHandler(BaseHTTPRequestHandler):
    package = b"server-package-bytes"
    uploaded = b""
    uploaded_filename = ""
    uploaded_block_name = ""
    result = b'{"result":"fixture"}'
    artifact = b"Participant,Word\n7,test\n"
    bulk_archive = b"PK\x03\x04server-built-results-archive"
    bulk_started_event = None
    bulk_release_event = None
    requests = []
    last_authorization = None

    def log_message(self, format, *args):
        pass

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).last_authorization = self.headers.get("Authorization")
        if self.path in {"/api/v1/invalid-json", "/api/v1/invalid-utf8"}:
            body = b"{" if self.path.endswith("invalid-json") else b"\xff"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/v1/experiments" and parsed.query:
            query = parse_qs(parsed.query)
            type(self).requests.append(("GET_PAGE", parsed.path, query))
            self.send_json(
                200,
                [{"id": "experiment-1", "name": "fixture", "versions": []}],
                {"X-Total-Count": "3", "X-Next-Cursor": "next-experiment"},
            )
            return
        if (
            parsed.path == "/api/v1/experiments/experiment-1/runs"
            and parsed.query
        ):
            query = parse_qs(parsed.query)
            type(self).requests.append(("GET_RUN_PAGE", parsed.path, query))
            self.send_json(
                200,
                [{"id": "run-1", "results": [], "artifacts": []}],
                {"X-Total-Count": "2", "X-Next-Cursor": "next-run"},
            )
            return
        if self.path == "/api/v1/experiments":
            self.send_json(200, [{"id": "experiment-1", "name": "fixture", "versions": []}])
            return
        if self.path == "/api/v1/experiments/experiment-1":
            self.send_json(
                200,
                {
                    "id": "experiment-1",
                    "name": "fixture",
                    "current_revision_id": "revision-1",
                    "blocks": [],
                },
            )
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
        if self.path == "/api/v1/runs/run-1":
            self.send_json(200, {
                "id": "run-1", "experiment_id": "experiment-1",
                "session_id": "7_20260807_120000_abcdef",
                "source_experiment_name": "fixture",
            })
            return
        if self.path == "/api/v1/runs/run-1/analysis-state":
            state = {"schema_version": "1.1", "run_id": "run-1", "sources": []}
            self.send_json(200, state, {
                "ETag": '"state-r2"',
                "X-Analysis-Revision": "2",
                "X-Checksum-SHA256": "a" * 64,
                "X-Source-Fingerprint": "b" * 64,
            })
            return
        if self.path == "/api/v1/runs/run-1/analysis-copies":
            self.send_json(200, [{
                "id": "revision-3",
                "run_id": "run-1",
                "revision": 3,
                "created_at": "2026-08-09T12:00:00Z",
                "is_current_editable": True,
            }])
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
        type(self).last_authorization = self.headers.get("Authorization")
        if self.path == "/api/v1/experiments/experiment-1/bulk-export":
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            type(self).requests.append(("BULK_EXPORT", self.path, payload))
            if type(self).bulk_started_event is not None:
                type(self).bulk_started_event.set()
            if type(self).bulk_release_event is not None:
                type(self).bulk_release_event.wait(timeout=5)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(self.bulk_archive)))
            self.send_header(
                "X-Checksum-SHA256",
                hashlib.sha256(self.bulk_archive).hexdigest(),
            )
            self.send_header(
                "Content-Disposition",
                "attachment; filename=fixture-results.zip",
            )
            self.end_headers()
            self.wfile.write(self.bulk_archive)
            return
        if self.path == "/api/v1/auth/login":
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_json(200, {"access_token": "session-token", "user": {"username": "tester"}})
            return
        if self.path == "/api/v1/experiment-revisions/revision-1/runs":
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            type(self).requests.append(("POST", self.path, payload))
            self.send_json(201, {"id": "run-1", "status": "created"})
            return
        if self.path in {"/api/v1/runs/run-1/start", "/api/v1/runs/run-1/finalize"}:
            status_value = "running" if self.path.endswith("/start") else "completed"
            self.send_json(200, {"id": "run-1", "status": status_value})
            return
        if self.path == "/api/v1/runs/run-1/results":
            length = int(self.headers["Content-Length"])
            type(self).uploaded = self.rfile.read(length)
            self.send_json(201, {"id": "result-1"})
            return
        if self.path == "/api/v1/run-results/resolve":
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_json(200, {
                "results": [{
                    "id": "result-1", "run_id": "run-1",
                    "sha256": payload["sha256"][0], "block_index": 1,
                }],
                "missing_sha256": payload["sha256"][1:],
            })
            return
        if self.path == "/api/v1/runs/run-1/analysis/finalize":
            type(self).uploaded = self.rfile.read(int(self.headers["Content-Length"]))
            type(self).requests.append((
                "ANALYSIS_FINALIZE", self.path,
                self.headers.get("If-Match"),
                self.headers.get("X-Idempotency-Key"),
                self.headers.get("X-Existing-Analysis-Policy"),
            ))
            self.send_json(200, {"run_id": "run-1", "completed": True}, {
                "ETag": '"state-r3"', "X-Analysis-Revision": "3",
            })
            return
        if self.path == "/api/v1/runs/run-1/analysis-copies/revision-3/set-editable":
            type(self).requests.append(("SET_EDITABLE", self.path, None))
            self.send_json(200, {"id": "revision-3", "run_id": "run-1"})
            return
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
        if self.path == "/api/v1/staged-blocks":
            length = int(self.headers["Content-Length"])
            type(self).uploaded = self.rfile.read(length)
            type(self).uploaded_filename = self.headers["X-Filename"]
            type(self).uploaded_block_name = self.headers["X-Block-Name"]
            type(self).requests.append(
                ("STAGE", self.path, self.headers["X-Idempotency-Key"])
            )
            self.send_json(
                201,
                {
                    "id": "staged-1",
                    "request_id": self.headers["X-Idempotency-Key"],
                    "name": "Block Î±",
                },
            )
            return
        if self.path in {
            "/api/v1/experiments/publish",
            "/api/v1/experiments/experiment-1/publish",
        }:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            type(self).requests.append(("POST", self.path, payload))
            self.send_json(
                201 if self.path.endswith("/experiments/publish") else 200,
                {
                    "id": "experiment-1",
                    "name": payload["name"],
                    "current_revision_id": "revision-2",
                    "blocks": [],
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
        if self.path == "/api/v1/runs/run-1/analysis-state":
            type(self).uploaded = self.rfile.read(int(self.headers["Content-Length"]))
            type(self).requests.append((
                "ANALYSIS_STATE", self.path,
                self.headers.get("If-Match"),
                self.headers.get("X-Idempotency-Key"),
            ))
            self.send_json(200, {"run_id": "run-1"}, {
                "ETag": '"state-r3"', "X-Analysis-Revision": "3",
            })
            return
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

    def test_invalid_success_json_is_reported_as_stable_api_error(self):
        for path in ("/api/v1/invalid-json", "/api/v1/invalid-utf8"):
            with self.subTest(path=path), self.assertRaisesRegex(
                APIError, "invalid JSON response"
            ):
                self.api._json_request("GET", path)

    def test_paginated_lists_retain_headers_and_encode_server_filters(self):
        experiments = self.api.list_experiments(
            search="alpha beta",
            include_versions=False,
            limit=25,
            cursor="opaque cursor",
        )
        self.assertIsInstance(experiments, PaginatedList)
        self.assertEqual(experiments.total_count, 3)
        self.assertEqual(experiments.next_cursor, "next-experiment")
        experiment_request = next(
            request for request in reversed(ApiHandler.requests)
            if request[0] == "GET_PAGE"
        )
        self.assertEqual(experiment_request[2], {
            "search": ["alpha beta"],
            "include_versions": ["false"],
            "limit": ["25"],
            "cursor": ["opaque cursor"],
        })

        runs = self.api.list_experiment_runs(
            "experiment-1",
            participant_number=7,
            session_search="session abc",
            status="incomplete",
            complete=False,
            has_raw_data=True,
            has_analyzed_csv=False,
            has_trainable_json=True,
            include_files=False,
            limit=50,
            cursor="next page",
        )
        self.assertEqual(runs.total_count, 2)
        self.assertEqual(runs.next_cursor, "next-run")
        run_request = next(
            request for request in reversed(ApiHandler.requests)
            if request[0] == "GET_RUN_PAGE"
        )
        self.assertEqual(run_request[2], {
            "participant_number": ["7"],
            "session_search": ["session abc"],
            "status": ["incomplete"],
            "complete": ["false"],
            "has_raw_data": ["true"],
            "has_analyzed_csv": ["false"],
            "has_trainable_json": ["true"],
            "include_files": ["false"],
            "limit": ["50"],
            "cursor": ["next page"],
        })

    def test_bulk_results_export_is_one_streamed_server_request(self):
        progress = []
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "results.zip"
            downloaded = self.api.download_experiment_bulk_export(
                "experiment-1",
                ["run-1", "run-2"],
                ["raw_data", "analysis_csv"],
                destination,
                analysis_policy="all",
                progress=lambda received, total: progress.append((received, total)),
            )
            self.assertEqual(downloaded.read_bytes(), ApiHandler.bulk_archive)
            self.assertFalse(destination.with_name("results.zip.part").exists())
        request = next(
            request for request in reversed(ApiHandler.requests)
            if request[0] == "BULK_EXPORT"
        )
        self.assertEqual(request[2], {
            "run_ids": ["run-1", "run-2"],
            "include": ["raw_data", "analysis_csv"],
            "analysis_policy": "all",
        })
        self.assertEqual(progress[-1], (
            len(ApiHandler.bulk_archive), len(ApiHandler.bulk_archive)
        ))

    def test_cancelled_bulk_export_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "cancelled.zip"
            destination.write_bytes(b"previous export")

            def cancel(_received, _total):
                raise APICancelled("cancelled")

            with self.assertRaises(APICancelled):
                self.api.download_experiment_bulk_export(
                    "experiment-1",
                    ["run-1"],
                    ["raw_data"],
                    destination,
                    progress=cancel,
                )
            self.assertEqual(destination.read_bytes(), b"previous export")
            self.assertFalse(destination.with_name("cancelled.zip.part").exists())

    def test_cancel_event_during_server_preparation_never_replaces_destination(self):
        started = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        errors = []
        ApiHandler.bulk_started_event = started
        ApiHandler.bulk_release_event = release
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "prepared.zip"
                destination.write_bytes(b"previous export")

                def download():
                    try:
                        self.api.download_experiment_bulk_export(
                            "experiment-1",
                            ["run-1"],
                            ["raw_data"],
                            destination,
                            cancel_event=cancelled,
                        )
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=download)
                worker.start()
                self.assertTrue(started.wait(timeout=2))
                cancelled.set()
                release.set()
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], APICancelled)
                self.assertEqual(destination.read_bytes(), b"previous export")
                self.assertFalse(
                    destination.with_name("prepared.zip.part").exists()
                )
        finally:
            release.set()
            ApiHandler.bulk_started_event = None
            ApiHandler.bulk_release_event = None

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

    def test_atomic_staging_publish_and_get_requests(self):
        self.assertEqual(
            self.api.get_experiment("experiment-1")["current_revision_id"],
            "revision-1",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = Path(temp_dir) / "block.zip"
            package_path.write_bytes(b"atomic-block")
            staged = self.api.stage_block(
                package_path,
                "Block Î±",
                request_id="11111111-1111-1111-1111-111111111111",
            )
        self.assertEqual(staged["id"], "staged-1")
        self.assertEqual(ApiHandler.uploaded, b"atomic-block")

        blocks = [
            {
                "source": "staged",
                "id": "staged-1",
                "same_page_as_previous": False,
            }
        ]
        created = self.api.publish_experiment(
            "Atomic",
            blocks,
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(created["current_revision_id"], "revision-2")
        updated = self.api.publish_experiment(
            "Atomic Renamed",
            blocks,
            "33333333-3333-3333-3333-333333333333",
            experiment_id="experiment-1",
            expected_current_revision_id="revision-1",
        )
        self.assertEqual(updated["name"], "Atomic Renamed")
        self.assertIn(
            (
                "POST",
                "/api/v1/experiments/experiment-1/publish",
                {
                    "request_id": "33333333-3333-3333-3333-333333333333",
                    "name": "Atomic Renamed",
                    "description": None,
                    "blocks": blocks,
                    "expected_current_revision_id": "revision-1",
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

    def test_analysis_state_etag_finalize_and_sha_resolution(self):
        state = self.api.get_run_analysis_state("run-1")
        self.assertEqual(state["revision"], 2)
        self.assertEqual(state["etag"], '"state-r2"')
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "analysis_state.json"
            bundle_path = Path(temp_dir) / "analysis.zip"
            state_path.write_text(json.dumps(state["state"]), encoding="utf-8")
            bundle_path.write_bytes(b"zip-bundle")
            updated = self.api.put_run_analysis_state(
                "run-1", state_path, base_etag=state["etag"], request_id="request-1"
            )
            finalized = self.api.finalize_run_analysis(
                "run-1",
                bundle_path,
                base_etag=updated["etag"],
                request_id="request-2",
                existing_policy="replace",
            )
        self.assertEqual(updated["revision"], 3)
        self.assertEqual(finalized["etag"], '"state-r3"')
        finalize_request = next(
            request for request in ApiHandler.requests
            if request[0] == "ANALYSIS_FINALIZE"
        )
        self.assertEqual(finalize_request[-1], "replace")
        copies = self.api.list_run_analysis_copies("run-1")
        self.assertEqual(copies[0]["id"], "revision-3")
        editable = self.api.set_run_analysis_copy_editable(
            "run-1", "revision-3"
        )
        self.assertEqual(editable["id"], "revision-3")
        self.api.delete_run_analysis_copy("run-1", "revision-3")
        self.assertIn(
            (
                "DELETE",
                "/api/v1/runs/run-1/analysis-copies/revision-3",
                None,
            ),
            ApiHandler.requests,
        )
        resolved = self.api.resolve_run_results_by_sha(["a" * 64, "b" * 64])
        self.assertEqual(resolved["results"][0]["run_id"], "run-1")
        self.assertEqual(resolved["missing_sha256"], ["b" * 64])

    def test_login_bearer_header_and_explicit_run_lifecycle(self):
        api = AutoScriptAPI(self.api.base_url)
        logged_in = api.login("tester", "long-password")
        self.assertEqual(logged_in["user"]["username"], "tester")
        run = api.create_run("revision-1", "session-1", 7, 25, "Other")
        self.assertEqual(run["status"], "created")
        self.assertEqual(ApiHandler.last_authorization, "Bearer session-token")
        self.assertEqual(api.start_run("run-1")["status"], "running")
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            result_path.write_bytes(ApiHandler.result)
            self.assertEqual(api.upload_run_result("run-1", result_path)["id"], "result-1")
        self.assertEqual(api.finalize_run("run-1")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
