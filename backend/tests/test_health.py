import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.runtime_state import DATABASE_SCHEMA_REVISION
from app.services.storage import get_object_storage


class FakeStorage:
    def __init__(self, exists=True, error=False):
        self.exists = exists
        self.error = error

    def bucket_exists(self):
        if self.error:
            raise RuntimeError("storage unavailable")
        return self.exists


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.session_factory = sessionmaker(bind=self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO alembic_version (version_num) VALUES (?)",
                (DATABASE_SCHEMA_REVISION,),
            )
        self.app = create_app()

        def override_database():
            with self.session_factory() as session:
                yield session

        self.app.dependency_overrides[get_db] = override_database
        self.app.dependency_overrides[get_object_storage] = lambda: FakeStorage()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def test_liveness_does_not_require_dependencies(self):
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_readiness_checks_database_and_object_storage(self):
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(
            response.json()["components"],
            {
                "database": "ready",
                "schema": "ready",
                "object_storage": "ready",
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_readiness_reports_missing_bucket(self):
        self.app.dependency_overrides[get_object_storage] = lambda: FakeStorage(False)
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(
            response.json()["components"]["object_storage"],
            "bucket_missing",
        )

    def test_readiness_reports_schema_drift(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = 'old-revision'"
            )
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["components"]["database"], "ready")
        self.assertEqual(
            response.json()["components"]["schema"], "migration_required"
        )

    def test_readiness_hides_storage_errors(self):
        self.app.dependency_overrides[get_object_storage] = lambda: FakeStorage(
            error=True
        )
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["components"]["object_storage"], "unavailable"
        )
        self.assertNotIn("storage unavailable", response.text)


if __name__ == "__main__":
    unittest.main()
