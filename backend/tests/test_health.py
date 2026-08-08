import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.services.storage import get_object_storage


class FakeStorage:
    def __init__(self, exists=True):
        self.exists = exists

    def bucket_exists(self):
        return self.exists


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.session_factory = sessionmaker(bind=self.engine)
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

    def test_readiness_checks_database_and_object_storage(self):
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(
            response.json()["components"],
            {"database": "ready", "object_storage": "ready"},
        )

    def test_readiness_reports_missing_bucket(self):
        self.app.dependency_overrides[get_object_storage] = lambda: FakeStorage(False)
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(
            response.json()["components"]["object_storage"],
            "bucket_missing",
        )


if __name__ == "__main__":
    unittest.main()
