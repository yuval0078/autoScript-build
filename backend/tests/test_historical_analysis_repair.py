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
    repair_historical_analysis_sessions,
    seed_historical_edit_states,
)

try:
    from .test_result_api import raw_result, word_record
except ImportError:  # pragma: no cover
    from test_result_api import raw_result, word_record


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        self.objects[object_name] = (Path(file_path).read_bytes(), content_type)

    def iter_object(self, object_name, chunk_size=1024 * 1024):
        data = self.objects[object_name][0]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    def remove_object(self, object_name):
        self.objects.pop(object_name, None)


class HistoricalAnalysisRepairTests(unittest.TestCase):
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
                session_id="historical-pilot-p7-20260801",
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
            timestamps = ["20260801_120000", "20260801_120500"]
            results = []
            for index, timestamp in enumerate(timestamps, start=1):
                payload = raw_result(
                    str(experiment.id), block_index=index, block_count=2
                )
                payload.update(
                    {
                        "experiment_name": "pilot",
                        "participant_number": 7,
                        "session_id": f"7_20260801_12{index:02d}00_abcde{index}",
                        "timestamp": timestamp,
                        "completed_word_count": 1,
                        "expected_word_count": 1,
                        "words": [word_record(f"word-{index}")],
                    }
                )
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                key = f"legacy/raw-{index}.json"
                self.storage.objects[key] = (raw, "application/json")
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
                        completed_word_count=1,
                        expected_word_count=1,
                        schema_version="1.2",
                        app_version="1.0.3.1",
                        result_timestamp=timestamp,
                        storage_key=key,
                        original_filename=f"raw-{index}.json",
                        sha256=hashlib.sha256(raw).hexdigest(),
                        size_bytes=len(raw),
                        created_by=actor.id,
                    )
                )
            trainable_payload = [
                {
                    "participant_number": 7,
                    "timestamp": timestamp,
                    "words": [
                        {
                            "letters": [{"char": str(index), "stroke_ids": [0]}],
                            "written_word": f"edited-{index}",
                            "trainability": "trainable",
                        }
                    ],
                }
                for index, timestamp in enumerate(timestamps, start=1)
            ]
            trainable_raw = json.dumps(
                trainable_payload, ensure_ascii=False
            ).encode("utf-8")
            csv_raw = b"Participant,Word\n7,test\n"
            artifacts = []
            for kind, key, raw in (
                ("trainable_json", "legacy/trainable.json", trainable_raw),
                ("analysis_csv", "legacy/analysis.csv", csv_raw),
            ):
                self.storage.objects[key] = (raw, "application/json")
                artifacts.append(
                    RunArtifact(
                        id=uuid.uuid4(),
                        run_id=run.id,
                        kind=kind,
                        analysis_revision_id=None,
                        storage_key=key,
                        original_filename=Path(key).name,
                        sha256=hashlib.sha256(raw).hexdigest(),
                        size_bytes=len(raw),
                        created_by=actor.id,
                    )
                )
            database.add_all([*results, *artifacts])
            database.commit()
            self.run_id = run.id
            self.old_raw_keys = {result.storage_key for result in results}

        # Reproduce the first import: its state used the synthetic Run session,
        # while the immutable raw Blocks still had three/independent sessions.
        with self.sessions() as database:
            seed_historical_edit_states(
                database,
                self.storage,
                experiment_name="pilot",
                apply=True,
            )

    def tearDown(self):
        self.engine.dispose()

    def _run(self, database):
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

    def test_targeted_repair_normalizes_raw_files_and_current_state(self):
        with self.sessions() as database:
            dry_run = repair_historical_analysis_sessions(
                database, self.storage, experiment_name="pilot", apply=False
            )
            self.assertEqual(dry_run["eligible_runs"], 1)
            self.assertEqual(dry_run["changed_raw_results"], 2)
            self.assertEqual(dry_run["restored_words"], 2)
            before = self._run(database)
            self.assertTrue(before.session_id.startswith("historical-"))

        with self.sessions() as database:
            applied = repair_historical_analysis_sessions(
                database, self.storage, experiment_name="pilot", apply=True
            )
            self.assertEqual(applied["new_sessions"], 1)
            run = self._run(database)
            self.assertRegex(run.session_id, r"^7_20260801_120000_[0-9a-f]{6}$")
            self.assertTrue(run.analysis_completed)
            raw_sessions = set()
            for result in run.results:
                raw = self.storage.objects[result.storage_key][0]
                self.assertEqual(hashlib.sha256(raw).hexdigest(), result.sha256)
                payload = json.loads(raw)
                raw_sessions.add(payload["session_id"])
                self.assertEqual(payload["server_run_id"], str(run.id))
            self.assertEqual(raw_sessions, {run.session_id})
            self.assertTrue(self.old_raw_keys.isdisjoint(self.storage.objects))

            revision = run.current_analysis_revision
            self.assertEqual(revision.revision_number, 2)
            state_artifact = next(
                item for item in revision.artifacts if item.kind == "analysis_state"
            )
            state_raw = self.storage.objects[state_artifact.storage_key][0]
            state = json.loads(state_raw)
            self.assertEqual(state["session_id"], run.session_id)
            self.assertEqual(state["sources"][0]["words"][0]["written_word"], "edited-1")
            with tempfile.TemporaryDirectory() as temporary:
                state_path = Path(temporary) / "state.json"
                state_path.write_bytes(state_raw)
                validate_analysis_state(state_path, run)

        object_count = len(self.storage.objects)
        with self.sessions() as database:
            retried = repair_historical_analysis_sessions(
                database, self.storage, experiment_name="pilot", apply=True
            )
            self.assertEqual(retried["new_sessions"], 0)
            self.assertEqual(retried["unchanged_runs"], 1)
            self.assertEqual(len(self.storage.objects), object_count)


if __name__ == "__main__":
    unittest.main()
