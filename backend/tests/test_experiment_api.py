import hashlib
import io
import json
import sys
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base, Experiment, ExperimentRun, StagedBlockAsset, User
from app.services.storage import get_object_storage


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
from experiment_packages import unpack_experiment_package


def block_package(
    name="block-a",
    include_app_version=True,
    include_auto_slice_word=True,
    include_owner_group=True,
    order="stiff",
    repetitions=1,
    grid_rows=1,
    grid_cols=1,
):
    config = {
        "app_version": "1.0.3.1",
        "name": name,
        "grid": {"rows": grid_rows, "cols": grid_cols},
        "order": order,
        "sequence": ["word-1"],
        "repetitions": {"group-a": repetitions},
        "active_block_sequence": [["group-a", 1]],
        "proceed_condition": {"type": "key", "delay_ms": 0},
        "beeps": {
            "before": {"enabled": False, "delay_ms": 0},
            "after": {"enabled": False, "delay_ms": 0},
        },
        "files": [
            {
                "file_name": "word.wav",
                "path": "media/word.wav",
                "original_name": "word.wav",
                "owner_group": "group-a",
                "auto_slice_word": True,
            }
        ],
        "groups": [
            {
                "name": "group-a",
                "words": [
                    {
                        "id": "word-1",
                        "text": "test",
                        "source_file": "word.wav",
                        "start_ms": 0,
                        "end_ms": 250,
                    }
                ],
            }
        ],
    }
    if not include_app_version:
        config.pop("app_version")
    if not include_auto_slice_word:
        config["files"][0].pop("auto_slice_word")
    if not include_owner_group:
        config["files"][0].pop("owner_group")
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}.json", json.dumps(config))
        archive.writestr("media/word.wav", b"RIFF-fixture")
    return package.getvalue()


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.copy_attempts = 0
        self.fail_copy_at = None

    def bucket_exists(self):
        return True

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        self.objects[object_name] = file_path.read_bytes()

    def iter_object(self, object_name, chunk_size=1024 * 1024):
        yield self.objects[object_name]

    def remove_object(self, object_name):
        self.objects.pop(object_name, None)

    def copy_object(self, source_object_name, destination_object_name):
        self.copy_attempts += 1
        if self.copy_attempts == self.fail_copy_at:
            raise RuntimeError("simulated object-storage copy failure")
        self.objects[destination_object_name] = self.objects[source_object_name]


