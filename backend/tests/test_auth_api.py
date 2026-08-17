import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.database import get_db
from app.main import create_app
from app.models import (
    AccessToken,
    Base,
    Experiment,
    ExperimentRevision,
    ExperimentRevisionBlock,
    User,
)
from app.security_models import LoginRateLimit, SecurityEvent
from app.security_settings import get_security_settings
from app.services.auth import hash_password, hash_token


class AuthenticationApiTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "AUTOSCRIPT_AUTH_MODE": "token",
                "AUTOSCRIPT_LOGIN_RATE_LIMIT_ATTEMPTS": "3",
                "AUTOSCRIPT_LOGIN_RATE_LIMIT_ADDRESS_MULTIPLIER": "4",
                "AUTOSCRIPT_LOGIN_RATE_LIMIT_WINDOW_SECONDS": "60",
                "AUTOSCRIPT_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS": "120",
            },
        )
        self.environment.start()
        get_settings.cache_clear()
        get_security_settings.cache_clear()
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
        get_security_settings.cache_clear()

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
        with self.sessions() as database:
            self.assertEqual(database.query(AccessToken).count(), 0)
            logout_events = database.query(SecurityEvent).filter_by(
                event_type="logout"
            ).all()
        self.assertEqual(len(logout_events), 1)

    def test_admin_user_management_and_shared_lab_workspace(self):
        admin = self.login("admin", "admin-password")
        researcher = self.login("researcher", "research-password")
        created = self.client.post(
            "/api/v1/users", headers=self.headers(admin),
            json={"username": "second", "password": "second-password", "role": "researcher"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        renamed = self.client.patch(
            f"/api/v1/users/{created.json()['id']}",
            headers=self.headers(admin),
            json={"username": "second-renamed"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["username"], "second-renamed")
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
        second = self.login("second-renamed", "second-password")
        shared = self.client.get("/api/v1/experiments", headers=self.headers(second))
        self.assertEqual(shared.status_code, 200, shared.text)
        self.assertEqual([item["name"] for item in shared.json()], ["Private"])

        events = self.client.get(
            "/api/v1/security/events?event_type=user_created",
            headers=self.headers(admin),
        )
        self.assertEqual(events.status_code, 200, events.text)
        self.assertEqual(events.json()[0]["username"], "second")
        self.assertEqual(events.json()[0]["metadata"], {"role": "researcher"})
        self.assertEqual(
            self.client.get(
                "/api/v1/security/events", headers=self.headers(researcher)
            ).status_code,
            403,
        )

    def test_operator_can_run_and_analyze_but_cannot_author_or_delete(self):
        admin = self.login("admin", "admin-password")
        researcher = self.login("researcher", "research-password")
        created_operator = self.client.post(
            "/api/v1/users",
            headers=self.headers(admin),
            json={
                "username": "operator",
                "password": "operator-password",
                "role": "operator",
            },
        )
        self.assertEqual(created_operator.status_code, 201, created_operator.text)
        operator = self.login("operator", "operator-password")

        experiment_response = self.client.post(
            "/api/v1/experiments",
            headers=self.headers(researcher),
            json={"name": "Shared operator study"},
        )
        self.assertEqual(experiment_response.status_code, 201, experiment_response.text)
        experiment_id = uuid.UUID(experiment_response.json()["id"])
        revision_id = uuid.uuid4()
        with self.sessions() as database:
            researcher_user = database.query(User).filter_by(
                username="researcher"
            ).one()
            revision = ExperimentRevision(
                id=revision_id,
                experiment_id=experiment_id,
                revision_number=1,
                name="Shared operator study",
                created_by=researcher_user.id,
            )
            revision.blocks = [
                ExperimentRevisionBlock(
                    id=uuid.uuid4(),
                    source_block_id=None,
                    position=0,
                    same_page_as_previous=False,
                    name="Block one",
                    expected_word_count=1,
                    grid_rows=1,
                    grid_cols=1,
                    storage_key="revisions/operator-test/block.zip",
                    original_filename="block.zip",
                    sha256="a" * 64,
                    size_bytes=10,
                )
            ]
            database.add(revision)
            database.commit()

        operator_headers = self.headers(operator)
        shared = self.client.get("/api/v1/experiments", headers=operator_headers)
        self.assertEqual(shared.status_code, 200, shared.text)
        self.assertEqual([item["id"] for item in shared.json()], [str(experiment_id)])
        self.assertEqual(
            self.client.post(
                "/api/v1/experiments",
                headers=operator_headers,
                json={"name": "Forbidden authoring"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/v1/experiments/{experiment_id}",
                headers=operator_headers,
                json={"name": "Forbidden rename"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/experiments/{experiment_id}",
                headers=operator_headers,
            ).status_code,
            403,
        )

        run = self.client.post(
            f"/api/v1/experiment-revisions/{revision_id}/runs",
            headers=operator_headers,
            json={
                "session_id": "operator-session",
                "participant_number": 1,
                "participant_age": 25,
                "participant_gender": "Other",
            },
        )
        self.assertEqual(run.status_code, 201, run.text)
        run_id = run.json()["id"]
        started = self.client.post(
            f"/api/v1/runs/{run_id}/start", headers=operator_headers
        )
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["status"], "running")

        researcher_run = self.client.post(
            f"/api/v1/experiment-revisions/{revision_id}/runs",
            headers=self.headers(researcher),
            json={
                "session_id": "researcher-session",
                "participant_number": 2,
                "participant_age": 26,
                "participant_gender": "Other",
            },
        )
        self.assertEqual(researcher_run.status_code, 201, researcher_run.text)
        researcher_run_id = researcher_run.json()["id"]
        shared_detail = self.client.get(
            f"/api/v1/runs/{researcher_run_id}", headers=operator_headers
        )
        self.assertEqual(shared_detail.status_code, 200, shared_detail.text)
        self.assertEqual(
            self.client.post(
                f"/api/v1/runs/{researcher_run_id}/start",
                headers=operator_headers,
            ).status_code,
            404,
        )

        operator_runs = self.client.get(
            f"/api/v1/experiments/{experiment_id}/runs",
            headers=operator_headers,
        )
        self.assertEqual(operator_runs.status_code, 200, operator_runs.text)
        self.assertEqual(
            {item["id"] for item in operator_runs.json()},
            {run_id, researcher_run_id},
        )
        operator_run = self.client.get(
            f"/api/v1/runs/{run_id}", headers=operator_headers
        )
        self.assertEqual(operator_run.status_code, 200, operator_run.text)
        analysis_copies = self.client.get(
            f"/api/v1/runs/{run_id}/analysis-copies",
            headers=operator_headers,
        )
        self.assertEqual(analysis_copies.status_code, 200, analysis_copies.text)
        self.assertEqual(analysis_copies.json(), [])
        analysis_status = self.client.patch(
            f"/api/v1/runs/{run_id}/analysis",
            headers=operator_headers,
            json={"completed": False},
        )
        self.assertEqual(analysis_status.status_code, 200, analysis_status.text)
        bulk = self.client.post(
            f"/api/v1/experiments/{experiment_id}/bulk-export",
            headers=operator_headers,
            json={"run_ids": [run_id], "include": ["raw_data"]},
        )
        self.assertEqual(bulk.status_code, 200, bulk.text)
        self.assertEqual(
            self.client.delete(
                f"/api/v1/runs/{run_id}", headers=operator_headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/runs/{run_id}/analysis-copies/{uuid.uuid4()}",
                headers=operator_headers,
            ).status_code,
            403,
        )

        researcher_runs = self.client.get(
            f"/api/v1/experiments/{experiment_id}/runs",
            headers=self.headers(researcher),
        )
        self.assertEqual(researcher_runs.status_code, 200, researcher_runs.text)
        self.assertEqual(
            {item["id"] for item in researcher_runs.json()},
            {run_id, researcher_run_id},
        )

    def test_login_rate_limit_is_durable_and_audited(self):
        request_ids = []
        for attempt in range(3):
            request_id = f"failed-login-{attempt}"
            request_ids.append(request_id)
            response = self.client.post(
                "/api/v1/auth/login",
                headers={"X-Request-ID": request_id},
                json={"username": "researcher", "password": "wrong-password"},
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertEqual(response.headers["Retry-After"], "120")
        blocked = self.client.post(
            "/api/v1/auth/login",
            json={"username": "researcher", "password": "research-password"},
        )
        self.assertEqual(blocked.status_code, 429, blocked.text)

        with self.sessions() as database:
            buckets = database.query(LoginRateLimit).all()
            events = database.query(SecurityEvent).order_by(SecurityEvent.created_at).all()
        self.assertEqual({bucket.scope for bucket in buckets}, {"principal", "address"})
        self.assertIn("login_rate_limited", [event.event_type for event in events])
        self.assertEqual(events[2].request_id, request_ids[2])
        serialized = " ".join(event.metadata_json or "" for event in events)
        self.assertNotIn("wrong-password", serialized)
        self.assertNotIn("research-password", serialized)

    def test_login_cleans_expired_and_revoked_sessions(self):
        with self.sessions() as database:
            researcher = database.query(User).filter_by(username="researcher").one()
            active_id = uuid.uuid4()
            database.add_all(
                [
                    AccessToken(
                        id=uuid.uuid4(),
                        user_id=researcher.id,
                        token_hash=hash_token("expired-token"),
                        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                    ),
                    AccessToken(
                        id=uuid.uuid4(),
                        user_id=researcher.id,
                        token_hash=hash_token("revoked-token"),
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                        revoked_at=datetime.now(timezone.utc),
                    ),
                    AccessToken(
                        id=active_id,
                        user_id=researcher.id,
                        token_hash=hash_token("active-token"),
                        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    ),
                ]
            )
            database.commit()

        self.login("admin", "admin-password")
        with self.sessions() as database:
            tokens = database.query(AccessToken).all()
            cleanup = database.query(SecurityEvent).filter_by(
                event_type="security_state_cleanup"
            ).all()
        self.assertIn(active_id, {token.id for token in tokens})
        self.assertEqual(len(tokens), 2)  # active fixture plus the new admin session
        self.assertTrue(cleanup)
        self.assertIn('"access_tokens_removed":2', cleanup[-1].metadata_json)

    def test_protected_reads_do_not_run_session_cleanup_writes(self):
        token = self.login("researcher", "research-password")
        with self.sessions() as database:
            researcher = database.query(User).filter_by(username="researcher").one()
            expired_id = uuid.uuid4()
            database.add(
                AccessToken(
                    id=expired_id,
                    user_id=researcher.id,
                    token_hash=hash_token("unused-expired-token"),
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            database.commit()

        for _ in range(2):
            response = self.client.get("/api/v1/auth/me", headers=self.headers(token))
            self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as database:
            self.assertIsNotNone(database.get(AccessToken, expired_id))
