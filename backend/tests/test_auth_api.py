import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import get_db
from app.main import create_app
from app.models import Base, User
from app.services.auth import hash_password


class AuthenticationApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"AUTOSCRIPT_AUTH_MODE": "token"})
        self.environment.start()
        get_settings.cache_clear()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as database:
            database.add_all([
                User(username="admin", password_hash=hash_password("admin-password"), role="admin", is_active=True),
                User(username="researcher", password_hash=hash_password("research-password"), role="researcher", is_active=True),
            ])
            database.commit()
        self.app = create_app()

        def override_database():
            with self.sessions() as database:
                yield database

        self.app.dependency_overrides[get_db] = override_database
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.environment.stop()
        get_settings.cache_clear()

    def login(self, username, password):
        response = self.client.post("/api/v1/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    @staticmethod
    def headers(token):
        return {"Authorization": f"Bearer {token}"}

    def test_token_required_login_logout_and_me(self):
        self.assertEqual(self.client.get("/api/v1/experiments").status_code, 401)
        token = self.login("researcher", "research-password")
        me = self.client.get("/api/v1/auth/me", headers=self.headers(token))
        self.assertEqual(me.status_code, 200, me.text)
        self.assertEqual(me.json()["username"], "researcher")
        logged_out = self.client.post("/api/v1/auth/logout", headers=self.headers(token))
        self.assertEqual(logged_out.status_code, 204, logged_out.text)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=self.headers(token)).status_code, 401)

    def test_admin_user_management_and_owner_isolation(self):
        admin = self.login("admin", "admin-password")
        researcher = self.login("researcher", "research-password")
        created = self.client.post(
            "/api/v1/users", headers=self.headers(admin),
            json={"username": "second", "password": "second-password", "role": "researcher"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(
            self.client.post(
                "/api/v1/users", headers=self.headers(researcher),
                json={"username": "forbidden", "password": "forbidden-pass", "role": "researcher"},
            ).status_code,
            403,
        )
        experiment = self.client.post(
            "/api/v1/experiments", headers=self.headers(researcher), json={"name": "Private"}
        )
        self.assertEqual(experiment.status_code, 201, experiment.text)
        second = self.login("second", "second-password")
        self.assertEqual(self.client.get("/api/v1/experiments", headers=self.headers(second)).json(), [])
