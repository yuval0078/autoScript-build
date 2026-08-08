"""Secure, atomic component updates backed by signed GitHub release catalogs.

The updater deliberately keeps installed application versions separate from
legacy AutoScript user data.  Callers may inject a fetcher, clock, trust keys,
and root directory so the complete update protocol can be tested offline.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import ntpath
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app_paths import asset_path, user_data_dir
from component_versions import (
    COMPONENT_NAMES,
    compare_component_versions,
    parse_component_version,
)


DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')


class UpdateError(RuntimeError):
    """Base class for update failures safe to present to a user."""


class CatalogError(UpdateError):
    pass


class CatalogFetchError(CatalogError):
    pass


class NetworkUnavailableError(CatalogFetchError):
    pass


class SignatureVerificationError(CatalogError):
    pass


class TrustConfigurationError(CatalogError):
    pass


class CatalogExpiredError(CatalogError):
    pass


class RollbackProtectionError(CatalogError):
    pass


class ComponentDowngradeError(CatalogError):
    pass


class CompatibilityError(UpdateError):
    pass


class DownloadVerificationError(UpdateError):
    pass


class UnsafeComponentArchiveError(UpdateError):
    pass


class InstallStateError(UpdateError):
    pass


class UpdateLockError(UpdateError):
    pass


@dataclass(frozen=True)
class CatalogComponent:
    name: str
    version: str
    url: str
    sha256: str
    size: int
    entrypoint: str
    min_bootstrap: str
    protocol: int


@dataclass(frozen=True)
class UpdateCatalog:
    schema_version: int
    sequence: int
    channel: str
    published_at: datetime
    expires_at: datetime
    components: Mapping[str, CatalogComponent]
    sha256: str
    source: str


@dataclass(frozen=True)
class InstalledComponent:
    name: str
    version: str
    relative_path: str
    sha256: str
    size: int
    entrypoint: str
    catalog_sequence: int
    activated_at: str


class FetchResponse:
    """Small streaming response wrapper used by production and test fetchers."""

    def __init__(
        self,
        status: int,
        headers: Mapping[str, object] | None = None,
        body: bytes | BinaryIO | Iterable[bytes] = b"",
    ):
        self.status = int(status)
        self.headers = {
            str(key).lower(): str(value) for key, value in (headers or {}).items()
        }
        self._body = body

    def iter_bytes(self, chunk_size: int = 1024 * 1024):
        if isinstance(self._body, (bytes, bytearray, memoryview)):
            view = memoryview(self._body)
            for offset in range(0, len(view), chunk_size):
                yield bytes(view[offset : offset + chunk_size])
            return
        if hasattr(self._body, "read"):
            while True:
                chunk = self._body.read(chunk_size)
                if not chunk:
                    break
                yield bytes(chunk)
            return
        for chunk in self._body:
            if chunk:
                yield bytes(chunk)

    def close(self):
        close = getattr(self._body, "close", None)
        if close:
            close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _validate_https_url(url: str, allowed_hosts: frozenset[str]) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise CatalogError("Update URLs must be non-empty strings of a safe length.")
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise CatalogError("Update URL is not an allowed GitHub HTTPS URL.")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validate_https_url(newurl, self.allowed_hosts)
        except CatalogError as exc:
            raise CatalogFetchError(f"Unsafe update redirect: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HTTPSFetcher:
    """HTTPS-only fetcher with GitHub-host validation on every redirect."""

    def __init__(
        self,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
        timeout: float = 30,
    ):
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            _SafeRedirectHandler(self.allowed_hosts)
        )

    def fetch(self, url: str, etag: str | None = None) -> FetchResponse:
        _validate_https_url(url, self.allowed_hosts)
        headers = {
            "Accept": "application/octet-stream, application/json",
            "User-Agent": "AutoScript-Component-Updater/1",
        }
        if etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=self.timeout)
            return FetchResponse(response.status, response.headers, response)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FetchResponse(304, exc.headers, exc)
            if exc.code == 429 or 500 <= exc.code <= 599:
                exc.close()
                raise NetworkUnavailableError(
                    f"GitHub update service returned HTTP {exc.code}."
                ) from exc
            exc.close()
            raise CatalogFetchError(
                f"GitHub update service returned HTTP {exc.code}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkUnavailableError(
                f"Could not reach the GitHub update service: {exc}"
            ) from exc


_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class ComponentUpdateManager:
    """Validate, download, install, activate, and roll back components."""

    CATALOG_SCHEMA_VERSION = 1
    SIGNATURE_SCHEMA_VERSION = 1
    STATE_SCHEMA_VERSION = 1

    def __init__(
        self,
        catalog_url: str,
        *,
        signature_url: str | None = None,
        root: str | Path | None = None,
        fetcher=None,
        trusted_keys: Mapping[str, bytes | str] | None = None,
        signature_threshold: int | None = None,
        trust_config_path: str | Path | None = None,
        require_signature: bool = True,
        bootstrap_version: str = "0.0",
        clock: Callable[[], datetime] | None = None,
        allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_HOSTS,
        max_catalog_bytes: int = 1024 * 1024,
        max_archive_bytes: int = 2 * 1024 * 1024 * 1024,
        max_archive_members: int = 20_000,
        max_member_bytes: int = 512 * 1024 * 1024,
        max_expanded_bytes: int = 4 * 1024 * 1024 * 1024,
        max_compression_ratio: int = 1000,
        lock_timeout: float = 30,
    ):
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.catalog_url = _validate_https_url(catalog_url, self.allowed_hosts)
        self.signature_url = _validate_https_url(
            signature_url or self._default_signature_url(catalog_url),
            self.allowed_hosts,
        )
        self.root = Path(root) if root is not None else self._default_root()
        self.root = self.root.expanduser().resolve()
        self.state_dir = self.root / "state"
        self.cache_dir = self.root / "cache"
        self.components_dir = self.root / "components"
        self.staging_dir = self.root / ".staging"
        for directory in (
            self.state_dir,
            self.cache_dir,
            self.components_dir,
            self.staging_dir,
            self.state_dir / "active",
        ):
            directory.mkdir(parents=True, exist_ok=True)

        self.fetcher = fetcher or HTTPSFetcher(self.allowed_hosts)
        self.require_signature = bool(require_signature)
        self.bootstrap_version = bootstrap_version
        parse_component_version(bootstrap_version)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_catalog_bytes = self._positive_limit(
            max_catalog_bytes, "max_catalog_bytes"
        )
        self.max_archive_bytes = self._positive_limit(
            max_archive_bytes, "max_archive_bytes"
        )
        self.max_archive_members = self._positive_limit(
            max_archive_members, "max_archive_members"
        )
        self.max_member_bytes = self._positive_limit(
            max_member_bytes, "max_member_bytes"
        )
        self.max_expanded_bytes = self._positive_limit(
            max_expanded_bytes, "max_expanded_bytes"
        )
        self.max_compression_ratio = self._positive_limit(
            max_compression_ratio, "max_compression_ratio"
        )
        self.lock_timeout = float(lock_timeout)
        if self.lock_timeout < 0:
            raise ValueError("lock_timeout cannot be negative.")

        if trusted_keys is None:
            trust_path = (
                Path(trust_config_path)
                if trust_config_path is not None
                else asset_path("update_trust.json")
            )
            keys, configured_threshold = self._load_trust_config(trust_path)
        else:
            keys = self._normalize_trusted_keys(trusted_keys)
            configured_threshold = 1
        self.trusted_keys = keys
        self.signature_threshold = (
            configured_threshold
            if signature_threshold is None
            else signature_threshold
        )
        if (
            isinstance(self.signature_threshold, bool)
            or not isinstance(self.signature_threshold, int)
            or self.signature_threshold < 1
        ):
            raise TrustConfigurationError("Signature threshold must be positive.")
        if self.trusted_keys and self.signature_threshold > len(self.trusted_keys):
            raise TrustConfigurationError(
                "Signature threshold exceeds the number of trusted keys."
            )

    @staticmethod
    def _positive_limit(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer.")
        return value

    @staticmethod
    def _default_root() -> Path:
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            return Path(os.environ["LOCALAPPDATA"]) / "AutoScript"
        return user_data_dir() / "component_updates"

    @staticmethod
    def _default_signature_url(catalog_url: str) -> str:
        parsed = urlsplit(catalog_url)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, f"{parsed.path}.sig", parsed.query, "")
        )

    @property
    def registry_path(self) -> Path:
        return self.state_dir / "installed.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "update.lock"

    @property
    def cached_catalog_path(self) -> Path:
        return self.cache_dir / "catalog.json"

    @property
    def cached_signature_path(self) -> Path:
        return self.cache_dir / "catalog.json.sig"

    @property
    def cache_metadata_path(self) -> Path:
        return self.cache_dir / "catalog-cache.json"

    def active_pointer_path(self, component: str) -> Path:
        self._require_component_name(component)
        return self.state_dir / "active" / f"{component}.json"

    @staticmethod
    def _require_component_name(component: str):
        if component not in COMPONENT_NAMES:
            raise InstallStateError(f"Unknown component: {component!r}")

    @staticmethod
    def _decode_public_key(value: bytes | str) -> bytes:
        if isinstance(value, bytes):
            decoded = value
        elif isinstance(value, str):
            try:
                decoded = base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise TrustConfigurationError(
                    "Trusted Ed25519 public keys must be valid base64."
                ) from exc
        else:
            raise TrustConfigurationError("Trusted key values must be bytes or base64.")
        if len(decoded) != 32:
            raise TrustConfigurationError("Ed25519 public keys must contain 32 bytes.")
        return decoded

    @classmethod
    def _normalize_trusted_keys(
        cls, trusted_keys: Mapping[str, bytes | str]
    ) -> dict[str, bytes]:
        if not isinstance(trusted_keys, Mapping):
            raise TrustConfigurationError("Trusted keys must be a mapping.")
        normalized = {}
        for key_id, value in trusted_keys.items():
            if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
                raise TrustConfigurationError("Trusted key ID is invalid.")
            normalized[key_id] = cls._decode_public_key(value)
        return normalized

    @classmethod
    def _load_trust_config(cls, path: Path) -> tuple[dict[str, bytes], int]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TrustConfigurationError(
                f"Could not read update trust configuration from {path}: {exc}"
            ) from exc
        expected = {"schema_version", "algorithm", "threshold", "keys"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise TrustConfigurationError("Update trust configuration has unexpected fields.")
        if payload["schema_version"] != 1 or payload["algorithm"] != "ed25519":
            raise TrustConfigurationError("Unsupported update trust configuration.")
        threshold = payload["threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
            raise TrustConfigurationError("Signature threshold must be positive.")
        keys_payload = payload["keys"]
        if not isinstance(keys_payload, dict):
            raise TrustConfigurationError("Trust configuration keys must be an object.")
        keys = {}
        for key_id, definition in keys_payload.items():
            if (
                not isinstance(definition, dict)
                or set(definition) != {"algorithm", "public_key"}
                or definition.get("algorithm") != "ed25519"
            ):
                raise TrustConfigurationError("Trusted key definition is invalid.")
            keys[key_id] = definition["public_key"]
        return cls._normalize_trusted_keys(keys), threshold

    def _fetch(self, url: str, etag: str | None = None) -> FetchResponse:
        _validate_https_url(url, self.allowed_hosts)
        method = getattr(self.fetcher, "fetch", None)
        if method is None:
            method = self.fetcher
        try:
            response = method(url, etag=etag)
        except TypeError:
            response = method(url, etag)
        if not isinstance(response, FetchResponse):
            raise CatalogFetchError("Update fetcher returned an invalid response.")
        return response

    @staticmethod
    def _parse_time(value: object, field: str) -> datetime:
        if not isinstance(value, str) or not _RFC3339_PATTERN.fullmatch(value):
            raise CatalogError(f"{field} must be an RFC 3339 timestamp.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CatalogError(f"{field} is not a valid timestamp.") from exc
        if parsed.tzinfo is None:
            raise CatalogError(f"{field} must include a timezone.")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _safe_relative_path(value: object, field: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
            raise CatalogError(f"{field} is not a safe relative path.")
        normalized = value.replace("\\", "/")
        drive, _ = ntpath.splitdrive(normalized)
        path = PurePosixPath(normalized)
        if (
            drive
            or normalized.startswith(("/", "//"))
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise CatalogError(f"{field} is not a safe relative path.")
        for part in path.parts:
            device_name = part.split(".", 1)[0].upper()
            if (
                part.endswith((" ", "."))
                or device_name in _WINDOWS_RESERVED_NAMES
                or any(
                    ord(character) < 32
                    or character in _WINDOWS_INVALID_CHARACTERS
                    for character in part
                )
            ):
                raise CatalogError(f"{field} is not a Windows-safe relative path.")
        return path.as_posix()

    def _parse_catalog(self, raw: bytes, source: str) -> UpdateCatalog:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError("Update catalog is not valid UTF-8 JSON.") from exc
        expected = {
            "schema_version",
            "sequence",
            "channel",
            "published_at",
            "expires_at",
            "components",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise CatalogError("Update catalog has unexpected fields.")
        if payload["schema_version"] != self.CATALOG_SCHEMA_VERSION:
            raise CatalogError("Unsupported update catalog schema.")
        sequence = payload["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise CatalogError("Catalog sequence must be a positive integer.")
        channel = payload["channel"]
        if channel not in {"stable", "beta"}:
            raise CatalogError("Catalog channel must be stable or beta.")
        published_at = self._parse_time(payload["published_at"], "published_at")
        expires_at = self._parse_time(payload["expires_at"], "expires_at")
        if published_at >= expires_at:
            raise CatalogError("Catalog expiry must be after its publication time.")
        now = self.clock().astimezone(timezone.utc)
        if expires_at <= now:
            raise CatalogExpiredError("Update catalog has expired.")

        definitions = payload["components"]
        if not isinstance(definitions, dict) or set(definitions) != set(COMPONENT_NAMES):
            raise CatalogError(
                "Catalog must define interface, builder, runner, and analyzer exactly once."
            )
        component_fields = {
            "version",
            "url",
            "sha256",
            "size",
            "entrypoint",
            "min_bootstrap",
            "protocol",
        }
        components = {}
        for name in COMPONENT_NAMES:
            definition = definitions[name]
            if not isinstance(definition, dict) or set(definition) != component_fields:
                raise CatalogError(f"Catalog component {name!r} has unexpected fields.")
            version = definition["version"]
            min_bootstrap = definition["min_bootstrap"]
            try:
                parse_component_version(version)
                parse_component_version(min_bootstrap)
            except ValueError as exc:
                raise CatalogError(f"Catalog component {name!r} has an invalid version.") from exc
            sha256 = definition["sha256"]
            if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
                raise CatalogError(f"Catalog component {name!r} has an invalid SHA-256.")
            size = definition["size"]
            if (
                isinstance(size, bool)
                or not isinstance(size, int)
                or size < 1
                or size > self.max_archive_bytes
            ):
                raise CatalogError(f"Catalog component {name!r} has an invalid size.")
            protocol = definition["protocol"]
            if isinstance(protocol, bool) or not isinstance(protocol, int) or protocol < 1:
                raise CatalogError(f"Catalog component {name!r} has an invalid protocol.")
            entrypoint = self._safe_relative_path(
                definition["entrypoint"], f"{name}.entrypoint"
            )
            if not entrypoint.casefold().endswith(".exe"):
                raise CatalogError(f"Catalog component {name!r} entrypoint must be an EXE.")
            components[name] = CatalogComponent(
                name=name,
                version=version,
                url=_validate_https_url(definition["url"], self.allowed_hosts),
                sha256=sha256,
                size=size,
                entrypoint=entrypoint,
                min_bootstrap=min_bootstrap,
                protocol=protocol,
            )
        return UpdateCatalog(
            schema_version=self.CATALOG_SCHEMA_VERSION,
            sequence=sequence,
            channel=channel,
            published_at=published_at,
            expires_at=expires_at,
            components=components,
            sha256=hashlib.sha256(raw).hexdigest(),
            source=source,
        )

    def _verify_signature(self, raw: bytes, signature_raw: bytes):
        if not self.require_signature:
            return
        if not self.trusted_keys:
            raise TrustConfigurationError(
                "No production update signing keys have been provisioned."
            )
        try:
            payload = json.loads(signature_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignatureVerificationError(
                "Catalog signature envelope is not valid UTF-8 JSON."
            ) from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "signatures"}
            or payload["schema_version"] != self.SIGNATURE_SCHEMA_VERSION
            or not isinstance(payload["signatures"], list)
        ):
            raise SignatureVerificationError("Catalog signature envelope is invalid.")

        valid_keys = set()
        seen_keys = set()
        for item in payload["signatures"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"key_id", "algorithm", "signature"}
                or item.get("algorithm") != "ed25519"
            ):
                raise SignatureVerificationError("Catalog signature entry is invalid.")
            key_id = item.get("key_id")
            if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
                raise SignatureVerificationError("Catalog signature key ID is invalid.")
            if key_id in seen_keys:
                raise SignatureVerificationError("Catalog contains duplicate signatures.")
            seen_keys.add(key_id)
            public_key = self.trusted_keys.get(key_id)
            if public_key is None:
                continue
            try:
                signature = base64.b64decode(item["signature"], validate=True)
            except (TypeError, ValueError, binascii.Error) as exc:
                raise SignatureVerificationError(
                    "Catalog signature is not valid base64."
                ) from exc
            if len(signature) != 64:
                raise SignatureVerificationError("Ed25519 signatures must contain 64 bytes.")
            try:
                Ed25519PublicKey.from_public_bytes(public_key).verify(signature, raw)
            except (InvalidSignature, ValueError):
                continue
            valid_keys.add(key_id)
        if len(valid_keys) < self.signature_threshold:
            raise SignatureVerificationError(
                "Catalog does not meet the trusted signature threshold."
            )

    @staticmethod
    def _read_response_limited(response: FetchResponse, limit: int) -> bytes:
        result = io.BytesIO()
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise CatalogFetchError("Update metadata exceeds its size limit.")
            result.write(chunk)
        return result.getvalue()

    def _read_cache_metadata(self) -> dict:
        if not self.cache_metadata_path.exists():
            return {}
        try:
            payload = json.loads(self.cache_metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "etag"}
            or payload.get("schema_version") != 1
            or payload.get("etag") is not None
            and not isinstance(payload.get("etag"), str)
        ):
            return {}
        return payload

    def _read_cached_catalog(self) -> tuple[bytes, bytes] | None:
        try:
            catalog = self.cached_catalog_path.read_bytes()
            signature = (
                self.cached_signature_path.read_bytes()
                if self.require_signature
                else b""
            )
        except OSError:
            return None
        return catalog, signature

    def _remote_catalog(self, etag: str | None):
        with self._fetch(self.catalog_url, etag=etag) as response:
            if response.status == 304:
                return 304, None, None, response.headers.get("etag") or etag
            if response.status != 200:
                if response.status == 429 or response.status >= 500:
                    raise NetworkUnavailableError(
                        f"GitHub update service returned HTTP {response.status}."
                    )
                raise CatalogFetchError(
                    f"GitHub update service returned HTTP {response.status}."
                )
            raw = self._read_response_limited(response, self.max_catalog_bytes)
            new_etag = response.headers.get("etag")
        signature_raw = b""
        if self.require_signature:
            with self._fetch(self.signature_url) as response:
                if response.status != 200:
                    if response.status == 429 or response.status >= 500:
                        raise NetworkUnavailableError(
                            f"GitHub signature service returned HTTP {response.status}."
                        )
                    raise CatalogFetchError(
                        f"GitHub signature service returned HTTP {response.status}."
                    )
                signature_raw = self._read_response_limited(response, 256 * 1024)
        return 200, raw, signature_raw, new_etag

    def load_catalog(self, *, allow_offline: bool = True) -> UpdateCatalog:
        """Fetch and authenticate the newest catalog, or use verified cache offline."""

        cached = self._read_cached_catalog()
        etag = self._read_cache_metadata().get("etag")
        network_error = None
        try:
            status, raw, signature_raw, new_etag = self._remote_catalog(etag)
        except (NetworkUnavailableError, OSError, TimeoutError) as exc:
            network_error = exc
            status = None
            raw = signature_raw = new_etag = None

        if status == 304:
            if cached is None:
                raise CatalogFetchError("GitHub returned not-modified without a local catalog.")
            raw, signature_raw = cached
            source = "cache"
        elif status == 200:
            source = "network"
        elif allow_offline and cached is not None:
            raw, signature_raw = cached
            source = "offline-cache"
        else:
            raise network_error or NetworkUnavailableError(
                "No authenticated update catalog is available offline."
            )

        self._verify_signature(raw, signature_raw)
        catalog = self._parse_catalog(raw, source)
        with self._update_lock():
            self._accept_catalog(catalog)
            if status == 200:
                self._atomic_write_bytes(self.cached_catalog_path, raw)
                if self.require_signature:
                    self._atomic_write_bytes(
                        self.cached_signature_path, signature_raw
                    )
                self._atomic_write_json(
                    self.cache_metadata_path,
                    {"schema_version": 1, "etag": new_etag},
                )
        return catalog

    def _empty_registry(self) -> dict:
        return {
            "schema_version": self.STATE_SCHEMA_VERSION,
            "highest_catalog_sequence": 0,
            "highest_catalog_sha256": None,
            "components": {},
        }

    def _load_registry(self) -> dict:
        if not self.registry_path.exists():
            return self._empty_registry()
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallStateError("Installed component registry is unreadable.") from exc
        expected = {
            "schema_version",
            "highest_catalog_sequence",
            "highest_catalog_sha256",
            "components",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise InstallStateError("Installed component registry has unexpected fields.")
        if payload["schema_version"] != self.STATE_SCHEMA_VERSION:
            raise InstallStateError("Unsupported installed component registry schema.")
        sequence = payload["highest_catalog_sequence"]
        digest = payload["highest_catalog_sha256"]
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or digest is not None
            and (not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest))
            or not isinstance(payload["components"], dict)
            or not set(payload["components"]).issubset(COMPONENT_NAMES)
        ):
            raise InstallStateError("Installed component registry is invalid.")
        for name, entry in payload["components"].items():
            self._validate_current_entry(name, entry)
        return payload

    def _accept_catalog(self, catalog: UpdateCatalog):
        registry = self._load_registry()
        highest = registry["highest_catalog_sequence"]
        if catalog.sequence < highest:
            raise RollbackProtectionError(
                f"Catalog sequence {catalog.sequence} is older than accepted sequence {highest}."
            )
        if (
            catalog.sequence == highest
            and registry["highest_catalog_sha256"] not in (None, catalog.sha256)
        ):
            raise RollbackProtectionError(
                "Catalog content changed without increasing its sequence."
            )
        for name, installed in registry["components"].items():
            if compare_component_versions(
                catalog.components[name].version, installed["version"]
            ) < 0:
                raise ComponentDowngradeError(
                    f"Catalog would downgrade {name} from {installed['version']} "
                    f"to {catalog.components[name].version}."
                )
        if catalog.sequence > highest or registry["highest_catalog_sha256"] is None:
            registry["highest_catalog_sequence"] = catalog.sequence
            registry["highest_catalog_sha256"] = catalog.sha256
            self._atomic_write_json(self.registry_path, registry)

    def _ensure_catalog_is_accepted(self, catalog: UpdateCatalog, registry: dict):
        highest = registry["highest_catalog_sequence"]
        digest = registry["highest_catalog_sha256"]
        if catalog.sequence < highest or (
            catalog.sequence == highest and digest not in (None, catalog.sha256)
        ):
            raise RollbackProtectionError("Catalog is not the currently trusted catalog.")
        if catalog.expires_at <= self.clock().astimezone(timezone.utc):
            raise CatalogExpiredError("Update catalog expired before installation.")

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: object):
        encoded = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        ).encode("utf-8") + b"\n"
        cls._atomic_write_bytes(path, encoded)

    @contextmanager
    def _update_lock(self):
        lock_key = os.path.normcase(str(self.lock_path.resolve()))
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(lock_key, threading.Lock())
        if not process_lock.acquire(timeout=self.lock_timeout):
            raise UpdateLockError("Another component update is already in progress.")
        handle = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise UpdateLockError(
                            "Another component update is already in progress."
                        )
                    time.sleep(0.05)
            yield
        finally:
            if handle is not None:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()
            process_lock.release()

    def _download_archive(self, definition: CatalogComponent, destination: Path):
        partial = destination.with_suffix(".part")
        digest = hashlib.sha256()
        received = 0
        try:
            with self._fetch(definition.url) as response:
                if response.status != 200:
                    raise CatalogFetchError(
                        f"Component download returned HTTP {response.status}."
                    )
                header_size = response.headers.get("content-length")
                if header_size is not None:
                    try:
                        header_size = int(header_size)
                    except ValueError as exc:
                        raise DownloadVerificationError(
                            "Component download has an invalid Content-Length."
                        ) from exc
                    if header_size != definition.size:
                        raise DownloadVerificationError(
                            "Component download size does not match the signed catalog."
                        )
                with partial.open("xb") as output:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > definition.size or received > self.max_archive_bytes:
                            raise DownloadVerificationError(
                                "Component download exceeded its signed size."
                            )
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if received != definition.size:
                raise DownloadVerificationError(
                    "Component download is shorter than its signed size."
                )
            if digest.hexdigest() != definition.sha256:
                raise DownloadVerificationError(
                    "Component download failed its SHA-256 verification."
                )
            os.replace(partial, destination)
        finally:
            if partial.exists():
                partial.unlink()

    def _validated_archive_entries(self, archive: zipfile.ZipFile):
        entries = archive.infolist()
        if len(entries) > self.max_archive_members:
            raise UnsafeComponentArchiveError("Component archive contains too many members.")
        total_size = 0
        paths: dict[str, bool] = {}
        validated = []
        for info in entries:
            if info.flag_bits & 0x1:
                raise UnsafeComponentArchiveError(
                    "Encrypted component archives are not supported."
                )
            try:
                relative = self._safe_relative_path(info.filename, "Archive member")
            except CatalogError as exc:
                raise UnsafeComponentArchiveError(str(exc)) from exc
            is_directory = info.is_dir() or info.filename.endswith(("/", "\\"))
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if file_type == stat.S_IFLNK:
                raise UnsafeComponentArchiveError("Archive symlinks are not allowed.")
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UnsafeComponentArchiveError(
                    "Archive contains a non-regular filesystem entry."
                )
            if (info.external_attr & 0xFFFF) & 0x400:
                raise UnsafeComponentArchiveError("Archive reparse points are not allowed.")
            if info.file_size < 0 or info.file_size > self.max_member_bytes:
                raise UnsafeComponentArchiveError("Archive member exceeds its size limit.")
            if is_directory and info.file_size != 0:
                raise UnsafeComponentArchiveError(
                    "Archive directory has unexpected file content."
                )
            total_size += info.file_size
            if total_size > self.max_expanded_bytes:
                raise UnsafeComponentArchiveError(
                    "Component archive exceeds its expanded-size limit."
                )
            if (
                info.file_size > 10 * 1024 * 1024
                and (
                    info.compress_size == 0
                    or info.file_size / info.compress_size
                    > self.max_compression_ratio
                )
            ):
                raise UnsafeComponentArchiveError(
                    "Component archive has an unsafe compression ratio."
                )
            key = unicodedata.normalize("NFC", relative).casefold()
            if key in paths:
                raise UnsafeComponentArchiveError(
                    "Archive contains duplicate or case-colliding paths."
                )
            parent = PurePosixPath(relative).parent
            while parent != PurePosixPath("."):
                parent_key = unicodedata.normalize("NFC", parent.as_posix()).casefold()
                if parent_key in paths and not paths[parent_key]:
                    raise UnsafeComponentArchiveError(
                        "Archive path is nested below a file."
                    )
                parent = parent.parent
            if not is_directory and any(existing.startswith(f"{key}/") for existing in paths):
                raise UnsafeComponentArchiveError(
                    "Archive file collides with an existing directory."
                )
            paths[key] = is_directory
            validated.append((info, relative, is_directory))
        return validated

    def _safe_extract_component(self, archive_path: Path, destination: Path):
        destination.mkdir(parents=True, exist_ok=False)
        root = destination.resolve()
        try:
            with zipfile.ZipFile(archive_path, "r") as archive:
                validated = self._validated_archive_entries(archive)
                expanded = 0
                for info, relative, is_directory in validated:
                    target = destination.joinpath(*PurePosixPath(relative).parts)
                    resolved = target.resolve(strict=False)
                    try:
                        resolved.relative_to(root)
                    except ValueError as exc:
                        raise UnsafeComponentArchiveError(
                            "Archive member escapes the staging directory."
                        ) from exc
                    if is_directory:
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    member_size = 0
                    with archive.open(info, "r") as source, target.open("xb") as output:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            member_size += len(chunk)
                            expanded += len(chunk)
                            if (
                                member_size > info.file_size
                                or member_size > self.max_member_bytes
                                or expanded > self.max_expanded_bytes
                            ):
                                raise UnsafeComponentArchiveError(
                                    "Archive expanded beyond its declared limits."
                                )
                            output.write(chunk)
                    if member_size != info.file_size:
                        raise UnsafeComponentArchiveError(
                            "Archive member size does not match its declaration."
                        )
        except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
            raise UnsafeComponentArchiveError(
                f"Component archive is invalid: {exc}"
            ) from exc

    def _entry_to_installed(self, name: str, entry: dict) -> InstalledComponent:
        return InstalledComponent(
            name=name,
            version=entry["version"],
            relative_path=entry["path"],
            sha256=entry["sha256"],
            size=entry["size"],
            entrypoint=entry["entrypoint"],
            catalog_sequence=entry["catalog_sequence"],
            activated_at=entry["activated_at"],
        )

    def _validate_current_entry(self, component: str, entry: object) -> dict:
        fields = {
            "version",
            "path",
            "sha256",
            "size",
            "entrypoint",
            "catalog_sequence",
            "activated_at",
        }
        if not isinstance(entry, dict) or set(entry) != fields:
            raise InstallStateError(f"Installed state for {component} is invalid.")
        try:
            parse_component_version(entry["version"])
            relative_path = self._safe_relative_path(entry["path"], "Installed path")
            entrypoint = self._safe_relative_path(entry["entrypoint"], "Installed entrypoint")
            self._parse_time(entry["activated_at"], "activated_at")
        except (CatalogError, ValueError) as exc:
            raise InstallStateError(f"Installed state for {component} is invalid.") from exc
        expected_prefix = f"components/{component}/"
        if not relative_path.casefold().startswith(expected_prefix.casefold()):
            raise InstallStateError("Installed component path is outside its component root.")
        if (
            not isinstance(entry["sha256"], str)
            or not _SHA256_PATTERN.fullmatch(entry["sha256"])
            or isinstance(entry["size"], bool)
            or not isinstance(entry["size"], int)
            or entry["size"] < 1
            or isinstance(entry["catalog_sequence"], bool)
            or not isinstance(entry["catalog_sequence"], int)
            or entry["catalog_sequence"] < 1
        ):
            raise InstallStateError(f"Installed state for {component} is invalid.")
        entry["path"] = relative_path
        entry["entrypoint"] = entrypoint
        return entry

    def _read_pointer(self, component: str) -> dict | None:
        path = self.active_pointer_path(component)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallStateError(f"Active pointer for {component} is unreadable.") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "component", "current", "previous"}
            or payload["schema_version"] != 1
            or payload["component"] != component
        ):
            raise InstallStateError(f"Active pointer for {component} is invalid.")
        self._validate_current_entry(component, payload["current"])
        if payload["previous"] is not None:
            self._validate_current_entry(component, payload["previous"])
        return payload

    def get_installed_component(self, component: str) -> InstalledComponent | None:
        self._require_component_name(component)
        pointer = self._read_pointer(component)
        if pointer is None:
            return None
        return self._entry_to_installed(component, pointer["current"])

    def installed_registry(self) -> dict:
        """Return a defensive copy of the durable installed component registry."""

        return json.loads(json.dumps(self._load_registry()))

    def _verify_component_descriptor(self, extracted, component, definition):
        descriptor_path = extracted / "component.json"
        try:
            descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UnsafeComponentArchiveError(
                "Component archive has no readable component.json descriptor."
            ) from exc
        expected_fields = {
            "format_version",
            "id",
            "version",
            "platform",
            "arch",
            "entrypoint",
            "protocol_version",
            "source_commit",
        }
        if not isinstance(descriptor, dict) or set(descriptor) != expected_fields:
            raise UnsafeComponentArchiveError(
                "Component descriptor has unexpected fields."
            )
        if (
            descriptor["format_version"] != 1
            or descriptor["id"] != component
            or descriptor["version"] != definition.version
            or descriptor["platform"] != "windows"
            or descriptor["arch"] not in {"x86_64", "amd64"}
            or descriptor["entrypoint"] != definition.entrypoint
            or descriptor["protocol_version"] != definition.protocol
            or not isinstance(descriptor["source_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", descriptor["source_commit"])
        ):
            raise UnsafeComponentArchiveError(
                "Component descriptor does not match the signed catalog."
            )
        return descriptor

    def available_updates(self, catalog: UpdateCatalog) -> dict[str, bool]:
        updates = {}
        for name in COMPONENT_NAMES:
            installed = self.get_installed_component(name)
            updates[name] = installed is None or compare_component_versions(
                catalog.components[name].version, installed.version
            ) > 0
        return updates

    def install_component(
        self, catalog: UpdateCatalog, component: str
    ) -> InstalledComponent:
        """Install and atomically activate one catalog component."""

        self._require_component_name(component)
        definition = catalog.components[component]
        if compare_component_versions(
            self.bootstrap_version, definition.min_bootstrap
        ) < 0:
            raise CompatibilityError(
                f"{component} requires updater {definition.min_bootstrap} or newer."
            )

        with self._update_lock():
            registry = self._load_registry()
            self._ensure_catalog_is_accepted(catalog, registry)
            old_pointer = self._read_pointer(component)
            if old_pointer is not None:
                old_entry = old_pointer["current"]
                comparison = compare_component_versions(
                    definition.version, old_entry["version"]
                )
                if comparison < 0:
                    raise ComponentDowngradeError(
                        f"Refusing to downgrade {component} from {old_entry['version']} "
                        f"to {definition.version}."
                    )
                if comparison == 0:
                    if old_entry["sha256"] != definition.sha256:
                        raise RollbackProtectionError(
                            "A component version cannot be replaced with different bytes."
                        )
                    return self._entry_to_installed(component, old_entry)

            transaction = self.staging_dir / f"{component}-{uuid.uuid4().hex}"
            transaction.mkdir(parents=False, exist_ok=False)
            archive_path = transaction / "component.zip"
            extracted = transaction / "extracted"
            target = self.components_dir / component / definition.version
            try:
                self._download_archive(definition, archive_path)
                self._safe_extract_component(archive_path, extracted)
                self._verify_component_descriptor(extracted, component, definition)
                entrypoint = extracted.joinpath(
                    *PurePosixPath(definition.entrypoint).parts
                )
                if not entrypoint.is_file() or entrypoint.is_symlink():
                    raise UnsafeComponentArchiveError(
                        "Component archive does not contain its signed entrypoint."
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                marker = {
                    "schema_version": 1,
                    "component": component,
                    "version": definition.version,
                    "sha256": definition.sha256,
                    "size": definition.size,
                    "entrypoint": definition.entrypoint,
                }
                self._atomic_write_json(extracted / ".autoscript-component.json", marker)
                if target.exists():
                    marker_path = target / ".autoscript-component.json"
                    try:
                        existing_marker = json.loads(
                            marker_path.read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise InstallStateError(
                            f"Existing {component} {definition.version} is unverified."
                        ) from exc
                    if existing_marker != marker:
                        raise InstallStateError(
                            f"Existing {component} {definition.version} conflicts with the catalog."
                        )
                    existing_entrypoint = target.joinpath(
                        *PurePosixPath(definition.entrypoint).parts
                    )
                    if (
                        not existing_entrypoint.is_file()
                        or existing_entrypoint.is_symlink()
                    ):
                        raise InstallStateError(
                            f"Existing {component} {definition.version} is incomplete."
                        )
                else:
                    os.replace(extracted, target)

                activated_at = self.clock().astimezone(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                current = {
                    "version": definition.version,
                    "path": target.relative_to(self.root).as_posix(),
                    "sha256": definition.sha256,
                    "size": definition.size,
                    "entrypoint": definition.entrypoint,
                    "catalog_sequence": catalog.sequence,
                    "activated_at": activated_at,
                }
                new_pointer = {
                    "schema_version": 1,
                    "component": component,
                    "current": current,
                    "previous": old_pointer["current"] if old_pointer else None,
                }
                pointer_path = self.active_pointer_path(component)
                self._atomic_write_json(pointer_path, new_pointer)
                try:
                    registry["components"][component] = current
                    self._atomic_write_json(self.registry_path, registry)
                except Exception:
                    if old_pointer is None:
                        pointer_path.unlink(missing_ok=True)
                    else:
                        self._atomic_write_json(pointer_path, old_pointer)
                    raise
                return self._entry_to_installed(component, current)
            finally:
                resolved_transaction = transaction.resolve()
                try:
                    resolved_transaction.relative_to(self.staging_dir.resolve())
                except ValueError:
                    raise InstallStateError("Unsafe staging cleanup target.")
                shutil.rmtree(resolved_transaction, ignore_errors=True)

    def rollback_component(self, component: str) -> InstalledComponent:
        """Atomically reactivate the previous successfully installed version."""

        self._require_component_name(component)
        with self._update_lock():
            registry = self._load_registry()
            old_pointer = self._read_pointer(component)
            if old_pointer is None or old_pointer["previous"] is None:
                raise InstallStateError(f"No previous {component} version is available.")
            previous = old_pointer["previous"]
            previous_root = self.root / Path(previous["path"])
            previous_entrypoint = previous_root.joinpath(
                *PurePosixPath(previous["entrypoint"]).parts
            )
            if not previous_entrypoint.is_file() or previous_entrypoint.is_symlink():
                raise InstallStateError(
                    f"Previous {component} installation is unavailable."
                )
            new_pointer = {
                "schema_version": 1,
                "component": component,
                "current": previous,
                "previous": old_pointer["current"],
            }
            pointer_path = self.active_pointer_path(component)
            self._atomic_write_json(pointer_path, new_pointer)
            try:
                registry["components"][component] = previous
                self._atomic_write_json(self.registry_path, registry)
            except Exception:
                self._atomic_write_json(pointer_path, old_pointer)
                raise
            return self._entry_to_installed(component, previous)


__all__ = [
    "CatalogComponent",
    "CatalogError",
    "CatalogExpiredError",
    "CatalogFetchError",
    "CompatibilityError",
    "ComponentDowngradeError",
    "ComponentUpdateManager",
    "DEFAULT_ALLOWED_HOSTS",
    "DownloadVerificationError",
    "FetchResponse",
    "HTTPSFetcher",
    "InstallStateError",
    "InstalledComponent",
    "NetworkUnavailableError",
    "RollbackProtectionError",
    "SignatureVerificationError",
    "TrustConfigurationError",
    "UnsafeComponentArchiveError",
    "UpdateCatalog",
    "UpdateError",
    "UpdateLockError",
]
