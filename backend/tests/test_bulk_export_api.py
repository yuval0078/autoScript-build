import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import (
    Base,
    Experiment,
    ExperimentRun,
    RunAnalysisRevision,
    RunArtifact,
    RunResult,
    User,
)
from app.services.storage import get_object_storage
from app.api.bulk_exports import _stream_and_remove

try:
    from .test_result_api import FakeStorage
except ImportError:  # unittest discovery imports test modules without a package.
    from test_result_api import FakeStorage


class BulkExportApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        with self.sessions() as database:
            actor = User(
                username="local-admin",
                password_hash="!test!",
                role="admin",
                is_active=True,
            )
            other = User(
                username="other-user",
                password_hash="!test!",
                role="user",
                is_active=True,
            )
            database.add_all([actor, other])
            database.flush()
            experiment = Experiment(name="Bulk / Study", owner_id=actor.id)
            hidden = Experiment(name="Hidden Study", owner_id=other.id)
            database.add_all([experiment, hidden])
            database.commit()
            self.experiment_id = experiment.id
            self.hidden_experiment_id = hidden.id
            self.actor_id = actor.id
            self.other_id = other.id

        self.storage = FakeStorage()
        self.app = create_app()

        def override_database():
            with self.sessions() as database:
                yield database

        self.app.dependency_overrides[get_db] = override_database
        self.app.dependency_overrides[get_object_storage] = lambda: self.storage
        self.client = TestClient(self.app)
        self.run_id = self._add_run(
            self.experiment_id,
            self.actor_id,
            participant_number=7,
            session_id="participant-7-session",
        )

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def _stored(self, key, content):
        self.storage.objects[key] = content
        return hashlib.sha256(content).hexdigest(), len(content)

    def _add_run(
        self,
        experiment_id,
        creator_id,
        *,
        participant_number,
        session_id,
        raw_blocks=2,
    ):
        created_at = datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc)
        with self.sessions() as database:
            experiment = database.get(Experiment, experiment_id)
            run = ExperimentRun(
                experiment_id=experiment_id,
                session_id=session_id,
                participant_number=participant_number,
                participant_age=25,
                participant_gender="Other",
                block_count=max(raw_blocks, 1),
                source_experiment_name=experiment.name,
                source_experiment_id=str(experiment_id),
                status="completed",
                created_by=creator_id,
                created_at=created_at,
            )
            database.add(run)
            database.flush()
            for index in range(1, raw_blocks + 1):
                content = json.dumps({"block": index}).encode("utf-8")
                key = f"runs/{run.id}/raw/{index}.json"
                digest, size = self._stored(key, content)
                database.add(
                    RunResult(
                        run_id=run.id,
                        block_index=index,
                        block_count=raw_blocks,
                        block_name=f"../unsafe/block {index}",
                        block_completed=True,
                        experiment_completed=True,
                        completed_word_count=1,
                        expected_word_count=1,
                        schema_version="1.2",
                        app_version="0.0",
                        result_timestamp=f"20260810_08000{index}",
                        storage_key=key,
                        original_filename=f"block-{index}.json",
                        sha256=digest,
                        size_bytes=size,
                        created_by=creator_id,
                        created_at=created_at + timedelta(seconds=index),
                    )
                )
            database.commit()
            return run.id

    def _add_analysis_revision(self, run_id, revision_number, marker):
        created_at = datetime(2026, 8, 10, 9, revision_number, tzinfo=timezone.utc)
        with self.sessions() as database:
            run = database.get(ExperimentRun, run_id)
            revision = RunAnalysisRevision(
                run_id=run_id,
                revision_number=revision_number,
                source_fingerprint=f"{revision_number:064x}",
                finalized=True,
                completed=True,
                finalized_at=created_at,
                created_by=run.created_by,
                created_at=created_at,
            )
            database.add(revision)
            database.flush()
            for kind, suffix in (
                ("analysis_csv", ".csv"),
                ("trainable_json", ".json"),
            ):
                content = f"{kind}:{marker}".encode("utf-8")
                key = f"runs/{run_id}/analysis/{revision_number}/{kind}{suffix}"
                digest, size = self._stored(key, content)
                database.add(
                    RunArtifact(
                        run_id=run_id,
                        analysis_revision_id=revision.id,
                        kind=kind,
                        storage_key=key,
                        original_filename=f"{kind}{suffix}",
                        sha256=digest,
                        size_bytes=size,
                        created_by=run.created_by,
                        created_at=created_at,
                    )
                )
            database.commit()

    def _add_legacy_artifact(
        self, run_id, kind, marker, minute, *, hour=10, microsecond=0
    ):
        created_at = datetime(
            2026, 8, 10, hour, minute, microsecond=microsecond,
            tzinfo=timezone.utc,
        )
        suffix = ".csv" if kind == "analysis_csv" else ".json"
        with self.sessions() as database:
            run = database.get(ExperimentRun, run_id)
            content = f"legacy:{kind}:{marker}".encode("utf-8")
            key = f"runs/{run_id}/legacy/{minute}/{kind}{suffix}"
            digest, size = self._stored(key, content)
            database.add(
                RunArtifact(
                    run_id=run_id,
                    kind=kind,
                    storage_key=key,
                    original_filename=f"legacy{suffix}",
                    sha256=digest,
                    size_bytes=size,
                    created_by=run.created_by,
                    created_at=created_at,
                )
            )
            database.commit()

    def _export(self, **overrides):
        payload = {
            "run_ids": [str(self.run_id)],
            "include": ["raw_data", "analysis_csv", "trainable_json"],
            "analysis_policy": "latest",
        }
        payload.update(overrides)
        return self.client.post(
            f"/api/v1/experiments/{self.experiment_id}/bulk-export",
            json=payload,
        )

    @staticmethod
    def _archive(response):
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        manifest = json.loads(archive.read("manifest.json"))
        return archive, manifest

    def test_latest_export_contains_all_raw_blocks_and_latest_analysis_copy(self):
        self._add_analysis_revision(self.run_id, 1, "old")
        self._add_analysis_revision(self.run_id, 2, "latest")

        response = self._export()

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertIn("filename*=UTF-8''Bulk___Study-results.zip", response.headers["content-disposition"])
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        self.assertEqual(
            response.headers["x-checksum-sha256"],
            hashlib.sha256(response.content).hexdigest(),
        )
        archive, manifest = self._archive(response)
        self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
        entries = manifest["entries"]
        self.assertEqual(
            [entry["kind"] for entry in entries].count("raw_data"), 2
        )
        self.assertEqual(
            [entry["revision"] for entry in entries if entry["kind"] == "analysis_csv"],
            [2],
        )
        self.assertEqual(
            [entry["revision"] for entry in entries if entry["kind"] == "trainable_json"],
            [2],
        )
        self.assertEqual(manifest["selection"]["analysis_policy"], "latest")
        self.assertFalse(any(".." in name for name in archive.namelist()))
        self.assertFalse(any("\\" in name for name in archive.namelist()))
        exported = [archive.read(entry["path"]) for entry in entries]
        self.assertIn(b"analysis_csv:latest", exported)
        self.assertNotIn(b"analysis_csv:old", exported)
        for entry in entries:
            self.assertNotIn("storage_key", entry)
            self.assertEqual(
                hashlib.sha256(archive.read(entry["path"])).hexdigest(),
                entry["sha256"],
            )

    def test_all_policy_includes_every_finalized_analysis_copy(self):
        self._add_analysis_revision(self.run_id, 1, "first")
        self._add_analysis_revision(self.run_id, 2, "second")

        response = self._export(include=["analysis_csv"], analysis_policy="all")

        self.assertEqual(response.status_code, 200, response.text)
        archive, manifest = self._archive(response)
        csv_entries = [
            entry for entry in manifest["entries"] if entry["kind"] == "analysis_csv"
        ]
        self.assertEqual([entry["revision"] for entry in csv_entries], [1, 2])
        self.assertEqual(
            {archive.read(entry["path"]) for entry in csv_entries},
            {b"analysis_csv:first", b"analysis_csv:second"},
        )

    def test_legacy_only_analysis_obeys_latest_and_all(self):
        self._add_legacy_artifact(self.run_id, "analysis_csv", "old", 1)
        self._add_legacy_artifact(self.run_id, "analysis_csv", "new", 2)

        latest = self._export(include=["analysis_csv"])
        all_copies = self._export(
            include=["analysis_csv"], analysis_policy="all"
        )

        self.assertEqual(latest.status_code, 200, latest.text)
        latest_archive, latest_manifest = self._archive(latest)
        self.assertEqual(len(latest_manifest["entries"]), 1)
        self.assertEqual(latest_manifest["entries"][0]["revision"], None)
        self.assertEqual(
            latest_archive.read(latest_manifest["entries"][0]["path"]),
            b"legacy:analysis_csv:new",
        )
        _, all_manifest = self._archive(all_copies)
        self.assertEqual(len(all_manifest["entries"]), 2)

    def test_mixed_legacy_and_finalized_copies_obey_latest_and_all(self):
        self._add_legacy_artifact(self.run_id, "analysis_csv", "legacy", 1)
        self._add_analysis_revision(self.run_id, 1, "finalized")

        all_response = self._export(
            include=["analysis_csv"], analysis_policy="all"
        )
        latest_response = self._export(
            include=["analysis_csv"], analysis_policy="latest"
        )

        self.assertEqual(all_response.status_code, 200, all_response.text)
        all_archive, all_manifest = self._archive(all_response)
        self.assertEqual(len(all_manifest["entries"]), 2)
        self.assertEqual(
            {
                all_archive.read(entry["path"])
                for entry in all_manifest["entries"]
            },
            {b"analysis_csv:finalized", b"legacy:analysis_csv:legacy"},
        )
        latest_archive, latest_manifest = self._archive(latest_response)
        self.assertEqual(len(latest_manifest["entries"]), 1)
        self.assertIsNone(latest_manifest["entries"][0]["revision"])
        self.assertEqual(
            latest_archive.read(latest_manifest["entries"][0]["path"]),
            b"legacy:analysis_csv:legacy",
        )

    def test_latest_has_a_stable_revision_tie_break(self):
        self._add_legacy_artifact(
            self.run_id, "analysis_csv", "legacy", 1, hour=9
        )
        self._add_analysis_revision(self.run_id, 1, "finalized")

        response = self._export(include=["analysis_csv"])

        self.assertEqual(response.status_code, 200, response.text)
        archive, manifest = self._archive(response)
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertEqual(manifest["entries"][0]["revision"], 1)
        self.assertEqual(
            archive.read(manifest["entries"][0]["path"]),
            b"analysis_csv:finalized",
        )

    def test_latest_compares_subsecond_timestamps_chronologically(self):
        self._add_analysis_revision(self.run_id, 1, "finalized")
        self._add_legacy_artifact(
            self.run_id,
            "analysis_csv",
            "half-second-newer",
            1,
            hour=9,
            microsecond=500_000,
        )

        response = self._export(include=["analysis_csv"])

        self.assertEqual(response.status_code, 200, response.text)
        archive, manifest = self._archive(response)
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertIsNone(manifest["entries"][0]["revision"])
        self.assertEqual(
            archive.read(manifest["entries"][0]["path"]),
            b"legacy:analysis_csv:half-second-newer",
        )

    def test_missing_analysis_artifacts_produce_a_valid_manifest_only_zip(self):
        response = self._export(include=["analysis_csv", "trainable_json"])

        self.assertEqual(response.status_code, 200, response.text)
        archive, manifest = self._archive(response)
        self.assertEqual(archive.namelist(), ["manifest.json"])
        self.assertEqual(manifest["entries"], [])
        self.assertEqual(manifest["runs"][0]["id"], str(self.run_id))

    def test_multiple_runs_are_grouped_by_participant_and_run(self):
        second_run_id = self._add_run(
            self.experiment_id,
            self.actor_id,
            participant_number=12,
            session_id="participant-12-session",
            raw_blocks=1,
        )

        response = self._export(
            run_ids=[str(second_run_id), str(self.run_id)],
            include=["raw_data"],
        )

        self.assertEqual(response.status_code, 200, response.text)
        _, manifest = self._archive(response)
        paths = [entry["path"] for entry in manifest["entries"]]
        self.assertTrue(any("participants/000007/" in path for path in paths))
        self.assertTrue(any("participants/000012/" in path for path in paths))
        self.assertEqual(
            manifest["selection"]["run_ids"],
            [str(second_run_id), str(self.run_id)],
        )

    def test_empty_and_duplicate_selections_are_rejected(self):
        empty_runs = self._export(run_ids=[])
        duplicate_runs = self._export(run_ids=[str(self.run_id)] * 2)
        duplicate_include = self._export(include=["raw_data", "raw_data"])

        self.assertEqual(empty_runs.status_code, 422)
        self.assertEqual(duplicate_runs.status_code, 422)
        self.assertIn("run_ids must not contain duplicates", duplicate_runs.text)
        self.assertEqual(duplicate_include.status_code, 422)
        self.assertIn("include must not contain duplicates", duplicate_include.text)

    def test_missing_or_wrong_experiment_run_is_hidden(self):
        missing = self._export(run_ids=[str(uuid.uuid4())])
        hidden_run = self._add_run(
            self.hidden_experiment_id,
            self.other_id,
            participant_number=99,
            session_id="hidden-session",
            raw_blocks=1,
        )
        cross_experiment = self._export(run_ids=[str(hidden_run)])
        hidden_experiment = self.client.post(
            f"/api/v1/experiments/{self.hidden_experiment_id}/bulk-export",
            json={"run_ids": [str(hidden_run)], "include": ["raw_data"]},
        )

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(cross_experiment.status_code, 404)
        self.assertEqual(hidden_experiment.status_code, 404)

    def test_source_size_is_rejected_before_storage_is_read(self):
        class ReadTrackingStorage(FakeStorage):
            read_count = 0

            def iter_object(self, object_name, chunk_size=1024 * 1024):
                self.read_count += 1
                yield from super().iter_object(object_name, chunk_size)

        tracking_storage = ReadTrackingStorage()
        tracking_storage.objects.update(self.storage.objects)
        self.app.dependency_overrides[get_object_storage] = lambda: tracking_storage
        with patch(
            "app.api.bulk_exports.get_settings",
            return_value=SimpleNamespace(max_uncompressed_package_bytes=1),
        ):
            response = self._export(include=["raw_data"])

        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(tracking_storage.read_count, 0)

    def test_storage_read_failure_removes_partial_archive(self):
        class FailingReadStorage(FakeStorage):
            def iter_object(self, object_name, chunk_size=1024 * 1024):
                raise RuntimeError("simulated object read failure")

        failing_storage = FailingReadStorage()
        failing_storage.objects.update(self.storage.objects)
        self.app.dependency_overrides[get_object_storage] = lambda: failing_storage
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "partial.zip"
            descriptor = os.open(
                archive_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
            )
            with patch(
                "app.api.bulk_exports.tempfile.mkstemp",
                return_value=(descriptor, str(archive_path)),
            ):
                response = self._export(include=["raw_data"])
            self.assertEqual(response.status_code, 500)
            self.assertFalse(archive_path.exists())

    def test_stream_generator_removes_file_when_consumer_disconnects(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "disconnect.zip"
            archive_path.write_bytes(b"x" * (1024 * 1024 + 1))
            stream = _stream_and_remove(archive_path)
            self.assertTrue(next(stream))

            stream.close()

            self.assertFalse(archive_path.exists())

    def test_openapi_documents_zip_contract_and_validation_responses(self):
        operation = self.client.get("/openapi.json").json()["paths"][
            "/api/v1/experiments/{experiment_id}/bulk-export"
        ]["post"]

        self.assertEqual(operation["summary"], "Download selected Run data as one ZIP")
        self.assertIn("application/zip", operation["responses"]["200"]["content"])
        self.assertIn(
            "Content-Disposition", operation["responses"]["200"]["headers"]
        )
        self.assertIn(
            "X-Checksum-SHA256", operation["responses"]["200"]["headers"]
        )
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/zip"][
                "schema"
            ],
            {"type": "string", "format": "binary"},
        )
        self.assertTrue({"401", "404", "413", "422"}.issubset(operation["responses"]))
        schema = self.client.get("/openapi.json").json()["components"]["schemas"][
            "BulkExportRequest"
        ]
        self.assertIn("examples", schema)
        for field in ("run_ids", "include", "analysis_policy"):
            self.assertIn("description", schema["properties"][field])


if __name__ == "__main__":
    unittest.main()
