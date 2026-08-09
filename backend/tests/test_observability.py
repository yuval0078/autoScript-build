import json
import unittest

from fastapi.testclient import TestClient

from app.main import create_app


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()

        @self.app.get("/_test/unhandled")
        def unhandled():
            raise RuntimeError("sensitive internal message")

        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()

    def test_request_and_correlation_ids_are_returned_and_logged(self):
        with self.assertLogs("autoscript.access", level="INFO") as logs:
            response = self.client.get(
                "/health/live",
                headers={
                    "X-Request-ID": "request-123",
                    "X-Correlation-ID": "correlation-456",
                    "Authorization": "Bearer must-never-be-logged",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "request-123")
        self.assertEqual(response.headers["X-Correlation-ID"], "correlation-456")
        payload = json.loads(logs.records[-1].getMessage())
        self.assertEqual(payload["request_id"], "request-123")
        self.assertEqual(payload["correlation_id"], "correlation-456")
        self.assertEqual(payload["path"], "/health/live")
        self.assertNotIn("must-never-be-logged", logs.output[0])

    def test_invalid_request_id_is_replaced(self):
        response = self.client.get(
            "/health/live", headers={"X-Request-ID": "bad id with spaces"}
        )
        self.assertNotEqual(response.headers["X-Request-ID"], "bad id with spaces")
        self.assertRegex(
            response.headers["X-Request-ID"],
            r"^[0-9a-f]{8}-[0-9a-f-]{27}$",
        )

    def test_unhandled_error_is_generic_correlated_and_secret_safe(self):
        with self.assertLogs("autoscript.error", level="ERROR") as logs:
            response = self.client.get(
                "/_test/unhandled",
                headers={"Authorization": "Bearer hidden-token"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["request_id"], response.headers["X-Request-ID"])
        combined = "\n".join(logs.output)
        self.assertIn('"exception_type":"RuntimeError"', combined)
        self.assertNotIn("sensitive internal message", combined)
        self.assertNotIn("hidden-token", combined)


if __name__ == "__main__":
    unittest.main()
