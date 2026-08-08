"""Small standard-library client for the local AutoScript API."""

import hashlib
import http.client
import json
import os
import socket
from pathlib import Path
from urllib.parse import quote, urlsplit


_SESSION_TOKEN = None


def set_session_token(token):
    global _SESSION_TOKEN
    _SESSION_TOKEN = token


def get_session_token():
    return _SESSION_TOKEN or os.environ.get("AUTOSCRIPT_API_TOKEN")


class APIError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class AutoScriptAPI:
    def __init__(self, base_url=None, timeout=60, token=None):
        self.base_url = (base_url or os.environ.get(
            "AUTOSCRIPT_API_URL", "http://127.0.0.1:8000"
        )).rstrip("/")
        self.timeout = timeout
        self.token = token or os.environ.get("AUTOSCRIPT_API_TOKEN") or _SESSION_TOKEN
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AUTOSCRIPT_API_URL must be an HTTP or HTTPS URL.")
        self._parsed = parsed

    def _connection(self):
        connection_class = (
            http.client.HTTPSConnection
            if self._parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_class(
            self._parsed.hostname,
            self._parsed.port,
            timeout=self.timeout,
        )

    def _path(self, path):
        prefix = self._parsed.path.rstrip("/")
        return f"{prefix}/{path.lstrip('/')}"

    @staticmethod
    def _error_from_response(response, body):
        message = f"AutoScript API returned HTTP {response.status}."
        try:
            payload = json.loads(body.decode("utf-8"))
            detail = payload.get("detail") if isinstance(payload, dict) else None
            if detail:
                message = str(detail)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return APIError(message, response.status)

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _json_request(self, method, path, payload=None, auth=True):
        body = None
        headers = {"Accept": "application/json"}
        if auth:
            headers.update(self._auth_headers())
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        connection = self._connection()
        try:
            connection.request(method, self._path(path), body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            if not response_body:
                return None
            return json.loads(response_body.decode("utf-8"))
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(f"Could not connect to the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    def login(self, username, password):
        response = self._json_request(
            "POST", "/api/v1/auth/login",
            {"username": username, "password": password}, auth=False,
        )
        self.token = response["access_token"]
        set_session_token(self.token)
        return response

    def me(self):
        return self._json_request("GET", "/api/v1/auth/me")

    def logout(self):
        result = self._json_request("POST", "/api/v1/auth/logout")
        self.token = None
        set_session_token(None)
        return result

    def create_run(self, revision_id, session_id, participant_number, participant_age, participant_gender):
        return self._json_request(
            "POST", f"/api/v1/experiment-revisions/{revision_id}/runs",
            {
                "session_id": session_id,
                "participant_number": int(participant_number),
                "participant_age": int(participant_age),
                "participant_gender": participant_gender,
            },
        )

    def start_run(self, run_id):
        return self._json_request("POST", f"/api/v1/runs/{run_id}/start")

    def finalize_run(self, run_id):
        return self._json_request("POST", f"/api/v1/runs/{run_id}/finalize")

    def cancel_run(self, run_id):
        return self._json_request("POST", f"/api/v1/runs/{run_id}/cancel")

    def fail_run(self, run_id):
        return self._json_request("POST", f"/api/v1/runs/{run_id}/fail")

    def list_experiments(self):
        return self._json_request("GET", "/api/v1/experiments")

    def list_experiment_runs(self, experiment_id):
        return self._json_request(
            "GET", f"/api/v1/experiments/{experiment_id}/runs"
        )

    def update_run_analysis(self, run_id, completed):
        return self._json_request(
            "PATCH",
            f"/api/v1/runs/{run_id}/analysis",
            {"completed": bool(completed)},
        )

    def delete_run(self, run_id):
        return self._json_request("DELETE", f"/api/v1/runs/{run_id}")

    def create_experiment(self, name, description=None):
        payload = {"name": name}
        if description:
            payload["description"] = description
        return self._json_request("POST", "/api/v1/experiments", payload)

    def update_experiment(self, experiment_id, name):
        return self._json_request(
            "PATCH",
            f"/api/v1/experiments/{experiment_id}",
            {"name": name},
        )

    def delete_experiment(self, experiment_id):
        return self._json_request("DELETE", f"/api/v1/experiments/{experiment_id}")

    def duplicate_experiment(self, experiment_id):
        return self._json_request(
            "POST",
            f"/api/v1/experiments/{experiment_id}/duplicate",
        )

    def create_experiment_revision(self, experiment_id):
        return self._json_request(
            "POST", f"/api/v1/experiments/{experiment_id}/revisions"
        )

    def upload_block(self, experiment_id, package_path, block_name, position=0, progress=None):
        package_path = Path(package_path)
        total = package_path.stat().st_size
        connection = self._connection()
        try:
            connection.putrequest(
                "POST",
                self._path(f"/api/v1/experiments/{experiment_id}/blocks"),
            )
            connection.putheader("Accept", "application/json")
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", "application/zip")
            connection.putheader("Content-Length", str(total))
            connection.putheader("X-Filename", quote(package_path.name, safe=""))
            connection.putheader("X-Block-Name", quote(block_name, safe=""))
            connection.putheader("X-Position", str(position))
            connection.endheaders()

            sent = 0
            with package_path.open("rb") as package:
                while True:
                    chunk = package.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)

            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            return json.loads(response_body.decode("utf-8"))
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(f"Could not upload Block to the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    def upload_result(self, experiment_id, result_path, progress=None):
        """Upload one exact Runner JSON artifact; retries are server-idempotent."""
        result_path = Path(result_path)
        total = result_path.stat().st_size
        connection = self._connection()
        try:
            connection.putrequest(
                "POST",
                self._path(f"/api/v1/experiments/{experiment_id}/results"),
            )
            connection.putheader("Accept", "application/json")
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", "application/json; charset=utf-8")
            connection.putheader("Content-Length", str(total))
            connection.putheader("X-Filename", quote(result_path.name, safe=""))
            connection.endheaders()

            sent = 0
            with result_path.open("rb") as result_file:
                while True:
                    chunk = result_file.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)

            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            return json.loads(response_body.decode("utf-8"))
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(
                f"Could not upload result to the AutoScript API: {exc}"
            ) from exc
        finally:
            connection.close()

    def upload_run_result(self, run_id, result_path, progress=None):
        result_path = Path(result_path)
        total = result_path.stat().st_size
        connection = self._connection()
        try:
            connection.putrequest("POST", self._path(f"/api/v1/runs/{run_id}/results"))
            connection.putheader("Accept", "application/json")
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", "application/json; charset=utf-8")
            connection.putheader("Content-Length", str(total))
            connection.putheader("X-Filename", quote(result_path.name, safe=""))
            connection.endheaders()
            sent = 0
            with result_path.open("rb") as result_file:
                while True:
                    chunk = result_file.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)
            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            return json.loads(response_body.decode("utf-8"))
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(f"Could not upload Run result to the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    def upload_run_artifact(self, run_id, kind, artifact_path, progress=None):
        artifact_path = Path(artifact_path)
        total = artifact_path.stat().st_size
        content_type = (
            "text/csv; charset=utf-8"
            if kind == "analysis_csv"
            else "application/json; charset=utf-8"
        )
        connection = self._connection()
        try:
            connection.putrequest(
                "POST",
                self._path(f"/api/v1/runs/{run_id}/artifacts/{kind}"),
            )
            connection.putheader("Accept", "application/json")
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(total))
            connection.putheader("X-Filename", quote(artifact_path.name, safe=""))
            connection.endheaders()

            sent = 0
            with artifact_path.open("rb") as artifact_file:
                while True:
                    chunk = artifact_file.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)

            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            return json.loads(response_body.decode("utf-8"))
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(
                f"Could not upload Analyzer artifact to the AutoScript API: {exc}"
            ) from exc
        finally:
            connection.close()

    def reorder_blocks(
        self,
        experiment_id,
        block_ids,
        same_page_block_ids=None,
    ):
        payload = {
            "block_ids": block_ids,
            "same_page_block_ids": list(same_page_block_ids or []),
        }
        return self._json_request(
            "PUT",
            f"/api/v1/experiments/{experiment_id}/blocks/order",
            payload,
        )

    def delete_block(self, block_id):
        return self._json_request("DELETE", f"/api/v1/blocks/{block_id}")

    def publish_package(self, experiment_id, package_path, progress=None):
        package_path = Path(package_path)
        total = package_path.stat().st_size
        connection = self._connection()
        try:
            connection.putrequest(
                "POST",
                self._path(f"/api/v1/experiments/{experiment_id}/versions"),
            )
            connection.putheader("Accept", "application/json")
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", "application/zip")
            connection.putheader("Content-Length", str(total))
            connection.putheader("X-Filename", quote(package_path.name, safe=""))
            connection.endheaders()

            sent = 0
            with package_path.open("rb") as package:
                while True:
                    chunk = package.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)

            response = connection.getresponse()
            response_body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            return json.loads(response_body.decode("utf-8"))
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(f"Could not publish to the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    def download_version(self, version, destination, progress=None):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        expected_sha256 = version.get("sha256")
        connection = self._connection()
        try:
            connection.request(
                "GET", self._path(version["download_url"]),
                headers=self._auth_headers(),
            )
            response = connection.getresponse()
            if response.status >= 400:
                body = response.read()
                raise self._error_from_response(response, body)

            expected_sha256 = expected_sha256 or response.getheader(
                "X-Checksum-SHA256"
            )
            expected_size = int(response.getheader("Content-Length") or 0)
            digest = hashlib.sha256()
            received = 0
            with partial.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, expected_size)

            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise APIError("Downloaded artifact failed its SHA-256 check.")
            partial.replace(destination)
            return destination
        except APIError:
            if partial.exists():
                partial.unlink()
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            if partial.exists():
                partial.unlink()
            raise APIError(f"Could not download from the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    def download_block(self, block, destination, progress=None):
        resource = {
            "download_url": block.get("download_url")
            or f"/api/v1/blocks/{block['id']}/download",
            "sha256": block.get("sha256"),
        }
        return self.download_version(resource, destination, progress=progress)

    def download_experiment(self, experiment, destination, progress=None):
        resource = {
            "download_url": experiment.get("download_url")
            or f"/api/v1/experiments/{experiment['id']}/download",
            "sha256": experiment.get("sha256"),
        }
        return self.download_version(resource, destination, progress=progress)

    def download_run_result(self, result, destination, progress=None):
        resource = {
            "download_url": result.get("download_url")
            or f"/api/v1/run-results/{result['id']}/download",
            "sha256": result.get("sha256"),
        }
        return self.download_version(resource, destination, progress=progress)

    def download_run_artifact(self, artifact, destination, progress=None):
        resource = {
            "download_url": artifact.get("download_url")
            or f"/api/v1/run-artifacts/{artifact['id']}/download",
            "sha256": artifact.get("sha256"),
        }
        return self.download_version(resource, destination, progress=progress)
