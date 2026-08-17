import hashlib
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Experiment,
    ExperimentRun,
    RunAnalysisRevision,
    RunArtifact,
    RunResult,
    User,
)
from app.services.analysis import validate_analysis_state
from app.services.historical_edit_state import (
    HistoricalEditStateError,
    seed_historical_edit_states,
)


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.fail_state_upload = False

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        self.objects[object_name] = (Path(file_path).read_bytes(), content_type)
        if self.fail_state_upload and "/analysis/" in object_name:
            raise RuntimeError("injected storage failure")

    def iter_object(self, object_name, chunk_size=1024 * 1024):
        data = self.objects[object_name][0]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def remove_object(self, object_name):
        self.objects.pop(object_name, None)


def _word(char, stroke_id, written, trainability="trainable"):
    return {
        "letters": [{"char": char, "stroke_ids": [stroke_id]}],
        "written_word": written,
        "trainability": trainability,
        "strokes": [],
        "audio_start_time": 1,
        "audio_end_time": 2,
    }


class HistoricalEditStateTests(unittest.TestCase):
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
        self.storage = FakeStorage()
        with self.sessions() as database:
            actor = User(
                id=uuid.uuid4(),
                username="admin",
                password_hash="!test!",
                role="admin",
                is_active=True,
            )
            experiment = Experiment(
                id=uuid.uuid4(), name="pilot", owner_id=actor.id
            )
            run = ExperimentRun(
                id=uuid.uuid4(),
                experiment_id=experiment.id,
                session_id="pilot-p7-session",
                participant_number=7,
                participant_age=25,
                participant_gender="Other",
                block_count=2,
                source_experiment_name="pilot",
                status="completed",
                started_at=datetime.now(timezone.utc),
                finalized_at=datetime.now(timezone.utc),
                analysis_completed=True,
                analysis_updated_at=datetime.now(timezone.utc),
                created_by=actor.id,
            )
            database.add_all([actor, experiment, run])
            database.flush()
            results = []
            for index, (timestamp, count) in enumerate(
                [("20260801_120000", 2), ("20260801_120500", 1)], start=1
            ):
                results.append(
                    RunResult(
                        id=uuid.uuid4(),
                        run_id=run.id,
                        block_id=None,
                        block_index=index,
                        block_count=2,
                        block_name=f"Block {index}",
                        block_completed=True,
                        experiment_completed=True,
                        completed_word_count=count,
                        expected_word_count=count,
                        schema_version="1.3",
                        app_version="legacy",
                        result_timestamp=timestamp,
                        storage_key=f"raw/{index}.json",
                        original_filename=f"raw-{index}.json",
                        sha256=str(index) * 64,
                        size_bytes=10,
                        created_by=actor.id,
                    )
                )
            # Deliberately reverse the Trainable JSON order.  Timestamp identity,
            # rather than array position, must choose the target Block.
            trainable_payload = [
                {
                    "participant_number": 7,
                    "timestamp": "20260801_120500",
                    "words": [_word("ג", 0, "ג")],
                },
                {
                    "participant_number": 7,
                    "timestamp": "20260801_120000",
                    "words": [
                        _word("א", 2, "אב"),
                        _word("ב", 3, "בא", "low-quality"),
                    ],
                },
            ]
            trainable_bytes = json.dumps(
                trainable_payload, ensure_ascii=False
            ).encode("utf-8")
            trainable_sha = hashlib.sha256(trainable_bytes).hexdigest()
            trainable = RunArtifact(
                id=uuid.uuid4(),
                run_id=run.id,
                kind="trainable_json",
                analysis_revision_id=None,
                storage_key="legacy/trainable.json",
                original_filename="trainable.json",
                sha256=trainable_sha,
                size_bytes=len(trainable_bytes),
                created_by=actor.id,
            )
            database.add_all([*results, trainable])
            database.commit()
            self.run_id = run.id
            self.trainable_key = trainable.storage_key
            self.storage.objects[trainable.storage_key] = (
                trainable_bytes,
                "application/json",
            )

    def tearDown(self):
        self.engine.dispose()

    def _load_run(self, database):
        return database.scalar(
            select(ExperimentRun)
            .where(ExperimentRun.id == self.run_id)
            .options(
                selectinload(ExperimentRun.results),
                selectinload(ExperimentRun.artifacts),
                selectinload(ExperimentRun.analysis_revisions).selectinload(
                    RunAnalysisRevision.artifacts
                ),
                selectinload(ExperimentRun.current_analysis_revision).selectinload(
                    RunAnalysisRevision.artifacts
                ),
            )
        )

    def test_dry_run_apply_and_retry_restore_current_state(self):
        with self.sessions() as database:
            dry_run = seed_historical_edit_states(
                database, self.storage, experiment_name="pilot", apply=False
            )
            self.assertEqual(dry_run["new_edit_states"], 1)
            self.assertEqual(dry_run["restored_words"], 3)
            self.assertFalse(dry_run["applied"])
            self.assertIsNone(self._load_run(database).current_analysis_revision_id)

        with self.sessions() as database:
            applied = seed_historical_edit_states(
                database, self.storage, experiment_name="pilot", apply=True
            )
            self.assertEqual(applied["new_edit_states"], 1)
            run = self._load_run(database)
            self.assertTrue(run.analysis_completed)
            self.assertIsNotNone(run.current_analysis_revision_id)
            revision = run.current_analysis_revision
            self.assertFalse(revision.finalized)
            self.assertIsNone(revision.completed)
            self.assertEqual(len(revision.artifacts), 1)
            state_artifact = revision.artifacts[0]
            state_bytes = self.storage.objects[state_artifact.storage_key][0]
            state = json.loads(state_bytes)
            self.assertEqual([item["block_index"] for item in state["sources"]], [1, 2])
            self.assertEqual(state["sources"][0]["words"][0]["written_word"], "אב")
            self.assertEqual(
                state["sources"][0]["words"][0]["letters"],
                [{"char": "א", "stroke_ids": [2]}],
            )
            self.assertEqual(state["sources"][0]["words"][0]["assigned_letters"], {})
            with tempfile.TemporaryDirectory() as temporary:
                state_path = Path(temporary) / "state.json"
                state_path.write_bytes(state_bytes)
                validate_analysis_state(state_path, run)

        object_count = len(self.storage.objects)
        with self.sessions() as database:
            retried = seed_historical_edit_states(
                database, self.storage, experiment_name="pilot", apply=True
            )
            self.assertEqual(retried["new_edit_states"], 0)
            self.assertEqual(retried["unchanged_edit_states"], 1)
            self.assertEqual(len(self.storage.objects), object_count)

    def test_timestamp_mismatch_is_rejected_before_any_write(self):
        original = json.loads(self.storage.objects[self.trainable_key][0])
        original[0]["timestamp"] = "not-a-result"
        raw = json.dumps(original, ensure_ascii=False).encode("utf-8")
        self.storage.objects[self.trainable_key] = (raw, "application/json")
        with self.sessions() as database:
            artifact = database.scalar(
                select(RunArtifact).where(RunArtifact.storage_key == self.trainable_key)
            )
            artifact.sha256 = hashlib.sha256(raw).hexdigest()
            artifact.size_bytes = len(raw)
            database.commit()
        with self.sessions() as database:
            with self.assertRaisesRegex(HistoricalEditStateError, "No Trainable JSON Block"):
                seed_historical_edit_states(
                    database, self.storage, experiment_name="pilot", apply=True
                )
            self.assertIsNone(self._load_run(database).current_analysis_revision_id)
        self.assertEqual(set(self.storage.objects), {self.trainable_key})

    def test_failed_state_upload_is_cleaned_and_database_rolls_back(self):
        self.storage.fail_state_upload = True
        with self.sessions() as database:
            with self.assertRaisesRegex(RuntimeError, "injected storage failure"):
                seed_historical_edit_states(
                    database, self.storage, experiment_name="pilot", apply=True
                )
            self.assertIsNone(self._load_run(database).current_analysis_revision_id)
        self.assertEqual(set(self.storage.objects), {self.trainable_key})


if __name__ == "__main__":
    unittest.main()
