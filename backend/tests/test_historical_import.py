import hashlib
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Experiment,
    ExperimentBlock,
    ExperimentRevision,
    ExperimentRevisionBlock,
    ExperimentRun,
    User,
)
from app.services.historical_import import (
    HistoricalImportError,
    import_historical_archive,
)


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def put_file(self, object_name, file_path, content_type="application/octet-stream"):
        self.objects[object_name] = (file_path.read_bytes(), content_type)

    def remove_object(self, object_name):
        self.objects.pop(object_name, None)


def _entry(path, data, **metadata):
    return {
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        **metadata,
    }


def build_archive(path, *, second_raw=b'{"block":2}'):
    raw_one = b'{"block":1}'
    csv_data = b"Participant,Word\n7,test\n"
    trainable = b'[{"participant_number":7,"words":[]}]'
    screenshots = b"PK\x05\x06" + b"\x00" * 18
    files = {
        "runs/7/raw/0001-one.json": raw_one,
        "runs/7/raw/0002-two.json": second_raw,
        "runs/7/artifacts/analysis_csv.csv": csv_data,
        "runs/7/artifacts/trainable_json.json": trainable,
        "runs/7/artifacts/screenshots_zip.zip": screenshots,
    }
    run = {
        "session_id": "historical-pilot-p7-20260801",
        "participant_number": 7,
        "participant_age": 25,
        "participant_gender": "Other",
        "started_at": "2026-08-01T09:00:00Z",
        "finalized_at": "2026-08-01T09:05:00Z",
        "raw_results": [
            _entry(
                "runs/7/raw/0001-one.json",
                raw_one,
                original_filename="one.json",
                source_archive="source.zip",
                source_block_name="legacy-one",
                block_index=1,
                schema_version="1.0",
                app_version="legacy",
                result_timestamp="20260801_120000",
                completed_word_count=1,
                expected_word_count=1,
            ),
            _entry(
                "runs/7/raw/0002-two.json",
                second_raw,
                original_filename="two.json",
                source_archive="source.zip",
                source_block_name="legacy-two",
                block_index=2,
                schema_version="1.0",
                app_version="legacy",
                result_timestamp="20260801_120500",
                completed_word_count=2,
                expected_word_count=2,
            ),
        ],
        "artifacts": [
            _entry(
                "runs/7/artifacts/analysis_csv.csv",
                csv_data,
                kind="analysis_csv",
                original_filename="analysis.csv",
            ),
            _entry(
                "runs/7/artifacts/trainable_json.json",
                trainable,
                kind="trainable_json",
                original_filename="trainable.json",
            ),
            _entry(
                "runs/7/artifacts/screenshots_zip.zip",
                screenshots,
                kind="screenshots_zip",
                original_filename="screenshots.zip",
                file_count=1,
            ),
        ],
    }
    manifest = {
        "format_version": "1.0",
        "experiment_name": "pilot",
        "source_id": "test-drive-folder",
        "expected_block_word_counts": [1, 2],
        "run_count": 1,
        "runs": [run],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            archive.writestr(name, data)


class HistoricalImportTests(unittest.TestCase):
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
            database.add_all([actor, experiment])
            database.flush()
            blocks = []
            revision_blocks = []
            for index in range(2):
                block_id = uuid.uuid4()
                blocks.append(
                    ExperimentBlock(
                        id=block_id,
                        experiment_id=experiment.id,
                        position=index,
                        same_page_as_previous=False,
                        name=f"Block {index + 1}",
                        expected_word_count=0,
                        grid_rows=1,
                        grid_cols=2,
                        storage_key=f"live/{index}.zip",
                        original_filename=f"block-{index}.zip",
                        sha256=str(index + 1) * 64,
                        size_bytes=10,
                        created_by=actor.id,
                    )
                )
            revision = ExperimentRevision(
                id=uuid.uuid4(),
                experiment_id=experiment.id,
                revision_number=1,
                name="pilot",
                created_by=actor.id,
            )
            for index, block in enumerate(blocks):
                revision_blocks.append(
                    ExperimentRevisionBlock(
                        id=uuid.uuid4(),
                        revision_id=revision.id,
                        source_block_id=block.id,
                        position=index,
                        same_page_as_previous=False,
                        name=block.name,
                        expected_word_count=0,
                        grid_rows=1,
                        grid_cols=2,
                        storage_key=f"revision/{index}.zip",
                        original_filename=f"block-{index}.zip",
                        sha256=str(index + 3) * 64,
                        size_bytes=10,
                    )
                )
            database.add_all([*blocks, revision, *revision_blocks])
            database.flush()
            experiment.current_revision_id = revision.id
            database.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_dry_run_apply_and_exact_retry_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "import.zip"
            build_archive(archive)
            with self.sessions() as database:
                dry_run = import_historical_archive(
                    database, self.storage, archive, apply=False
                )
                self.assertEqual(dry_run["new_runs"], 1)
                self.assertFalse(dry_run["applied"])
                self.assertEqual(database.scalar(select(ExperimentRun)), None)
                self.assertEqual(self.storage.objects, {})

            with self.sessions() as database:
                applied = import_historical_archive(
                    database, self.storage, archive, apply=True
                )
                self.assertEqual(applied["new_runs"], 1)
                run = database.scalar(select(ExperimentRun))
                self.assertEqual(len(run.results), 2)
                self.assertEqual(len(run.artifacts), 3)
                self.assertTrue(run.analysis_completed)
                self.assertEqual(
                    {artifact.kind for artifact in run.artifacts},
                    {"analysis_csv", "trainable_json", "screenshots_zip"},
                )
                self.assertEqual(len(self.storage.objects), 5)
                self.assertEqual(
                    [block.expected_word_count for block in run.revision.blocks],
                    [1, 2],
                )

            with self.sessions() as database:
                retried = import_historical_archive(
                    database, self.storage, archive, apply=True
                )
                self.assertEqual(retried["new_runs"], 0)
                self.assertEqual(retried["unchanged_runs"], 1)
                self.assertEqual(len(self.storage.objects), 5)

    def test_existing_session_with_different_bytes_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.zip"
            conflicting = Path(temporary) / "conflicting.zip"
            build_archive(first)
            build_archive(conflicting, second_raw=b'{"block":2,"changed":true}')
            with self.sessions() as database:
                import_historical_archive(database, self.storage, first, apply=True)
            with self.sessions() as database:
                with self.assertRaisesRegex(HistoricalImportError, "different content"):
                    import_historical_archive(
                        database, self.storage, conflicting, apply=True
                    )
            self.assertEqual(len(self.storage.objects), 5)


if __name__ == "__main__":
    unittest.main()
