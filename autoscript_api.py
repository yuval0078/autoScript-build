"""Small standard-library client for the AutoScript API."""

import hashlib
import http.client
import json
import os
import socket
import ssl
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import certifi


_SESSION_TOKEN = None
DEFAULT_API_URL = "https://api.autoscript-lab.org"
_TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def set_session_token(token):
    global _SESSION_TOKEN
    _SESSION_TOKEN = token


def get_session_token():
    return _SESSION_TOKEN or os.environ.get("AUTOSCRIPT_API_TOKEN")


class APIError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class APICancelled(APIError):
    """Raised when a user cancels a streaming API transfer."""


class PaginatedList(list):
    """List-compatible API page with pagination response metadata attached."""

    def __init__(self, values=(), *, headers=None):
        super().__init__(values)
        self.response_headers = dict(headers or {})
        self.next_cursor = self.response_headers.get("x-next-cursor") or None
        total = self.response_headers.get("x-total-count")
        try:
            self.total_count = int(total) if total is not None else len(self)
        except (TypeError, ValueError):
            self.total_count = len(self)

    @property
    def has_more(self):
        return bool(self.next_cursor)


class AutoScriptAPI:
    def __init__(self, base_url=None, timeout=60, token=None):
        self.base_url = (base_url or os.environ.get(
            "AUTOSCRIPT_API_URL", DEFAULT_API_URL
        )).rstrip("/")
        self.timeout = timeout
        self.token = token or os.environ.get("AUTOSCRIPT_API_TOKEN") or _SESSION_TOKEN
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("AUTOSCRIPT_API_URL must be an HTTP or HTTPS URL.")
        self._parsed = parsed

    def _connection(self, timeout=None):
        kwargs = {
            "timeout": self.timeout if timeout is None else timeout,
        }
        if self._parsed.scheme == "https":
            kwargs["context"] = _TLS_CONTEXT
            connection_class = http.client.HTTPSConnection
        else:
            connection_class = http.client.HTTPConnection
        return connection_class(self._parsed.hostname, self._parsed.port, **kwargs)

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

    def _json_request(self, method, path, payload=None, auth=True, return_headers=False):
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
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            if response.status >= 400:
                raise self._error_from_response(response, response_body)
            if not response_body:
                decoded = None
            else:
                try:
                    decoded = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise APIError(
                        "AutoScript API returned an invalid JSON response.",
                        response.status,
                    ) from exc
            if return_headers:
                return decoded, response_headers
            return decoded
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(f"Could not connect to the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _query_path(path, **parameters):
        values = []
        for name, value in parameters.items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            values.append((name, value))
        return f"{path}?{urlencode(values)}" if values else path

    def _file_request(
        self,
        method,
        path,
        source_path,
        *,
        content_type,
        headers=None,
        progress=None,
    ):
        """Stream a file request and return decoded JSON plus response headers."""
        source_path = Path(source_path)
        total = source_path.stat().st_size
        request_headers = {
            "Accept": "application/json",
            "Content-Type": content_type,
            "Content-Length": str(total),
            **self._auth_headers(),
            **(headers or {}),
        }
        connection = self._connection()
        try:
            connection.putrequest(method, self._path(path))
            for name, value in request_headers.items():
                connection.putheader(name, str(value))
            connection.endheaders()
            sent = 0
            with source_path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, total)
            response = connection.getresponse()
            body = response.read()
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            if response.status >= 400:
                raise self._error_from_response(response, body)
            payload = json.loads(body.decode("utf-8")) if body else None
            return payload, response_headers
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise APIError(f"Could not connect to the AutoScript API: {exc}") from exc
        finally:
            connection.close()

    def get_run_analysis_state(self, run_id):
        """Return the current editable state together with its concurrency token."""
        connection = self._connection()
        try:
            connection.request(
                "GET",
                self._path(f"/api/v1/runs/{run_id}/analysis-state"),
                headers={"Accept": "application/json", **self._auth_headers()},
            )
            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                raise self._error_from_response(response, body)
            return {
                "state": json.loads(body.decode("utf-8-sig")),
                "etag": response.getheader("ETag"),
                "revision": int(response.getheader("X-Analysis-Revision") or 0),
                "sha256": response.getheader("X-Checksum-SHA256"),
                "source_fingerprint": response.getheader("X-Source-Fingerprint"),
            }
        except APIError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException, ValueError) as exc:
            raise APIError(f"Could not load Analyzer edit state: {exc}") from exc
        finally:
            connection.close()

    def put_run_analysis_state(
        self,
        run_id,
        state_path,
        *,
        base_etag=None,
        request_id=None,
        progress=None,
    ):
        headers = {
            "X-Idempotency-Key": str(request_id or uuid.uuid4()),
            "X-Filename": quote(Path(state_path).name, safe=""),
        }
        if base_etag:
            headers["If-Match"] = base_etag
        else:
            headers["If-None-Match"] = "*"
        payload, response_headers = self._file_request(
            "PUT",
            f"/api/v1/runs/{run_id}/analysis-state",
            state_path,
            content_type="application/json; charset=utf-8",
            headers=headers,
            progress=progress,
        )
        if isinstance(payload, dict):
            payload.setdefault("etag", response_headers.get("etag"))
            payload.setdefault(
                "revision", int(response_headers.get("x-analysis-revision") or 0)
            )
        return payload

    def finalize_run_analysis(
        self,
        run_id,
        bundle_path,
        *,
        base_etag=None,
        request_id=None,
        existing_policy="keep",
        progress=None,
    ):
        existing_policy = str(existing_policy or "keep").strip().lower()
        if existing_policy not in {"keep", "replace"}:
            raise ValueError("existing_policy must be 'keep' or 'replace'.")
        headers = {
            "X-Idempotency-Key": str(request_id or uuid.uuid4()),
            "X-Filename": quote(Path(bundle_path).name, safe=""),
            "X-Existing-Analysis-Policy": existing_policy,
        }
        if base_etag:
            headers["If-Match"] = base_etag
        else:
            headers["If-None-Match"] = "*"
        payload, response_headers = self._file_request(
            "POST",
            f"/api/v1/runs/{run_id}/analysis/finalize",
            bundle_path,
            content_type="application/zip",
            headers=headers,
            progress=progress,
        )
        if isinstance(payload, dict):
            payload.setdefault("etag", response_headers.get("etag"))
            payload.setdefault(
                "revision", int(response_headers.get("x-analysis-revision") or 0)
            )
        return payload

    def list_run_analysis_copies(self, run_id):
        return self._json_request(
            "GET", f"/api/v1/runs/{run_id}/analysis-copies"
        )

    def delete_run_analysis_copy(self, run_id, revision_id):
        return self._json_request(
            "DELETE",
            f"/api/v1/runs/{run_id}/analysis-copies/{revision_id}",
        )

    def set_run_analysis_copy_editable(self, run_id, revision_id):
        return self._json_request(
            "POST",
            f"/api/v1/runs/{run_id}/analysis-copies/{revision_id}/set-editable",
        )

    def resolve_run_results_by_sha(self, sha256_values):
        return self._json_request(
            "POST",
            "/api/v1/run-results/resolve",
            {"sha256": list(sha256_values)},
        )

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

    def list_experiments(
        self, *, search=None, include_versions=None, limit=None, cursor=None
    ):
        path = self._query_path(
            "/api/v1/experiments",
            search=search,
            include_versions=include_versions,
            limit=limit,
            cursor=cursor,
        )
        payload, headers = self._json_request(
            "GET", path, return_headers=True
        )
        return PaginatedList(payload or [], headers=headers)

    def get_experiment(self, experiment_id):
        return self._json_request("GET", f"/api/v1/experiments/{experiment_id}")

    def list_experiment_runs(
        self,
        experiment_id,
        *,
        participant_number=None,
        session_search=None,
        status=None,
        complete=None,
        has_raw_data=None,
        has_analyzed_csv=None,
        has_trainable_json=None,
        include_files=None,
        limit=None,
        cursor=None,
    ):
        path = self._query_path(
            f"/api/v1/experiments/{experiment_id}/runs",
            participant_number=participant_number,
            session_search=session_search,
            status=status,
            complete=complete,
            has_raw_data=has_raw_data,
            has_analyzed_csv=has_analyzed_csv,
            has_trainable_json=has_trainable_json,
            include_files=include_files,
            limit=limit,
            cursor=cursor,
        )
        payload, headers = self._json_request(
            "GET", path, return_headers=True
        )
        return PaginatedList(payload or [], headers=headers)

    def get_experiment_run(self, run_id):
        return self._json_request("GET", f"/api/v1/runs/{run_id}")

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

    def stage_block(
        self,
        package_path,
        block_name,
        request_id=None,
        progress=None,
    ):
        """Upload an immutable Block asset without changing a live Experiment."""
        package_path = Path(package_path)
        request_id = str(request_id or uuid.uuid4())
        total = package_path.stat().st_size
        connection = self._connection()
        try:
            connection.putrequest("POST", self._path("/api/v1/staged-blocks"))
            connection.putheader("Accept", "application/json")
            if self.token:
                connection.putheader("Authorization", f"Bearer {self.token}")
            connection.putheader("Content-Type", "application/zip")
            connection.putheader("Content-Length", str(total))
            connection.putheader("X-Filename", quote(package_path.name, safe=""))
            connection.putheader("X-Block-Name", quote(block_name, safe=""))
            connection.putheader("X-Idempotency-Key", request_id)
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
            raise APIError(
                f"Could not stage Block with the AutoScript API: {exc}"
            ) from exc
        finally:
            connection.close()

    def publish_experiment(
        self,
        name,
        blocks,
        request_id,
        *,
        experiment_id=None,
        expected_current_revision_id=None,
        description=None,
    ):
        """Atomically publish an ordered Block set and immutable revision."""
        payload = {
            "request_id": str(request_id),
            "name": name,
            "description": description,
            "blocks": list(blocks),
        }
        if experiment_id is None:
            path = "/api/v1/experiments/publish"
        else:
            path = f"/api/v1/experiments/{experiment_id}/publish"
            payload["expected_current_revision_id"] = expected_current_revision_id
        return self._json_request("POST", path, payload)

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

    def download_experiment_bulk_export(
        self,
        experiment_id,
        run_ids,
        include,
        destination,
        *,
        analysis_policy="latest",
        progress=None,
        cancel_event=None,
    ):
        """Ask the server to build one results archive and stream it to disk."""
        run_ids = [str(run_id) for run_id in run_ids]
        include = [str(kind) for kind in include]
        if not run_ids:
            raise ValueError("At least one participant run must be selected.")
        if not include:
            raise ValueError("At least one export type must be selected.")
        if analysis_policy not in {"latest", "all"}:
            raise ValueError("analysis_policy must be 'latest' or 'all'.")

        def cancellation_requested():
            return cancel_event is not None and cancel_event.is_set()

        if cancellation_requested():
            raise APICancelled("Bulk export download was cancelled.")

        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(f"{destination.name}.part")
        body = json.dumps(
            {
                "run_ids": run_ids,
                "include": include,
                "analysis_policy": analysis_policy,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Accept": "application/zip",
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            **self._auth_headers(),
        }
        connection = self._connection(timeout=max(self.timeout, 300))
        try:
            connection.request(
                "POST",
                self._path(
                    f"/api/v1/experiments/{experiment_id}/bulk-export"
                ),
                body=body,
                headers=headers,
            )
            if cancellation_requested():
                raise APICancelled("Bulk export download was cancelled.")
            response = connection.getresponse()
            if cancellation_requested():
                raise APICancelled("Bulk export download was cancelled.")
            if response.status >= 400:
                error_body = response.read()
                raise self._error_from_response(response, error_body)

            expected_size = int(response.getheader("Content-Length") or 0)
            expected_sha256 = response.getheader("X-Checksum-SHA256")
            digest = hashlib.sha256()
            received = 0
            with partial.open("wb") as output:
                while True:
                    if cancellation_requested():
                        raise APICancelled("Bulk export download was cancelled.")
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    if cancellation_requested():
                        raise APICancelled("Bulk export download was cancelled.")
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, expected_size)
            if cancellation_requested():
                raise APICancelled("Bulk export download was cancelled.")
            if expected_size and received != expected_size:
                raise APIError("Bulk export download ended before it was complete.")
            if expected_sha256 and digest.hexdigest() != expected_sha256:
                raise APIError("Bulk export failed its SHA-256 check.")
            partial.replace(destination)
            return destination
        except (APIError, ValueError):
            partial.unlink(missing_ok=True)
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            partial.unlink(missing_ok=True)
            raise APIError(
                f"Could not download the bulk results export: {exc}"
            ) from exc
        finally:
            connection.close()

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