class ExperimentApiTests(unittest.TestCase):
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

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def create_experiment(self, name="Study"):
        response = self.client.post("/api/v1/experiments", json={"name": name})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def upload_block(self, experiment_id, name, position=None, package=None):
        headers = {
            "Content-Type": "application/zip",
            "X-Filename": f"{name}.zip",
            "X-Block-Name": name,
        }
        if position is not None:
            headers["X-Position"] = str(position)
        return self.client.post(
            f"/api/v1/experiments/{experiment_id}/blocks",
            content=package if package is not None else block_package(name),
            headers=headers,
        )

    def legacy_publish(self, experiment_id, package):
        return self.client.post(
            f"/api/v1/experiments/{experiment_id}/versions",
            content=package,
            headers={
                "Content-Type": "application/zip",
                "X-Filename": "legacy.zip",
            },
        )

    def stage_block(self, name, *, package=None, request_id=None):
        return self.client.post(
            "/api/v1/staged-blocks",
            content=package if package is not None else block_package(name),
            headers={
                "Content-Type": "application/zip",
                "X-Filename": f"{name}.zip",
                "X-Block-Name": name,
                "X-Idempotency-Key": str(request_id or uuid.uuid4()),
            },
        )

    def publish_create(self, name, staged_ids, *, request_id=None, joins=None):
        joins = set(joins or [])
        return self.client.post(
            "/api/v1/experiments/publish",
            json={
                "request_id": str(request_id or uuid.uuid4()),
                "name": name,
                "blocks": [
                    {
                        "source": "staged",
                        "id": staged_id,
                        "same_page_as_previous": index in joins,
                    }
                    for index, staged_id in enumerate(staged_ids)
                ],
            },
        )

    def test_create_rename_and_duplicate_name_conflict(self):
        experiment = self.create_experiment()
        self.assertEqual(experiment["name"], "Study")
        self.assertEqual(experiment["blocks"], [])

        renamed = self.client.patch(
            f"/api/v1/experiments/{experiment['id']}",
            json={"name": "Renamed Study"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "Renamed Study")

        duplicate_name = self.client.post(
            "/api/v1/experiments",
            json={"name": "Renamed Study"},
        )
        self.assertEqual(duplicate_name.status_code, 409)

    def test_experiment_list_counts_distinct_participant_numbers(self):
        experiment = self.create_experiment("Participant Count Study")
        with self.session_factory() as session:
            owner = session.query(User).filter_by(username="local-admin").one()
            for session_id, participant_number, analysis_completed in (
                ("session-1", 7, True),
                ("session-2", 7, True),
                ("session-3", 12, False),
            ):
                session.add(
                    ExperimentRun(
                        experiment_id=uuid.UUID(experiment["id"]),
                        revision_id=None,
                        session_id=session_id,
                        participant_number=participant_number,
                        participant_age=30,
                        participant_gender="Other",
                        block_count=1,
                        source_experiment_name=experiment["name"],
                        source_experiment_id=experiment["id"],
                        status="running",
                        analysis_completed=analysis_completed,
                        created_by=owner.id,
                    )
                )
            session.commit()

        response = self.client.get("/api/v1/experiments")

        self.assertEqual(response.status_code, 200, response.text)
        listed = next(item for item in response.json() if item["id"] == experiment["id"])
        self.assertEqual(listed["participant_count"], 2)
        self.assertEqual(listed["analyzed_participant_count"], 1)

    def test_upload_insert_reorder_download_and_delete_blocks(self):
        experiment = self.create_experiment()
        first_package = block_package("first")
        first = self.upload_block(
            experiment["id"], "first", package=first_package
        )
        second = self.upload_block(experiment["id"], "second", position=0)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["expected_word_count"], 1)
        self.assertEqual(first.json()["grid_rows"], 1)
        self.assertEqual(first.json()["grid_cols"], 1)

        listed = self.client.get("/api/v1/experiments").json()[0]
        self.assertEqual(
            [(block["name"], block["position"]) for block in listed["blocks"]],
            [("second", 0), ("first", 1)],
        )

        downloaded = self.client.get(first.json()["download_url"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, first_package)
        self.assertEqual(
            downloaded.headers["x-checksum-sha256"],
            hashlib.sha256(first_package).hexdigest(),
        )

        reordered = self.client.put(
            f"/api/v1/experiments/{experiment['id']}/blocks/order",
            json={
                "block_ids": [first.json()["id"], second.json()["id"]],
                "same_page_block_ids": [second.json()["id"]],
            },
        )
        self.assertEqual(reordered.status_code, 200, reordered.text)
        self.assertEqual(
            [block["name"] for block in reordered.json()["blocks"]],
            ["first", "second"],
        )
        self.assertEqual(
            [block["same_page_as_previous"] for block in reordered.json()["blocks"]],
            [False, True],
        )

        deleted = self.client.delete(f"/api/v1/blocks/{first.json()['id']}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        listed = self.client.get("/api/v1/experiments").json()[0]
        self.assertEqual(
            [(block["name"], block["position"]) for block in listed["blocks"]],
            [("second", 0)],
        )
        self.assertFalse(listed["blocks"][0]["same_page_as_previous"])
        self.assertEqual(len(self.storage.objects), 1)

    def test_upload_accepts_legacy_block_without_app_version(self):
        experiment = self.create_experiment("Legacy Study")
        package = block_package(
            "00Calibration",
            include_app_version=False,
            include_auto_slice_word=False,
            include_owner_group=False,
        )

        uploaded = self.upload_block(
            experiment["id"],
            "00Calibration",
            package=package,
        )

        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertIsNone(uploaded.json()["app_version"])
        self.assertEqual(
            self.client.get(uploaded.json()["download_url"]).content,
            package,
        )

    def test_upload_persists_random_repetition_and_grid_metrics(self):
        experiment = self.create_experiment("Metrics Study")
        package = block_package(
            "repeated",
            order="random",
            repetitions=3,
            grid_rows=2,
            grid_cols=4,
        )
        uploaded = self.upload_block(
            experiment["id"], "repeated", package=package
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        self.assertEqual(uploaded.json()["expected_word_count"], 3)
        self.assertEqual(uploaded.json()["grid_rows"], 2)
        self.assertEqual(uploaded.json()["grid_cols"], 4)

    def test_atomic_create_publish_is_idempotent_and_exposes_current_revision(self):
        first_bytes = block_package("first", grid_rows=1, grid_cols=2)
        second_bytes = block_package("second", grid_rows=1, grid_cols=2)
        first = self.stage_block("first", package=first_bytes)
        second = self.stage_block("second", package=second_bytes)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)

        request_id = uuid.uuid4()
        created = self.publish_create(
            "Atomic Study",
            [first.json()["id"], second.json()["id"]],
            request_id=request_id,
            joins={1},
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["current_revision_id"], body["current_revision"]["id"])
        self.assertEqual(body["current_revision"]["revision_number"], 1)
        self.assertEqual(
            [block["same_page_as_previous"] for block in body["blocks"]],
            [False, True],
        )
        self.assertEqual(
            self.client.get(f"/api/v1/experiments/{body['id']}").json(), body
        )

        copies_before_retry = self.storage.copy_attempts
        retried = self.publish_create(
            "Atomic Study",
            [first.json()["id"], second.json()["id"]],
            request_id=request_id,
            joins={1},
        )
        self.assertEqual(retried.status_code, 201, retried.text)
        self.assertEqual(retried.json()["current_revision_id"], body["current_revision_id"])
        self.assertEqual(self.storage.copy_attempts, copies_before_retry)
        revisions = self.client.get(
            f"/api/v1/experiments/{body['id']}/revisions"
        ).json()
        self.assertEqual(len(revisions), 1)

        divergent = self.publish_create(
            "Changed Name",
            [first.json()["id"], second.json()["id"]],
            request_id=request_id,
            joins={1},
        )
        self.assertEqual(divergent.status_code, 409, divergent.text)

    def test_failed_atomic_create_does_not_leave_an_empty_experiment(self):
        staged = self.stage_block("only")
        self.assertEqual(staged.status_code, 201, staged.text)
        self.storage.fail_copy_at = self.storage.copy_attempts + 1
        failed = self.publish_create("Never Visible", [staged.json()["id"]])
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertEqual(self.client.get("/api/v1/experiments").json(), [])
        with self.session_factory() as session:
            self.assertIsNone(
                session.query(Experiment).filter_by(name="Never Visible").first()
            )
            asset = session.get(StagedBlockAsset, uuid.UUID(staged.json()["id"]))
            self.assertIsNotNone(asset)
            self.assertIsNone(asset.consumed_at)

    def test_failed_update_preserves_live_blocks_and_old_revision_download(self):
        baseline_bytes = block_package("baseline")
        baseline_stage = self.stage_block("baseline", package=baseline_bytes)
        created = self.publish_create(
            "Safe Update", [baseline_stage.json()["id"]]
        ).json()
        experiment_id = created["id"]
        initial_revision_id = created["current_revision_id"]
        old_revision_url = created["current_revision"]["download_url"]
        replacement = self.stage_block("replacement")

        request_id = uuid.uuid4()
        update_payload = {
            "request_id": str(request_id),
            "expected_current_revision_id": initial_revision_id,
            "name": "Safe Update",
            "blocks": [
                {
                    "source": "staged",
                    "id": replacement.json()["id"],
                    "same_page_as_previous": False,
                }
            ],
        }
        self.storage.fail_copy_at = self.storage.copy_attempts + 2
        failed = self.client.post(
            f"/api/v1/experiments/{experiment_id}/publish", json=update_payload
        )
        self.assertEqual(failed.status_code, 503, failed.text)
        unchanged = self.client.get(f"/api/v1/experiments/{experiment_id}").json()
        self.assertEqual(unchanged["current_revision_id"], initial_revision_id)
        self.assertEqual([block["name"] for block in unchanged["blocks"]], ["baseline"])
        self.assertEqual(self.client.get(old_revision_url).content, baseline_bytes)

        self.storage.fail_copy_at = None
        published = self.client.post(
            f"/api/v1/experiments/{experiment_id}/publish", json=update_payload
        )
        self.assertEqual(published.status_code, 200, published.text)
        self.assertNotEqual(published.json()["current_revision_id"], initial_revision_id)
        self.assertEqual(published.json()["current_revision"]["revision_number"], 2)
        self.assertEqual(self.client.get(old_revision_url).content, baseline_bytes)

        retried = self.client.post(
            f"/api/v1/experiments/{experiment_id}/publish", json=update_payload
        )
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(
            retried.json()["current_revision_id"],
            published.json()["current_revision_id"],
        )

    def test_concurrent_editor_is_rejected_without_advancing_revision(self):
        baseline = self.stage_block("baseline")
        created = self.publish_create("Concurrent", [baseline.json()["id"]]).json()
        expected_revision = created["current_revision_id"]
        first_edit = self.stage_block("first-edit")
        stale_edit = self.stage_block("stale-edit")

        def payload(asset, request_id):
            return {
                "request_id": str(request_id),
                "expected_current_revision_id": expected_revision,
                "name": "Concurrent",
                "blocks": [
                    {
                        "source": "staged",
                        "id": asset.json()["id"],
                        "same_page_as_previous": False,
                    }
                ],
            }

        first = self.client.post(
            f"/api/v1/experiments/{created['id']}/publish",
            json=payload(first_edit, uuid.uuid4()),
        )
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.client.post(
            f"/api/v1/experiments/{created['id']}/publish",
            json=payload(stale_edit, uuid.uuid4()),
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        current = self.client.get(f"/api/v1/experiments/{created['id']}").json()
        self.assertEqual(current["current_revision_id"], first.json()["current_revision_id"])
        self.assertEqual([block["name"] for block in current["blocks"]], ["first-edit"])

    def test_atomic_update_can_mix_unchanged_and_staged_blocks(self):
        first = self.stage_block("first")
        second = self.stage_block("second")
        created = self.publish_create(
            "Mixed Sources", [first.json()["id"], second.json()["id"]]
        ).json()
        replacement = self.stage_block("replacement")
        payload = {
            "request_id": str(uuid.uuid4()),
            "expected_current_revision_id": created["current_revision_id"],
            "name": "Mixed Sources",
            "blocks": [
                {
                    "source": "existing",
                    "id": created["blocks"][1]["id"],
                    "same_page_as_previous": False,
                },
                {
                    "source": "staged",
                    "id": replacement.json()["id"],
                    "same_page_as_previous": False,
                },
            ],
        }
        updated = self.client.post(
            f"/api/v1/experiments/{created['id']}/publish", json=payload
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(
            [block["name"] for block in updated.json()["blocks"]],
            ["second", "replacement"],
        )
        self.assertEqual(updated.json()["current_revision"]["revision_number"], 2)

    def test_staging_idempotency_validation_and_expiry_cleanup(self):
        invalid = self.client.post(
            "/api/v1/staged-blocks",
            content=b"not-a-zip",
            headers={
                "X-Filename": "invalid.zip",
                "X-Block-Name": "invalid",
                "X-Idempotency-Key": str(uuid.uuid4()),
            },
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)
        self.assertEqual(self.storage.objects, {})

        request_id = uuid.uuid4()
        first = self.stage_block("same", request_id=request_id)
        exact = self.stage_block("same", request_id=request_id)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(first.json()["id"], exact.json()["id"])

        divergent = self.stage_block(
            "different", request_id=request_id, package=block_package("different")
        )
        self.assertEqual(divergent.status_code, 409, divergent.text)
        expired_key = next(iter(self.storage.objects))
        with self.session_factory() as session:
            asset = session.get(StagedBlockAsset, uuid.UUID(first.json()["id"]))
            asset.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            session.commit()
        fresh = self.stage_block("fresh")
        self.assertEqual(fresh.status_code, 201, fresh.text)
        self.assertNotIn(expired_key, self.storage.objects)

    def test_publish_rejects_invalid_same_page_layout(self):
        first = self.stage_block("first")
        second = self.stage_block("second")
        rejected = self.publish_create(
            "Too Full",
            [first.json()["id"], second.json()["id"]],
            joins={1},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.client.get("/api/v1/experiments").json(), [])

    def test_rejects_invalid_mismatched_or_incomplete_block_requests(self):
        experiment = self.create_experiment()
        invalid = self.client.post(
            f"/api/v1/experiments/{experiment['id']}/blocks",
            content=b"not-a-zip",
            headers={"X-Filename": "bad.zip", "X-Block-Name": "bad"},
        )
        self.assertEqual(invalid.status_code, 422)

        mismatched = self.client.post(
            f"/api/v1/experiments/{experiment['id']}/blocks",
            content=block_package("inside"),
            headers={"X-Filename": "inside.zip", "X-Block-Name": "outside"},
        )
        self.assertEqual(mismatched.status_code, 422)

        missing_name = self.client.post(
            f"/api/v1/experiments/{experiment['id']}/blocks",
            content=block_package("inside"),
            headers={"X-Filename": "inside.zip"},
        )
        self.assertEqual(missing_name.status_code, 422)
        self.assertEqual(self.storage.objects, {})

        uploaded = self.upload_block(experiment["id"], "inside")
        invalid_first_join = self.client.put(
            f"/api/v1/experiments/{experiment['id']}/blocks/order",
            json={
                "block_ids": [uploaded.json()["id"]],
                "same_page_block_ids": [uploaded.json()["id"]],
            },
        )
        self.assertEqual(invalid_first_join.status_code, 422)
        self.assertEqual(len(self.storage.objects), 1)

    def test_duplicate_copies_blocks_and_uses_collision_safe_names(self):
        experiment = self.create_experiment()
        baseline = self.upload_block(experiment["id"], "baseline")
        followup = self.upload_block(experiment["id"], "followup")
        self.assertEqual(baseline.status_code, 201, baseline.text)
        self.assertEqual(followup.status_code, 201, followup.text)
        layout = self.client.put(
            f"/api/v1/experiments/{experiment['id']}/blocks/order",
            json={
                "block_ids": [baseline.json()["id"], followup.json()["id"]],
                "same_page_block_ids": [followup.json()["id"]],
            },
        )
        self.assertEqual(layout.status_code, 200, layout.text)

        first_copy = self.client.post(
            f"/api/v1/experiments/{experiment['id']}/duplicate"
        )
        second_copy = self.client.post(
            f"/api/v1/experiments/{experiment['id']}/duplicate"
        )
        self.assertEqual(first_copy.status_code, 201, first_copy.text)
        self.assertEqual(second_copy.status_code, 201, second_copy.text)
        self.assertEqual(first_copy.json()["name"], "Study copy")
        self.assertEqual(second_copy.json()["name"], "Study copy 2")
        self.assertEqual(first_copy.json()["blocks"][0]["name"], "baseline")
        self.assertEqual(
            [block["same_page_as_previous"] for block in first_copy.json()["blocks"]],
            [False, True],
        )
        self.assertNotEqual(
            first_copy.json()["blocks"][0]["id"],
            baseline.json()["id"],
        )
        self.assertEqual(len(self.storage.objects), 10)
        self.assertEqual(first_copy.json()["current_revision"]["revision_number"], 1)

    def test_revision_snapshot_remains_downloadable_after_live_block_changes(self):
        experiment = self.create_experiment("Revision Study")
        first_bytes = block_package("first")
        first = self.upload_block(experiment["id"], "first", package=first_bytes)
        revision = self.client.post(f"/api/v1/experiments/{experiment['id']}/revisions")
        self.assertEqual(revision.status_code, 201, revision.text)
        self.assertEqual(revision.json()["revision_number"], 1)
        self.assertEqual(revision.json()["blocks"][0]["expected_word_count"], 1)
        self.assertEqual(revision.json()["blocks"][0]["grid_rows"], 1)
        self.assertEqual(revision.json()["blocks"][0]["grid_cols"], 1)
        history = self.client.get(
            f"/api/v1/experiments/{experiment['id']}/revisions"
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual([item["revision_number"] for item in history.json()], [1])
        self.assertEqual(
            self.client.get(revision.json()["download_url"]).content,
            first_bytes,
        )
        self.client.delete(f"/api/v1/blocks/{first.json()['id']}")
        self.upload_block(experiment["id"], "second")
        second_revision = self.client.post(f"/api/v1/experiments/{experiment['id']}/revisions")
        self.assertEqual(second_revision.json()["revision_number"], 2)
        history = self.client.get(
            f"/api/v1/experiments/{experiment['id']}/revisions"
        ).json()
        self.assertEqual([item["revision_number"] for item in history], [2, 1])
        self.assertEqual(
            self.client.get(revision.json()["download_url"]).content,
            first_bytes,
        )

    def test_multi_block_experiment_download_is_a_manifest_bundle(self):
        experiment = self.create_experiment("Bundle Study")
        packages = {
            "warmup": block_package("warmup"),
            "task": block_package("task"),
        }
        uploaded_blocks = {}
        for name in packages:
            response = self.upload_block(
                experiment["id"], name, package=packages[name]
            )
            self.assertEqual(response.status_code, 201, response.text)
            uploaded_blocks[name] = response.json()

        layout = self.client.put(
            f"/api/v1/experiments/{experiment['id']}/blocks/order",
            json={
                "block_ids": [
                    uploaded_blocks["warmup"]["id"],
                    uploaded_blocks["task"]["id"],
                ],
                "same_page_block_ids": [uploaded_blocks["task"]["id"]],
            },
        )
        self.assertEqual(layout.status_code, 200, layout.text)

        downloaded = self.client.get(
            f"/api/v1/experiments/{experiment['id']}/download"
        )
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(
            downloaded.headers["x-autoscript-package-type"],
            "experiment-bundle",
        )
        with zipfile.ZipFile(io.BytesIO(downloaded.content)) as bundle:
            manifest = json.loads(bundle.read("experiment.json"))
            self.assertEqual(manifest["schema_version"], "1.0")
            self.assertEqual(manifest["package_type"], "experiment")
            self.assertEqual(manifest["name"], "Bundle Study")
            self.assertEqual(manifest["id"], experiment["id"])
            self.assertEqual(
                [block["name"] for block in manifest["blocks"]],
                ["warmup", "task"],
            )
            self.assertEqual(
                [block["same_page_as_previous"] for block in manifest["blocks"]],
                [False, True],
            )
            for block in manifest["blocks"]:
                self.assertRegex(block["path"], r"^blocks/[0-9]{3}_[^/]+\.zip$")
                self.assertEqual(bundle.read(block["path"]), packages[block["name"]])

        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_path = Path(temp_dir) / "bundle.zip"
            bundle_path.write_bytes(downloaded.content)
            resolved = unpack_experiment_package(
                bundle_path,
                Path(temp_dir) / "resolved",
            )
            self.assertEqual(resolved.source_kind, "experiment")
            self.assertEqual(resolved.name, "Bundle Study")
            self.assertEqual(resolved.experiment_id, experiment["id"])
            self.assertEqual(
                [block.name for block in resolved.blocks],
                ["warmup", "task"],
            )
            self.assertEqual(
                [block.same_page_as_previous for block in resolved.blocks],
                [False, True],
            )
            for block in resolved.blocks:
                self.assertEqual(
                    block.archive_path.read_bytes(),
                    packages[block.name],
                )

    def test_single_block_experiment_download_is_the_exact_legacy_zip(self):
        experiment = self.create_experiment()
        package = block_package("only")
        uploaded = self.upload_block(experiment["id"], "only", package=package)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)

        downloaded = self.client.get(
            f"/api/v1/experiments/{experiment['id']}/download"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.content, package)
        self.assertEqual(downloaded.headers["x-autoscript-package-type"], "block")

    def test_legacy_publish_updates_single_block_and_remains_idempotent(self):
        experiment = self.create_experiment("Legacy Container")
        package = block_package("legacy block name")
        first = self.legacy_publish(experiment["id"], package)
        second = self.legacy_publish(experiment["id"], package)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])

        listed = self.client.get("/api/v1/experiments").json()[0]
        self.assertEqual(listed["blocks"][0]["name"], "legacy block name")
        self.assertEqual(len(listed["versions"]), 1)
        self.assertEqual(len(self.storage.objects), 1)

        block_download = self.client.get(listed["blocks"][0]["download_url"])
        version_download = self.client.get(listed["versions"][0]["download_url"])
        self.assertEqual(block_download.content, package)
        self.assertEqual(version_download.content, package)

        replacement = block_package("replacement block")
        replaced = self.legacy_publish(experiment["id"], replacement)
        self.assertEqual(replaced.status_code, 201, replaced.text)
        listed = self.client.get("/api/v1/experiments").json()[0]
        self.assertEqual(len(listed["blocks"]), 1)
        self.assertEqual(listed["blocks"][0]["name"], "replacement block")
        self.assertEqual(len(listed["versions"]), 2)
        self.assertEqual(
            self.client.get(listed["blocks"][0]["download_url"]).content,
            replacement,
        )
        self.assertEqual(len(self.storage.objects), 2)

        # A migrated/legacy block and its version can share one object. Removing
        # the block must preserve version download; removing the experiment can
        # finally clean up the now-unreferenced object.
        deleted_block = self.client.delete(
            f"/api/v1/blocks/{listed['blocks'][0]['id']}"
        )
        self.assertEqual(deleted_block.status_code, 204)
        self.assertEqual(len(self.storage.objects), 2)
        self.assertEqual(
            self.client.get(listed["versions"][1]["download_url"]).content,
            package,
        )
        deleted_experiment = self.client.delete(
            f"/api/v1/experiments/{experiment['id']}"
        )
        self.assertEqual(deleted_experiment.status_code, 204)
        self.assertEqual(self.storage.objects, {})

    def test_delete_experiment_removes_all_unreferenced_objects(self):
        experiment = self.create_experiment()
        self.upload_block(experiment["id"], "first")
        self.upload_block(experiment["id"], "second")
        self.assertEqual(len(self.storage.objects), 2)

        deleted = self.client.delete(f"/api/v1/experiments/{experiment['id']}")
        self.assertEqual(deleted.status_code, 204, deleted.text)
        self.assertEqual(self.client.get("/api/v1/experiments").json(), [])
        self.assertEqual(self.storage.objects, {})


if __name__ == "__main__":
    unittest.main()
