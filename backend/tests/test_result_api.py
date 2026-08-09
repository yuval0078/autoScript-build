import json
import unittest
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import (
    Base,
    Experiment,
    ExperimentRevision,
    ExperimentRevisionBlock,
    ExperimentRun,
    User,
)
from app.services.storage import get_object_storage


def raw_result(experiment_id, *, block_index=1, block_count=2):
    return {
        "schema_version": "1.2",
        "app_version": "1.0.3.1",
        "experiment_name": "Results Study",
        "experiment_id": experiment_id,
        "experiment_version": 1,
        "block_name": f"block-{block_index}",
        "block_id": None,
        "block_index": block_index,
        "block_count": block_count,
        "block_completed": True,
        "experiment_completed": True,
        "completed_word_count": 0,
        "expected_word_count": 0,
        "session_experiment_index": block_index,
        "session_experiment_count": block_count,
        "participant_number": 7,
        "participant_age": 25,
        "participant_gender": "Other",
        "session_id": "7_20260807_120000_abcdef",
        "timestamp": "20260807_120000",
        "calibration": {
            "corners": [[10.0, 10.0], [100.0, 10.0], [100.0, 70.0], [10.0, 70.0]]
        },
        "config": {},
        "words": [],
    }


def word_record(word="test"):
    return {
        "word": word,
        "cell": 0,
        "group": "group-a",
        "start_time": 1.0,
        "end_time": 2.0,
        "audio_start_time": 1.0,
        "audio_end_time": 1.5,
        "pen_events": [],
    }


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        self.objects[object_name] = file_path.read_bytes()

    def iter_object(self, object_name, chunk_size=1024 * 1024):
        yield self.objects[object_name]

    def remove_object(self, object_name):
        self.objects.pop(object_name, None)


class ResultApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )
        with self.session_factory() as session:
            session.add(
                User(
                    username="local-admin",
                    password_hash="!test!",
                    role="admin",
                    is_active=True,
                )
            )
            session.commit()
        self.storage = FakeStorage()
        self.app = create_app()

        def override_database():
            with self.session_factory() as session:
                yield session

        self.app.dependency_overrides[get_db] = override_database
        self.app.dependency_overrides[get_object_storage] = lambda: self.storage
        self.client = TestClient(self.app)
        created = self.client.post(
            "/api/v1/experiments", json={"name": "Results Study"}
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.experiment = created.json()

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def upload(self, payload, filename="result.json"):
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = self.client.post(
            f"/api/v1/experiments/{self.experiment['id']}/results",
            content=content,
            headers={
                "Content-Type": "application/json",
                "X-Filename": filename,
            },
        )
        return response, content

    def test_results_are_grouped_idempotent_immutable_and_downloadable(self):
        first_payload = raw_result(self.experiment["id"], block_index=1)
        first, first_bytes = self.upload(first_payload, "first.json")
        self.assertEqual(first.status_code, 201, first.text)

        retry, _ = self.upload(first_payload, "renamed.json")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertEqual(len(self.storage.objects), 1)

        conflicting_payload = deepcopy(first_payload)
        conflicting_payload["timestamp"] = "20260807_120001"
        conflict, _ = self.upload(conflicting_payload)
        self.assertEqual(conflict.status_code, 409, conflict.text)

        second_payload = raw_result(self.experiment["id"], block_index=2)
        second, _ = self.upload(second_payload, "second.json")
        self.assertEqual(second.status_code, 201, second.text)

        listed = self.client.get(
            f"/api/v1/experiments/{self.experiment['id']}/runs"
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 1)
        run = listed.json()[0]
        self.assertTrue(run["complete"])
        self.assertIsNone(run["analysis_completed"])
        self.assertEqual(run["result_count"], 2)
        self.assertEqual(
            [result["block_index"] for result in run["results"]], [1, 2]
        )

        fetched = self.client.get(f"/api/v1/runs/{run['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        downloaded = self.client.get(first.json()["download_url"])
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, first_bytes)
        self.assertEqual(downloaded.headers["x-checksum-sha256"], first.json()["sha256"])

        deleted = self.client.delete(
            f"/api/v1/experiments/{self.experiment['id']}"
        )
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self.storage.objects, {})

    def test_run_list_pagination_filters_compact_mode_and_owner_scope(self):
        first_payload = raw_result(self.experiment["id"], block_count=1)
        first_payload.update(
            {
                "participant_number": 7,
                "completed_word_count": 1,
                "expected_word_count": 1,
                "words": [word_record()],
            }
        )
        first_result, _ = self.upload(first_payload, "first-participant.json")
        self.assertEqual(first_result.status_code, 201, first_result.text)
        first_run_id = first_result.json()["run_id"]
        for kind, content in (
            ("analysis_csv", b"Participant,Word\n7,test\n"),
            ("trainable_json", b'{"words":[]}'),
        ):
            uploaded = self.client.post(
                f"/api/v1/runs/{first_run_id}/artifacts/{kind}",
                content=content,
                headers={"X-Filename": f"first.{kind.rsplit('_', 1)[-1]}"},
            )
            self.assertIn(uploaded.status_code, {200, 201}, uploaded.text)

        incomplete_payload = raw_result(self.experiment["id"], block_count=1)
        incomplete_payload.update(
            {
                "participant_number": 9,
                "session_id": "9_20260807_120001_abcdee",
                "block_completed": False,
                "experiment_completed": False,
                "completed_word_count": 1,
                "expected_word_count": 2,
                "words": [word_record()],
            }
        )
        incomplete_result, _ = self.upload(
            incomplete_payload,
            "incomplete-participant.json",
        )
        self.assertEqual(incomplete_result.status_code, 201, incomplete_result.text)

        shared_timestamp = datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc)
        with self.session_factory() as session:
            actor = session.query(User).filter_by(username="local-admin").one()
            experiment_id = uuid.UUID(self.experiment["id"])
            for session_id, participant_number, run_status in (
                ("literal%_session", 8, "failed"),
                ("literalXXsession", 7, "running"),
            ):
                session.add(
                    ExperimentRun(
                        experiment_id=experiment_id,
                        revision_id=None,
                        session_id=session_id,
                        participant_number=participant_number,
                        participant_age=30,
                        participant_gender="Other",
                        block_count=1,
                        source_experiment_name=self.experiment["name"],
                        source_experiment_id=self.experiment["id"],
                        status=run_status,
                        created_at=shared_timestamp,
                        created_by=actor.id,
                    )
                )
            for run in session.query(ExperimentRun).filter_by(
                experiment_id=experiment_id
            ):
                run.created_at = shared_timestamp

            other_user = User(
                username="other-results-owner",
                password_hash="!test!",
                role="user",
                is_active=True,
            )
            session.add(other_user)
            session.flush()
            private_experiment = Experiment(
                name="Private Results Study",
                owner_id=other_user.id,
            )
            session.add(private_experiment)
            session.flush()
            session.add(
                ExperimentRun(
                    experiment_id=private_experiment.id,
                    revision_id=None,
                    session_id="private-session",
                    participant_number=7,
                    participant_age=30,
                    participant_gender="Other",
                    block_count=1,
                    source_experiment_name=private_experiment.name,
                    source_experiment_id=str(private_experiment.id),
                    status="running",
                    created_by=other_user.id,
                )
            )
            session.commit()
            private_experiment_id = private_experiment.id

        runs_url = f"/api/v1/experiments/{self.experiment['id']}/runs"
        legacy = self.client.get(runs_url)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertNotIn("x-total-count", legacy.headers)
        expected_ids = [run["id"] for run in legacy.json()]
        self.assertEqual(len(expected_ids), 4)

        collected_ids = []
        cursor = None
        while True:
            params = {"limit": 2, "include_files": "false"}
            if cursor is not None:
                params["cursor"] = cursor
            page = self.client.get(runs_url, params=params)
            self.assertEqual(page.status_code, 200, page.text)
            self.assertEqual(page.headers["x-total-count"], "4")
            self.assertTrue(
                all(not run["results"] and not run["artifacts"] for run in page.json())
            )
            collected_ids.extend(run["id"] for run in page.json())
            cursor = page.headers.get("x-next-cursor")
            if cursor is None:
                break
        self.assertEqual(collected_ids, expected_ids)
        self.assertEqual(len(collected_ids), len(set(collected_ids)))

        compact = self.client.get(
            runs_url,
            params={
                "limit": 10,
                "has_analyzed_csv": "true",
                "has_trainable_json": "true",
                "include_files": "false",
            },
        )
        self.assertEqual(compact.status_code, 200, compact.text)
        self.assertEqual(compact.headers["x-total-count"], "1")
        compact_run = compact.json()[0]
        self.assertEqual(compact_run["id"], first_run_id)
        self.assertEqual(compact_run["raw_data_count"], 1)
        self.assertEqual(compact_run["analyzed_csv_count"], 1)
        self.assertEqual(compact_run["trainable_json_count"], 1)
        self.assertEqual(compact_run["completed_word_count"], 1)
        self.assertEqual(compact_run["expected_word_count"], 1)
        self.assertTrue(compact_run["complete"])
        self.assertEqual(compact_run["results"], [])
        self.assertEqual(compact_run["artifacts"], [])

        combined = self.client.get(
            runs_url,
            params={
                "participant_number": 8,
                "status": "failed",
                "has_raw_data": "false",
                "limit": 10,
            },
        )
        self.assertEqual(combined.status_code, 200, combined.text)
        self.assertEqual(
            [run["session_id"] for run in combined.json()],
            ["literal%_session"],
        )
        self.assertEqual(combined.headers["x-total-count"], "1")

        literal_session = self.client.get(
            runs_url,
            params={"session_search": "%_", "limit": 10},
        )
        self.assertEqual(
            [run["session_id"] for run in literal_session.json()],
            ["literal%_session"],
        )
        complete = self.client.get(
            runs_url,
            params={"complete": "true", "limit": 10},
        )
        incomplete = self.client.get(
            runs_url,
            params={"complete": "false", "limit": 10},
        )
        expected_complete_ids = {
            run["id"] for run in legacy.json() if run["complete"]
        }
        expected_incomplete_ids = set(expected_ids) - expected_complete_ids
        self.assertEqual(
            {run["id"] for run in complete.json()},
            expected_complete_ids,
        )
        self.assertEqual(
            {run["id"] for run in incomplete.json()},
            expected_incomplete_ids,
        )
        no_raw = self.client.get(
            runs_url,
            params={"has_raw_data": "false", "limit": 10},
        )
        self.assertEqual(
            {run["session_id"] for run in no_raw.json()},
            {"literal%_session", "literalXXsession"},
        )

        self.assertEqual(
            self.client.get(runs_url, params={"cursor": "opaque"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                runs_url,
                params={"limit": 2, "cursor": "not-a-valid-cursor"},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(runs_url, params={"status": "unknown"}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(runs_url, params={"participant_number": 0}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/experiments/{private_experiment_id}/runs",
                params={"participant_number": 7, "limit": 10},
            ).status_code,
            404,
        )

    def test_compact_run_list_uses_lightweight_owner_lookup_and_validates_cursor_first(self):
        runs_url = f"/api/v1/experiments/{self.experiment['id']}/runs"
        compact_statements = []

        def record_compact_sql(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            compact_statements.append(" ".join(statement.casefold().split()))

        event.listen(self.engine, "before_cursor_execute", record_compact_sql)
        try:
            compact = self.client.get(
                runs_url,
                params={"include_files": "false", "limit": 1},
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", record_compact_sql)
        self.assertEqual(compact.status_code, 200, compact.text)
        self.assertFalse(
            any(" from experiment_blocks " in statement for statement in compact_statements),
            compact_statements,
        )

        invalid_cursor_statements = []

        def record_invalid_cursor_sql(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            invalid_cursor_statements.append(statement.casefold())

        event.listen(
            self.engine,
            "before_cursor_execute",
            record_invalid_cursor_sql,
        )
        try:
            invalid = self.client.get(
                runs_url,
                params={"limit": 1, "cursor": "not-a-valid-cursor"},
            )
        finally:
            event.remove(
                self.engine,
                "before_cursor_execute",
                record_invalid_cursor_sql,
            )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertFalse(
            any("count(" in statement for statement in invalid_cursor_statements),
            invalid_cursor_statements,
        )

    def test_run_metadata_conflict_is_rejected(self):
        first_payload = raw_result(self.experiment["id"], block_index=1)
        first, _ = self.upload(first_payload)
        self.assertEqual(first.status_code, 201, first.text)

        second_payload = raw_result(self.experiment["id"], block_index=2)
        second_payload["participant_age"] = 26
        conflict, _ = self.upload(second_payload)
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_run_is_pinned_to_declared_experiment_revision(self):
        revision_id = uuid.uuid4()
        revision_block_id = uuid.uuid4()
        with self.session_factory() as session:
            actor = session.query(User).one()
            revision = ExperimentRevision(
                id=revision_id,
                experiment_id=uuid.UUID(self.experiment["id"]),
                revision_number=1,
                name="Results Study",
                created_by=actor.id,
            )
            revision.blocks = [ExperimentRevisionBlock(
                id=revision_block_id, source_block_id=None, position=0,
                same_page_as_previous=False, name="block-1",
                expected_word_count=0, grid_rows=1, grid_cols=1,
                storage_key="revision/0.zip", original_filename="block-1.zip",
                sha256="1" * 64, size_bytes=10,
            )]
            session.add(revision)
            session.commit()
        payload = raw_result(self.experiment["id"], block_count=1)
        payload.update({
            "schema_version": "1.3",
            "experiment_revision_id": str(revision_id),
            "experiment_revision_number": 1,
            "server_run_id": None,
            "block_id": str(revision_block_id),
        })
        uploaded, _ = self.upload(payload)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        run = self.client.get(
            f"/api/v1/experiments/{self.experiment['id']}/runs"
        ).json()[0]
        self.assertEqual(run["revision_id"], str(revision_id))

    def test_explicit_run_lifecycle_is_idempotent_and_finalizes(self):
        revision_id = uuid.uuid4()
        with self.session_factory() as session:
            actor = session.query(User).one()
            revision = ExperimentRevision(
                id=revision_id, experiment_id=uuid.UUID(self.experiment["id"]),
                revision_number=1, name="Results Study", created_by=actor.id,
            )
            revision.blocks = [
                ExperimentRevisionBlock(
                    id=uuid.uuid4(), source_block_id=None, position=index,
                    same_page_as_previous=False,
                    name=f"block-{index + 1}", storage_key=f"revision/{index}.zip",
                    original_filename=f"block-{index + 1}.zip", sha256=str(index + 1) * 64,
                    size_bytes=10, expected_word_count=0, grid_rows=1, grid_cols=1,
                )
                for index in range(2)
            ]
            session.add(revision)
            session.commit()
        create_payload = {
            "session_id": "7_20260807_120000_abcdef",
            "participant_number": 7,
            "participant_age": 25,
            "participant_gender": "Other",
        }
        created = self.client.post(
            f"/api/v1/experiment-revisions/{revision_id}/runs", json=create_payload
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["id"]
        self.assertEqual(created.json()["status"], "created")
        retried = self.client.post(
            f"/api/v1/experiment-revisions/{revision_id}/runs", json=create_payload
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        started = self.client.post(f"/api/v1/runs/{run_id}/start")
        self.assertEqual(started.json()["status"], "running")
        for block_index in (1, 2):
            payload = raw_result(self.experiment["id"], block_index=block_index)
            payload.update({
                "schema_version": "1.3",
                "experiment_revision_id": str(revision_id),
                "experiment_revision_number": 1,
                "server_run_id": run_id,
                "block_id": str(revision.blocks[block_index - 1].id),
            })
            content = json.dumps(payload).encode("utf-8")
            uploaded = self.client.post(
                f"/api/v1/runs/{run_id}/results", content=content,
                headers={"Content-Type": "application/json", "X-Filename": f"{block_index}.json"},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            if block_index == 1:
                incomplete = self.client.post(f"/api/v1/runs/{run_id}/finalize")
                self.assertEqual(incomplete.json()["status"], "incomplete")
                resumed = self.client.post(f"/api/v1/runs/{run_id}/start")
                self.assertEqual(resumed.json()["status"], "running")
                self.assertIsNone(resumed.json()["finalized_at"])
        finalized = self.client.post(f"/api/v1/runs/{run_id}/finalize")
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertEqual(finalized.json()["status"], "completed")
        self.assertIsNotNone(finalized.json()["finalized_at"])

    def test_revision_truth_controls_result_identity_and_completeness(self):
        revision_id = uuid.uuid4()
        revision_block_id = uuid.uuid4()
        with self.session_factory() as session:
            actor = session.query(User).one()
            revision = ExperimentRevision(
                id=revision_id,
                experiment_id=uuid.UUID(self.experiment["id"]),
                revision_number=1,
                name="Results Study",
                created_by=actor.id,
            )
            revision.blocks = [ExperimentRevisionBlock(
                id=revision_block_id,
                source_block_id=None,
                position=0,
                same_page_as_previous=False,
                name="block-1",
                expected_word_count=2,
                grid_rows=1,
                grid_cols=2,
                storage_key="revision/authoritative.zip",
                original_filename="block-1.zip",
                sha256="a" * 64,
                size_bytes=10,
            )]
            session.add(revision)
            session.commit()

        created = self.client.post(
            f"/api/v1/experiment-revisions/{revision_id}/runs",
            json={
                "session_id": "7_20260807_120000_abcdef",
                "participant_number": 7,
                "participant_age": 25,
                "participant_gender": "Other",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        run_id = created.json()["id"]
        self.assertEqual(created.json()["expected_word_count"], 2)
        self.client.post(f"/api/v1/runs/{run_id}/start")

        payload = raw_result(self.experiment["id"], block_count=1)
        payload.update({
            "schema_version": "1.3",
            "experiment_revision_id": str(revision_id),
            "experiment_revision_number": 1,
            "server_run_id": str(uuid.uuid4()),
            "block_id": str(revision_block_id),
            "completed_word_count": 2,
            "expected_word_count": 2,
            "block_completed": False,
            "experiment_completed": False,
            "words": [word_record("one"), word_record("two")],
        })
        conflicting = self.client.post(
            f"/api/v1/runs/{run_id}/results",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(conflicting.status_code, 409, conflicting.text)
        self.assertIn("server Run ID", conflicting.json()["detail"])
        self.assertEqual(self.storage.objects, {})

        payload["server_run_id"] = run_id
        payload["block_name"] = "wrong-block"
        wrong_block = self.client.post(
            f"/api/v1/runs/{run_id}/results",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(wrong_block.status_code, 409, wrong_block.text)
        self.assertIn("Block name", wrong_block.json()["detail"])

        payload["block_name"] = "block-1"
        payload["expected_word_count"] = 3
        wrong_count = self.client.post(
            f"/api/v1/runs/{run_id}/results",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(wrong_count.status_code, 409, wrong_count.text)
        self.assertIn("expected word count", wrong_count.json()["detail"])

        payload["expected_word_count"] = 2
        accepted = self.client.post(
            f"/api/v1/runs/{run_id}/results",
            content=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertTrue(accepted.json()["block_completed"])
        self.assertEqual(accepted.json()["completed_word_count"], 2)
        self.assertEqual(accepted.json()["expected_word_count"], 2)

        finalized = self.client.post(f"/api/v1/runs/{run_id}/finalize")
        self.assertEqual(finalized.status_code, 200, finalized.text)
        self.assertEqual(finalized.json()["status"], "completed")
        self.assertTrue(finalized.json()["complete"])

    def test_legacy_result_without_block_identity_is_accepted(self):
        payload = raw_result(self.experiment["id"], block_index=1, block_count=1)
        payload["schema_version"] = "1.0"
        for key in (
            "block_name",
            "block_id",
            "block_index",
            "block_count",
            "block_completed",
            "experiment_completed",
            "completed_word_count",
            "expected_word_count",
        ):
            payload.pop(key)

        uploaded, _ = self.upload(payload, "legacy.json")
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(uploaded.json()["block_name"], "Results Study")
        self.assertEqual(uploaded.json()["block_index"], 1)

    def test_invalid_result_is_rejected_without_storage_write(self):
        response, _ = self.upload({"not": "a result"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.storage.objects, {})

    def test_incomplete_word_metadata_marks_run_incomplete(self):
        payload = raw_result(self.experiment["id"], block_count=1)
        payload.update(
            {
                "block_completed": False,
                "experiment_completed": False,
                "completed_word_count": 1,
                "expected_word_count": 2,
            }
        )
        payload["words"] = [word_record()]
        uploaded, _ = self.upload(payload)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        run = self.client.get(
            f"/api/v1/experiments/{self.experiment['id']}/runs"
        ).json()[0]
        self.assertFalse(run["complete"])
        self.assertEqual(run["completed_word_count"], 1)
        self.assertEqual(run["expected_word_count"], 2)

    def test_analysis_status_and_immutable_artifacts(self):
        uploaded, _ = self.upload(
            raw_result(self.experiment["id"], block_count=1)
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        run = self.client.get(
            f"/api/v1/experiments/{self.experiment['id']}/runs"
        ).json()[0]

        csv_bytes = b"Participant,Word\n7,test\n"
        artifact = self.client.post(
            f"/api/v1/runs/{run['id']}/artifacts/analysis_csv",
            content=csv_bytes,
            headers={"Content-Type": "text/csv", "X-Filename": "analysis.csv"},
        )
        self.assertEqual(artifact.status_code, 201, artifact.text)
        retried = self.client.post(
            f"/api/v1/runs/{run['id']}/artifacts/analysis_csv",
            content=csv_bytes,
            headers={"Content-Type": "text/csv", "X-Filename": "renamed.csv"},
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(retried.json()["id"], artifact.json()["id"])

        trainable = self.client.post(
            f"/api/v1/runs/{run['id']}/artifacts/trainable_json",
            content=b'{"words":[]}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(trainable.status_code, 201, trainable.text)
        invalid = self.client.post(
            f"/api/v1/runs/{run['id']}/artifacts/trainable_json",
            content=b"not-json",
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)

        analyzed = self.client.patch(
            f"/api/v1/runs/{run['id']}/analysis", json={"completed": True}
        )
        self.assertEqual(analyzed.status_code, 200, analyzed.text)
        self.assertTrue(analyzed.json()["analysis_completed"])
        self.assertIsNotNone(analyzed.json()["analysis_updated_at"])
        self.assertEqual(analyzed.json()["raw_data_count"], 1)
        self.assertEqual(analyzed.json()["analyzed_csv_count"], 1)
        self.assertEqual(analyzed.json()["trainable_json_count"], 1)
        self.assertEqual(len(analyzed.json()["artifacts"]), 2)
        downloaded = self.client.get(artifact.json()["download_url"])
        self.assertEqual(downloaded.content, csv_bytes)

        deleted = self.client.delete(f"/api/v1/runs/{run['id']}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self.storage.objects, {})
        self.assertEqual(
            self.client.get(
                f"/api/v1/experiments/{self.experiment['id']}/runs"
            ).json(),
            [],
        )


if __name__ == "__main__":
    unittest.main()
