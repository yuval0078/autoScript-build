import json
import unittest
from copy import deepcopy

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, User
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

    def test_run_metadata_conflict_is_rejected(self):
        first_payload = raw_result(self.experiment["id"], block_index=1)
        first, _ = self.upload(first_payload)
        self.assertEqual(first.status_code, 201, first.text)

        second_payload = raw_result(self.experiment["id"], block_index=2)
        second_payload["participant_age"] = 26
        conflict, _ = self.upload(second_payload)
        self.assertEqual(conflict.status_code, 409, conflict.text)

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
