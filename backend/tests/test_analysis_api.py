import csv
import hashlib
import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import (
    Base,
    Experiment,
    ExperimentBlock,
    ExperimentRun,
    RunAnalysisRevision,
    RunArtifact,
    RunResult,
    ObjectDeletionTask,
    User,
)
from app.services.storage import get_object_storage

try:
    from .test_result_api import FakeStorage, raw_result, word_record
except ImportError:  # unittest discovery imports test modules without a package.
    from test_result_api import FakeStorage, raw_result, word_record


class FailingStorage(FakeStorage):
    fail_after = None
    fail_removes = False

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        if self.fail_after is not None:
            if self.fail_after == 0:
                self.fail_after = None
                raise RuntimeError("simulated object-store failure")
            self.fail_after -= 1
        return super().put_file(object_name, file_path, content_type)

    def remove_object(self, object_name):
        if self.fail_removes:
            raise RuntimeError("simulated deletion failure")
        return super().remove_object(object_name)


class AnalysisApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        with self.sessions() as database:
            database.add(
                User(
                    username="local-admin",
                    password_hash="!test!",
                    role="admin",
                    is_active=True,
                )
            )
            database.commit()
        self.storage = FailingStorage()
        self.app = create_app()

        def override_database():
            with self.sessions() as database:
                yield database

        self.app.dependency_overrides[get_db] = override_database
        self.app.dependency_overrides[get_object_storage] = lambda: self.storage
        self.client = TestClient(self.app)
        created = self.client.post(
            "/api/v1/experiments", json={"name": "Analysis Study"}
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.experiment = created.json()
        payload = raw_result(self.experiment["id"], block_index=1, block_count=1)
        payload["block_count"] = 1
        payload["session_experiment_count"] = 1
        payload["words"] = [word_record()]
        payload["completed_word_count"] = 1
        payload["expected_word_count"] = 1
        uploaded = self.client.post(
            f"/api/v1/experiments/{self.experiment['id']}/results",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.result = uploaded.json()
        self.run = self.client.get(f"/api/v1/runs/{self.result['run_id']}").json()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def state(self, written_word="test"):
        identity = [
            {
                "id": self.result["id"],
                "sha256": self.result["sha256"],
                "block_index": 1,
            }
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "schema_version": "1.1",
            "analyzer_version": "0.0",
            "run_id": self.run["id"],
            "experiment_id": self.run["experiment_id"],
            "session_id": self.run["session_id"],
            "source_fingerprint": fingerprint,
            "sources": [
                {
                    "result_id": self.result["id"],
                    "raw_sha256": self.result["sha256"],
                    "session_id": self.run["session_id"],
                    "block_id": self.result["block_id"],
                    "block_index": 1,
                    "block_name": self.result["block_name"],
                    "word_count": 1,
                    "words": [
                        {
                            "letters": [],
                            "assigned_letters": {},
                            "stroke_slices": [],
                            "trainability": "trainable",
                            "written_word": written_word,
                            "correct": written_word == "test",
                        }
                    ],
                }
            ],
        }

    def put_state(self, state, *, etag=None, request_id=None):
        headers = {
            "Content-Type": "application/json",
            "X-Idempotency-Key": str(request_id or uuid.uuid4()),
        }
        if etag is None:
            headers["If-None-Match"] = "*"
        else:
            headers["If-Match"] = etag
        return self.client.put(
            f"/api/v1/runs/{self.run['id']}/analysis-state",
            content=json.dumps(state, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )

    def bundle_bytes(
        self,
        state,
        *,
        completed=True,
        csv_header_only=False,
        omit_csv_experiment_id=False,
        omit_csv_block_id=False,
        omit_trainable_experiment_id=False,
        omit_trainable_block_id=False,
    ):
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        header = [
            "Exp Step", "Experiment", "Experiment ID", "Block", "Block ID",
            "Block Index", "Block Count", "Session ID", "Participant", "Age",
            "Gender", "Word", "Group", "Cell", "Correct", "Written Word",
            "Reading End", "Writing Start", "Writing End", "Strokes",
            "Avg Interval", "Screenshot File",
        ]
        writer.writerow(header)
        if not csv_header_only:
            writer.writerow([
                1, self.run["source_experiment_name"],
                "" if omit_csv_experiment_id else self.run["experiment_id"],
                self.result["block_name"],
                "" if omit_csv_block_id else (self.result["block_id"] or ""), 1, 1,
                self.run["session_id"], self.run["participant_number"],
                self.run["participant_age"], self.run["participant_gender"], "test",
                "group-a", 0, "True", "test", "0.0", "0.0", "0.0", 0,
                "0.0", "",
            ])
        files = {
            "analysis_state.json": json.dumps(state, ensure_ascii=False).encode("utf-8"),
            "analysis.csv": output.getvalue().encode("utf-8-sig"),
            "trainable.json": json.dumps({
                "run_id": self.run["id"],
                "source_result_id": self.result["id"],
                "source_sha256": self.result["sha256"],
                "analyzer_version": "0.0",
                "experiment_name": self.run["source_experiment_name"],
                "experiment_id": (
                    None if omit_trainable_experiment_id
                    else self.run["experiment_id"]
                ),
                "block_name": self.result["block_name"],
                "block_id": None if omit_trainable_block_id else self.result["block_id"],
                "block_index": 1,
                "block_count": 1,
                "session_id": self.run["session_id"],
                "participant_number": self.run["participant_number"],
                "participant_age": self.run["participant_age"],
                "participant_gender": self.run["participant_gender"],
                "timestamp": self.result["result_timestamp"],
                "words": [{}],
            }).encode("utf-8"),
        }
        manifest = {
            "schema_version": "1.0",
            "analyzer_version": "0.0",
            "run_id": self.run["id"],
            "session_id": self.run["session_id"],
            "completed": completed,
            "source_fingerprint": state["source_fingerprint"],
            "files": {
                name: {"sha256": hashlib.sha256(content).hexdigest()}
                for name, content in files.items()
            },
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, content in files.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def test_state_cas_idempotency_conditional_get_and_resolve(self):
        with self.sessions() as database:
            run = database.get(ExperimentRun, uuid.UUID(self.run["id"]))
            run.analysis_completed = True
            database.commit()
        request_id = uuid.uuid4()
        first = self.put_state(self.state(), request_id=request_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["revision"], 1)
        etag = first.headers["etag"]
        updated_run = self.client.get(f"/api/v1/runs/{self.run['id']}").json()
        self.assertFalse(updated_run["analysis_completed"])
        self.assertIsNotNone(updated_run["analysis_updated_at"])

        retry = self.put_state(self.state(), request_id=request_id)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        conflict = self.put_state(self.state("changed"), etag='"stale"')
        self.assertEqual(conflict.status_code, 412, conflict.text)

        fetched = self.client.get(f"/api/v1/runs/{self.run['id']}/analysis-state")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json(), self.state())
        self.assertIn("last-modified", fetched.headers)
        unchanged = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-state",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(unchanged.status_code, 304, unchanged.text)
        for header in (
            "etag",
            "last-modified",
            "cache-control",
            "x-analysis-revision",
            "x-checksum-sha256",
            "x-source-fingerprint",
        ):
            self.assertIn(header, unchanged.headers)
        self.assertEqual(unchanged.headers["cache-control"], "no-store")

        missing = "f" * 64
        resolved = self.client.post(
            "/api/v1/run-results/resolve",
            json={"sha256": [self.result["sha256"], missing]},
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["results"][0]["id"], self.result["id"])
        self.assertEqual(resolved.json()["missing_sha256"], [missing])

    def test_finalize_is_atomic_permanent_and_strictly_validated(self):
        saved = self.put_state(self.state())
        self.assertEqual(saved.status_code, 200, saved.text)
        request_id = uuid.uuid4()
        bundle = self.bundle_bytes(self.state())
        finalized = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=bundle,
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(request_id),
                "If-Match": saved.headers["etag"],
            },
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertTrue(finalized.json()["finalized"])
        self.assertEqual(
            {item["kind"] for item in finalized.json()["artifacts"]},
            {"analysis_state", "analysis_csv", "trainable_json"},
        )
        retry = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=bundle,
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(request_id),
                "If-Match": '"stale"',
            },
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], finalized.json()["id"])
        run = self.client.get(f"/api/v1/runs/{self.run['id']}").json()
        self.assertTrue(run["analysis_completed"])

        invalid = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=self.bundle_bytes(self.state(), csv_header_only=True),
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": finalized.headers["etag"],
            },
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)

        self.storage.fail_after = 1
        failed = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=self.bundle_bytes(self.state(), completed=False),
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": finalized.headers["etag"],
            },
        )
        self.assertEqual(failed.status_code, 500, failed.text)
        still_current = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-state"
        )
        self.assertEqual(still_current.headers["etag"], finalized.headers["etag"])

    def test_finalized_copies_can_be_listed_downloaded_restored_and_deleted(self):
        draft = self.put_state(self.state())
        self.assertEqual(draft.status_code, 200, draft.text)
        first = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=self.bundle_bytes(self.state(), completed=True),
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": draft.headers["etag"],
            },
        )
        self.assertEqual(first.status_code, 200, first.text)
        second = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=self.bundle_bytes(self.state(), completed=False),
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": first.headers["etag"],
            },
        )
        self.assertEqual(second.status_code, 200, second.text)

        run = self.client.get(f"/api/v1/runs/{self.run['id']}").json()
        self.assertEqual(run["raw_data_count"], 1)
        self.assertEqual(run["analyzed_csv_count"], 2)
        self.assertEqual(run["trainable_json_count"], 2)

        copies = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-copies"
        )
        self.assertEqual(copies.status_code, 200, copies.text)
        self.assertEqual(
            [copy["id"] for copy in copies.json()],
            [second.json()["id"], first.json()["id"]],
        )
        self.assertTrue(copies.json()[0]["is_current_editable"])
        self.assertFalse(copies.json()[1]["is_current_editable"])
        self.assertFalse(copies.json()[0]["completed"])
        self.assertTrue(copies.json()[1]["completed"])
        csv_download = self.client.get(
            copies.json()[0]["analyzed_csv"]["download_url"]
        )
        self.assertEqual(csv_download.status_code, 200, csv_download.text)
        self.assertIn(b"Participant", csv_download.content)

        restored = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis-copies/"
            f"{first.json()['id']}/set-editable"
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["id"], first.json()["id"])
        self.assertEqual(restored.headers["etag"], first.headers["etag"])
        restored_state = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-state"
        )
        self.assertEqual(restored_state.headers["etag"], first.headers["etag"])
        self.assertTrue(
            self.client.get(f"/api/v1/runs/{self.run['id']}").json()[
                "analysis_completed"
            ]
        )

        deleted_newer = self.client.delete(
            f"/api/v1/runs/{self.run['id']}/analysis-copies/{second.json()['id']}"
        )
        self.assertEqual(deleted_newer.status_code, 204, deleted_newer.text)
        remaining = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-copies"
        ).json()
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0]["is_current_editable"])

        deleted_current = self.client.delete(
            f"/api/v1/runs/{self.run['id']}/analysis-copies/{first.json()['id']}"
        )
        self.assertEqual(deleted_current.status_code, 204, deleted_current.text)
        self.assertEqual(
            self.client.get(
                f"/api/v1/runs/{self.run['id']}/analysis-copies"
            ).json(),
            [],
        )
        after_delete = self.client.get(f"/api/v1/runs/{self.run['id']}").json()
        self.assertEqual(after_delete["analyzed_csv_count"], 0)
        self.assertEqual(after_delete["trainable_json_count"], 0)
        self.assertFalse(after_delete["analysis_completed"])
        # The earlier draft is retained and becomes the current editable state.
        fallback_state = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-state"
        )
        self.assertEqual(fallback_state.status_code, 200, fallback_state.text)
        self.assertEqual(fallback_state.headers["x-analysis-revision"], "1")

        missing = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis-copies/"
            f"{uuid.uuid4()}/set-editable"
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_finalize_replace_policy_atomically_replaces_older_exports(self):
        draft = self.put_state(self.state())
        self.assertEqual(draft.status_code, 200, draft.text)
        first = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=self.bundle_bytes(self.state(), completed=True),
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": draft.headers["etag"],
            },
        )
        self.assertEqual(first.status_code, 200, first.text)
        first_artifact_urls = [
            artifact["download_url"] for artifact in first.json()["artifacts"]
        ]

        request_id = uuid.uuid4()
        replacement_bundle = self.bundle_bytes(self.state(), completed=False)
        replacement_headers = {
            "Content-Type": "application/zip",
            "X-Idempotency-Key": str(request_id),
            "If-Match": first.headers["etag"],
            "X-Existing-Analysis-Policy": "replace",
        }
        replaced = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=replacement_bundle,
            headers=replacement_headers,
        )
        self.assertEqual(replaced.status_code, 200, replaced.text)
        self.assertEqual(
            replaced.headers["x-existing-analysis-policy"], "replace"
        )
        copies = self.client.get(
            f"/api/v1/runs/{self.run['id']}/analysis-copies"
        ).json()
        self.assertEqual([copy["id"] for copy in copies], [replaced.json()["id"]])
        run = self.client.get(f"/api/v1/runs/{self.run['id']}").json()
        self.assertEqual(run["analyzed_csv_count"], 1)
        self.assertEqual(run["trainable_json_count"], 1)
        for download_url in first_artifact_urls:
            self.assertEqual(self.client.get(download_url).status_code, 404)

        retry = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=replacement_bundle,
            headers={
                **replacement_headers,
                "If-Match": '"stale"',
            },
        )
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], replaced.json()["id"])
        self.assertEqual(
            len(
                self.client.get(
                    f"/api/v1/runs/{self.run['id']}/analysis-copies"
                ).json()
            ),
            1,
        )

        changed_policy = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=replacement_bundle,
            headers={
                **replacement_headers,
                "If-Match": replaced.headers["etag"],
                "X-Existing-Analysis-Policy": "keep",
            },
        )
        self.assertEqual(changed_policy.status_code, 409, changed_policy.text)

        invalid_policy = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=replacement_bundle,
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": replaced.headers["etag"],
                "X-Existing-Analysis-Policy": "merge",
            },
        )
        self.assertEqual(invalid_policy.status_code, 400, invalid_policy.text)

    def test_draft_retention_never_prunes_finalizations(self):
        saved = self.put_state(self.state())
        bundle = self.bundle_bytes(self.state())
        finalized = self.client.post(
            f"/api/v1/runs/{self.run['id']}/analysis/finalize",
            content=bundle,
            headers={
                "Content-Type": "application/zip",
                "X-Idempotency-Key": str(uuid.uuid4()),
                "If-Match": saved.headers["etag"],
            },
        )
        self.assertEqual(finalized.status_code, 200, finalized.text)
        etag = finalized.headers["etag"]
        from app.config import get_settings

        settings = get_settings()
        previous = settings.analysis_draft_retention
        settings.analysis_draft_retention = 2
        try:
            for number in range(4):
                response = self.put_state(self.state(f"draft-{number}"), etag=etag)
                self.assertEqual(response.status_code, 200, response.text)
                etag = response.headers["etag"]
        finally:
            settings.analysis_draft_retention = previous
        with self.sessions() as database:
            revisions = database.scalars(
                select(RunAnalysisRevision).where(
                    RunAnalysisRevision.run_id == uuid.UUID(self.run["id"])
                )
            ).all()
        self.assertEqual(sum(not item.finalized for item in revisions), 2)
        self.assertEqual(sum(item.finalized for item in revisions), 1)

    def test_authoritative_ids_and_csv_word_coverage_are_required(self):
        authoritative_block_id = uuid.uuid4()
        with self.sessions() as database:
            result = database.get(RunResult, uuid.UUID(self.result["id"]))
            run = database.get(ExperimentRun, result.run_id)
            database.add(
                ExperimentBlock(
                    id=authoritative_block_id,
                    experiment_id=run.experiment_id,
                    position=0,
                    name=result.block_name,
                    storage_key=f"test/blocks/{authoritative_block_id}.zip",
                    original_filename="block.zip",
                    sha256="a" * 64,
                    size_bytes=1,
                    created_by=run.created_by,
                )
            )
            result.block_id = authoritative_block_id
            database.commit()
        self.result["block_id"] = str(authoritative_block_id)

        missing_block_state = self.state()
        missing_block_state["sources"][0]["block_id"] = None
        rejected_state = self.put_state(missing_block_state)
        self.assertEqual(rejected_state.status_code, 422, rejected_state.text)
        missing_experiment_state = self.state()
        missing_experiment_state["experiment_id"] = None
        rejected_state = self.put_state(missing_experiment_state)
        self.assertEqual(rejected_state.status_code, 422, rejected_state.text)

        saved = self.put_state(self.state())
        self.assertEqual(saved.status_code, 200, saved.text)
        cases = (
            {"omit_csv_experiment_id": True},
            {"omit_csv_block_id": True},
            {"omit_trainable_experiment_id": True},
            {"omit_trainable_block_id": True},
            {"csv_header_only": True},
        )
        for options in cases:
            with self.subTest(options=options):
                response = self.client.post(
                    f"/api/v1/runs/{self.run['id']}/analysis/finalize",
                    content=self.bundle_bytes(self.state(), **options),
                    headers={
                        "Content-Type": "application/zip",
                        "X-Idempotency-Key": str(uuid.uuid4()),
                        "If-Match": saved.headers["etag"],
                    },
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_legacy_state_cas_and_shared_lab_resolution(self):
        legacy_bytes = json.dumps(
            {"schema_version": "1.0", "sources": []}
        ).encode("utf-8")
        legacy_sha = hashlib.sha256(legacy_bytes).hexdigest()
        legacy_key = f"legacy/{self.run['id']}/{legacy_sha}.json"
        self.storage.objects[legacy_key] = legacy_bytes
        hidden_sha = "e" * 64
        with self.sessions() as database:
            owner = database.scalar(select(User).where(User.username == "local-admin"))
            database.add(
                RunArtifact(
                    id=uuid.uuid4(),
                    run_id=uuid.UUID(self.run["id"]),
                    kind="analysis_state",
                    storage_key=legacy_key,
                    original_filename="legacy.json",
                    sha256=legacy_sha,
                    size_bytes=len(legacy_bytes),
                    created_by=owner.id,
                )
            )
            other = User(
                username="other-owner",
                password_hash="!test!",
                role="researcher",
                is_active=True,
            )
            experiment = Experiment(name="Other private study", owner=other)
            database.add_all([other, experiment])
            database.flush()
            hidden_run = ExperimentRun(
                id=uuid.uuid4(),
                experiment=experiment,
                session_id="other-session",
                participant_number=9,
                participant_age=30,
                participant_gender="Other",
                block_count=1,
                source_experiment_name=experiment.name,
                source_experiment_id=str(experiment.id),
                status="completed",
                created_by=other.id,
            )
            hidden_result = RunResult(
                id=uuid.uuid4(),
                run=hidden_run,
                block_index=1,
                block_count=1,
                block_name="private-block",
                block_completed=True,
                experiment_completed=True,
                completed_word_count=1,
                expected_word_count=1,
                schema_version="1.3",
                app_version="1.0",
                result_timestamp="20260808_120000",
                storage_key=f"hidden/{hidden_sha}.json",
                original_filename="hidden.json",
                sha256=hidden_sha,
                size_bytes=1,
                created_by=other.id,
            )
            database.add_all([hidden_run, hidden_result])
            database.commit()
            hidden_run_id = hidden_run.id

        legacy = self.client.get(f"/api/v1/runs/{self.run['id']}/analysis-state")
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(legacy.headers["x-analysis-revision"], "0")
        advanced = self.put_state(self.state(), etag=legacy.headers["etag"])
        self.assertEqual(advanced.status_code, 200, advanced.text)
        self.assertEqual(advanced.json()["revision"], 1)

        resolved = self.client.post(
            "/api/v1/run-results/resolve", json={"sha256": [hidden_sha]}
        )
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(
            [item["sha256"] for item in resolved.json()["results"]],
            [hidden_sha],
        )
        self.assertEqual(resolved.json()["missing_sha256"], [])
        shared_run = self.client.get(f"/api/v1/runs/{hidden_run_id}")
        self.assertEqual(shared_run.status_code, 200, shared_run.text)
        hidden_revision_id = uuid.uuid4()
        shared_copies = self.client.get(
            f"/api/v1/runs/{hidden_run_id}/analysis-copies"
        )
        self.assertEqual(shared_copies.status_code, 200, shared_copies.text)
        self.assertEqual(shared_copies.json(), [])
        self.assertEqual(
            self.client.post(
                f"/api/v1/runs/{hidden_run_id}/analysis-copies/"
                f"{hidden_revision_id}/set-editable"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/runs/{hidden_run_id}/analysis-copies/"
                f"{hidden_revision_id}"
            ).status_code,
            404,
        )

    def test_failed_object_deletion_remains_durably_queued(self):
        saved = self.put_state(self.state())
        self.assertEqual(saved.status_code, 200, saved.text)
        self.storage.fail_removes = True
        deleted = self.client.delete(f"/api/v1/runs/{self.run['id']}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        with self.sessions() as database:
            tasks = database.scalars(select(ObjectDeletionTask)).all()
        self.assertGreaterEqual(len(tasks), 2)
        self.assertTrue(all(task.attempt_count == 1 for task in tasks))


if __name__ == "__main__":
    unittest.main()
