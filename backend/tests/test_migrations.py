import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, create_engine, inspect, select

from app.config import get_settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]


class MigrationTests(unittest.TestCase):
    def test_migrations_create_current_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "migration.sqlite"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"

            with patch.dict(
                os.environ,
                {"AUTOSCRIPT_DATABASE_URL": database_url},
            ):
                get_settings.cache_clear()
                config = Config(str(BACKEND_ROOT / "alembic.ini"))
                config.set_main_option(
                    "script_location",
                    str(BACKEND_ROOT / "migrations"),
                )
                command.upgrade(config, "head")
                command.check(config)

            get_settings.cache_clear()
            engine = create_engine(database_url)
            try:
                table_names = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

            self.assertEqual(
                table_names,
                {
                    "alembic_version",
                    "access_tokens",
                    "users",
                    "experiments",
                    "experiment_blocks",
                    "experiment_runs",
                    "experiment_versions",
                    "experiment_revisions",
                    "experiment_revision_blocks",
                    "run_artifacts",
                    "run_results",
                },
            )

    def test_block_migration_backfills_only_latest_legacy_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "migration.sqlite"
            database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
            engine = create_engine(database_url)
            with patch.dict(os.environ, {"AUTOSCRIPT_DATABASE_URL": database_url}):
                get_settings.cache_clear()
                config = Config(str(BACKEND_ROOT / "alembic.ini"))
                config.set_main_option(
                    "script_location",
                    str(BACKEND_ROOT / "migrations"),
                )
                command.upgrade(config, "20260807_0001")
            metadata = MetaData()
            metadata.reflect(engine)
            user_id = uuid.uuid4()
            experiment_id = uuid.uuid4()
            created_at = datetime.now(timezone.utc)
            with engine.begin() as connection:
                connection.execute(
                    metadata.tables["users"].insert().values(
                        id=user_id.hex,
                        username="migration-owner",
                        password_hash="!test!",
                        role="admin",
                        is_active=True,
                        created_at=created_at,
                    )
                )
                connection.execute(
                    metadata.tables["experiments"].insert().values(
                        id=experiment_id.hex,
                        name="Legacy Study",
                        owner_id=user_id.hex,
                        created_at=created_at,
                    )
                )
                for version_number in (1, 2):
                    connection.execute(
                        metadata.tables["experiment_versions"].insert().values(
                            id=uuid.uuid4().hex,
                            experiment_id=experiment_id.hex,
                            version_number=version_number,
                            storage_key=f"legacy/{version_number}.zip",
                            original_filename=f"legacy-{version_number}.zip",
                            sha256=str(version_number) * 64,
                            size_bytes=version_number * 100,
                            created_by=user_id.hex,
                            created_at=created_at,
                        )
                    )

            with patch.dict(os.environ, {"AUTOSCRIPT_DATABASE_URL": database_url}):
                get_settings.cache_clear()
                command.upgrade(config, "head")
            get_settings.cache_clear()
            current_metadata = MetaData()
            current_metadata.reflect(engine)
            with engine.connect() as connection:
                rows = connection.execute(
                    select(current_metadata.tables["experiment_blocks"])
                ).mappings().all()
            engine.dispose()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Legacy Study")
            self.assertEqual(rows[0]["position"], 0)
            self.assertFalse(rows[0]["same_page_as_previous"])
            self.assertEqual(rows[0]["storage_key"], "legacy/2.zip")
            self.assertEqual(rows[0]["sha256"], "2" * 64)


if __name__ == "__main__":
    unittest.main()
