import base64
import hashlib
import io
import json
import stat
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from component_update_manager import (
    CatalogError,
    CatalogExpiredError,
    CompatibilityError,
    ComponentDowngradeError,
    ComponentUpdateManager,
    DownloadVerificationError,
    FetchResponse,
    InstallStateError,
    NetworkUnavailableError,
    RollbackProtectionError,
    SignatureVerificationError,
    TrustConfigurationError,
    UnsafeComponentArchiveError,
    UpdateLockError,
)
from component_versions import (
    COMPONENT_NAMES,
    ComponentVersionError,
    compare_component_versions,
    load_component_versions,
    parse_component_version,
)


CATALOG_URL = (
    "https://github.com/yuval0078/autoScript-build/releases/latest/download/"
    "autoscript-update-catalog.json"
)
SIGNATURE_URL = f"{CATALOG_URL}.sig"
FIXED_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


class FakeFetcher:
    def __init__(self):
        self.responses = {}
        self.calls = []

    def add(self, url, response):
        self.responses.setdefault(url, []).append(response)

    def fetch(self, url, etag=None):
        self.calls.append((url, etag))
        queue = self.responses.get(url, [])
        if not queue:
            raise NetworkUnavailableError(f"No fake response for {url}")
        response = queue.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def archive_bytes(
    entrypoint,
    content=b"executable",
    extra_entries=None,
    *,
    component=None,
    version="0.0",
    protocol=1,
):
    component = component or Path(entrypoint).stem.lower()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entrypoint, content)
        archive.writestr(
            "component.json",
            json.dumps(
                {
                    "format_version": 1,
                    "id": component,
                    "version": version,
                    "platform": "windows",
                    "arch": "x86_64",
                    "entrypoint": entrypoint,
                    "protocol_version": protocol,
                    "source_commit": "0" * 40,
                },
                sort_keys=True,
            ),
        )
        for name, value in extra_entries or []:
            if isinstance(name, zipfile.ZipInfo):
                archive.writestr(name, value)
            else:
                archive.writestr(name, value)
    return output.getvalue()


def catalog_fixture(
    private_key,
    *,
    sequence=1,
    versions=None,
    expires_at="2027-08-08T12:00:00Z",
    min_bootstrap=None,
    archive_overrides=None,
    payload_mutator=None,
):
    versions = {name: "0.0" for name in COMPONENT_NAMES} | (versions or {})
    min_bootstrap = {name: "0.0" for name in COMPONENT_NAMES} | (
        min_bootstrap or {}
    )
    archives = {}
    components = {}
    for name in COMPONENT_NAMES:
        entrypoint = f"{name.title()}.exe"
        archive = (archive_overrides or {}).get(
            name,
            archive_bytes(
                entrypoint,
                f"{name}-{versions[name]}".encode(),
                component=name,
                version=versions[name],
            ),
        )
        archives[name] = archive
        components[name] = {
            "version": versions[name],
            "url": (
                "https://github.com/yuval0078/autoScript-build/releases/download/"
                f"updates-r{sequence:06d}/{name}-{versions[name]}.zip"
            ),
            "sha256": hashlib.sha256(archive).hexdigest(),
            "size": len(archive),
            "entrypoint": entrypoint,
            "min_bootstrap": min_bootstrap[name],
            "protocol": 1,
        }
    payload = {
        "schema_version": 1,
        "sequence": sequence,
        "channel": "stable",
        "published_at": "2026-08-08T11:00:00Z",
        "expires_at": expires_at,
        "components": components,
    }
    if payload_mutator:
        payload_mutator(payload)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    signature = private_key.sign(raw)
    envelope = json.dumps(
        {
            "schema_version": 1,
            "signatures": [
                {
                    "key_id": "release-key",
                    "algorithm": "ed25519",
                    "signature": base64.b64encode(signature).decode("ascii"),
                }
            ],
        },
        sort_keys=True,
    ).encode("utf-8")
    return raw, envelope, payload, archives


class ComponentVersionTests(unittest.TestCase):
    def test_bundled_versions_track_independent_component_releases(self):
        versions = load_component_versions()
        self.assertEqual(
            versions,
            {
                "interface": "0.1",
                "builder": "0.0",
                "runner": "0.0",
                "analyzer": "0.1",
            },
        )

    def test_version_order_is_numeric_and_rejects_ambiguous_versions(self):
        self.assertEqual(parse_component_version("0.0"), (0, 0, 0, 0))
        self.assertLess(compare_component_versions("0.9", "0.10"), 0)
        self.assertEqual(compare_component_versions("1.2", "1.2.0.0"), 0)
        for invalid in ("0", "01.0", "1.0-beta", "1..0", "1.2.3.4.5", 1):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ComponentVersionError):
                    parse_component_version(invalid)

    def test_version_file_requires_exact_component_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "versions.json"
            path.write_text(
                json.dumps(
                    {"schema_version": 1, "components": {"interface": "0.0"}}
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ComponentVersionError):
                load_component_versions(path)


class ComponentUpdateManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.fetcher = FakeFetcher()

    def tearDown(self):
        self.temporary.cleanup()

    def manager(self, fetcher=None, **kwargs):
        root = kwargs.pop("root", self.root)
        trusted_keys = kwargs.pop(
            "trusted_keys", {"release-key": self.public_key}
        )
        clock = kwargs.pop("clock", lambda: FIXED_NOW)
        return ComponentUpdateManager(
            CATALOG_URL,
            signature_url=SIGNATURE_URL,
            root=root,
            fetcher=fetcher or self.fetcher,
            trusted_keys=trusted_keys,
            clock=clock,
            **kwargs,
        )

    def queue_catalog(self, raw, signature, *, etag='"catalog-1"'):
        self.fetcher.add(
            CATALOG_URL, FetchResponse(200, {"ETag": etag}, raw)
        )
        self.fetcher.add(SIGNATURE_URL, FetchResponse(200, {}, signature))

    def queue_archive(self, payload, archives, component, **response_kwargs):
        definition = payload["components"][component]
        body = response_kwargs.pop("body", archives[component])
        headers = response_kwargs.pop(
            "headers", {"Content-Length": str(len(body))}
        )
        self.fetcher.add(
            definition["url"], FetchResponse(200, headers, body)
        )

    def test_signed_catalog_is_cached_and_records_monotonic_sequence(self):
        raw, signature, _, _ = catalog_fixture(self.private_key, sequence=7)
        self.queue_catalog(raw, signature, etag='"seven"')
        manager = self.manager()

        catalog = manager.load_catalog()

        self.assertEqual(catalog.sequence, 7)
        self.assertEqual(catalog.source, "network")
        self.assertEqual(manager.cached_catalog_path.read_bytes(), raw)
        self.assertEqual(manager.cached_signature_path.read_bytes(), signature)
        self.assertEqual(
            manager.installed_registry()["highest_catalog_sequence"], 7
        )
        self.assertEqual(
            json.loads(manager.cache_metadata_path.read_text(encoding="utf-8"))["etag"],
            '"seven"',
        )

    def test_etag_not_modified_reuses_and_reverifies_cache(self):
        raw, signature, _, _ = catalog_fixture(self.private_key)
        self.queue_catalog(raw, signature, etag='"one"')
        manager = self.manager()
        manager.load_catalog()
        self.fetcher.add(CATALOG_URL, FetchResponse(304, {"ETag": '"one"'}))

        cached = manager.load_catalog()

        self.assertEqual(cached.source, "cache")
        self.assertEqual(self.fetcher.calls[-1], (CATALOG_URL, '"one"'))

    def test_network_failure_uses_only_authenticated_unexpired_cache(self):
        raw, signature, _, _ = catalog_fixture(self.private_key)
        self.queue_catalog(raw, signature)
        manager = self.manager()
        manager.load_catalog()
        self.fetcher.add(CATALOG_URL, NetworkUnavailableError("offline"))

        cached = manager.load_catalog()

        self.assertEqual(cached.source, "offline-cache")

        manager.cached_catalog_path.write_bytes(raw + b" ")
        self.fetcher.add(CATALOG_URL, NetworkUnavailableError("offline"))
        with self.assertRaises(SignatureVerificationError):
            manager.load_catalog()

    def test_offline_without_cache_fails_closed(self):
        self.fetcher.add(CATALOG_URL, NetworkUnavailableError("offline"))
        with self.assertRaises(NetworkUnavailableError):
            self.manager().load_catalog()

    def test_invalid_signature_never_falls_back_to_preexisting_cache(self):
        first_raw, first_signature, _, _ = catalog_fixture(self.private_key)
        self.queue_catalog(first_raw, first_signature)
        manager = self.manager()
        manager.load_catalog()

        other_key = Ed25519PrivateKey.generate()
        raw, signature, _, _ = catalog_fixture(other_key, sequence=2)
        self.queue_catalog(raw, signature, etag='"two"')
        with self.assertRaises(SignatureVerificationError):
            manager.load_catalog()

    def test_signature_threshold_requires_distinct_trusted_keys(self):
        second_key = Ed25519PrivateKey.generate()
        second_public = second_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        raw, _, _, _ = catalog_fixture(self.private_key)
        signatures = [
            {
                "key_id": "release-key",
                "algorithm": "ed25519",
                "signature": base64.b64encode(self.private_key.sign(raw)).decode("ascii"),
            },
            {
                "key_id": "backup-key",
                "algorithm": "ed25519",
                "signature": base64.b64encode(second_key.sign(raw)).decode("ascii"),
            },
        ]
        envelope = json.dumps(
            {"schema_version": 1, "signatures": signatures}, sort_keys=True
        ).encode("utf-8")
        self.queue_catalog(raw, envelope)
        manager = self.manager(
            trusted_keys={
                "release-key": self.public_key,
                "backup-key": second_public,
            },
            signature_threshold=2,
        )
        self.assertEqual(manager.load_catalog().sequence, 1)

        duplicate_envelope = json.dumps(
            {"schema_version": 1, "signatures": [signatures[0], signatures[0]]},
            sort_keys=True,
        ).encode("utf-8")
        other_fetcher = FakeFetcher()
        other_fetcher.add(CATALOG_URL, FetchResponse(200, {}, raw))
        other_fetcher.add(SIGNATURE_URL, FetchResponse(200, {}, duplicate_envelope))
        with self.assertRaises(SignatureVerificationError):
            self.manager(
                fetcher=other_fetcher,
                root=self.root / "duplicate",
                trusted_keys={
                    "release-key": self.public_key,
                    "backup-key": second_public,
                },
                signature_threshold=2,
            ).load_catalog()

    def test_empty_trust_configuration_fails_closed(self):
        raw, signature, _, _ = catalog_fixture(self.private_key)
        self.queue_catalog(raw, signature)
        empty_trust = self.root / "empty-trust.json"
        empty_trust.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "algorithm": "ed25519",
                    "threshold": 1,
                    "keys": {},
                }
            ),
            encoding="utf-8",
        )
        manager = ComponentUpdateManager(
            CATALOG_URL,
            signature_url=SIGNATURE_URL,
            root=self.root,
            fetcher=self.fetcher,
            clock=lambda: FIXED_NOW,
            trust_config_path=empty_trust,
        )
        with self.assertRaises(TrustConfigurationError):
            manager.load_catalog()

    def test_expired_catalog_is_rejected_even_with_valid_signature(self):
        raw, signature, _, _ = catalog_fixture(
            self.private_key, expires_at="2026-08-08T11:59:59Z"
        )
        self.queue_catalog(raw, signature)
        with self.assertRaises(CatalogExpiredError):
            self.manager().load_catalog()

    def test_catalog_schema_and_github_https_urls_are_strict(self):
        cases = [
            lambda payload: payload.update({"extra": True}),
            lambda payload: payload["components"]["builder"].update(
                {"url": "http://github.com/unsafe.zip"}
            ),
            lambda payload: payload["components"]["builder"].update(
                {"url": "https://example.com/unsafe.zip"}
            ),
            lambda payload: payload["components"]["builder"].update(
                {"entrypoint": "../Builder.exe"}
            ),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                root = self.root / str(index)
                fetcher = FakeFetcher()
                raw, signature, _, _ = catalog_fixture(
                    self.private_key, payload_mutator=mutate
                )
                fetcher.add(CATALOG_URL, FetchResponse(200, {}, raw))
                fetcher.add(SIGNATURE_URL, FetchResponse(200, {}, signature))
                with self.assertRaises(CatalogError):
                    self.manager(fetcher=fetcher, root=root).load_catalog()

    def test_catalog_sequence_rollback_and_same_sequence_replacement_are_rejected(self):
        raw, signature, _, _ = catalog_fixture(self.private_key, sequence=2)
        self.queue_catalog(raw, signature)
        manager = self.manager()
        manager.load_catalog()

        older_raw, older_signature, _, _ = catalog_fixture(
            self.private_key, sequence=1
        )
        self.queue_catalog(older_raw, older_signature)
        with self.assertRaises(RollbackProtectionError):
            manager.load_catalog()

        changed_raw, changed_signature, _, _ = catalog_fixture(
            self.private_key, sequence=2, versions={"builder": "0.1"}
        )
        self.queue_catalog(changed_raw, changed_signature)
        with self.assertRaises(RollbackProtectionError):
            manager.load_catalog()

    def _load_and_install(self, *, sequence=1, versions=None, manager=None):
        raw, signature, payload, archives = catalog_fixture(
            self.private_key, sequence=sequence, versions=versions
        )
        self.queue_catalog(raw, signature, etag=f'"{sequence}"')
        manager = manager or self.manager()
        catalog = manager.load_catalog()
        self.queue_archive(payload, archives, "builder")
        installed = manager.install_component(catalog, "builder")
        return manager, catalog, installed, payload, archives

    def test_streamed_install_verifies_and_atomically_activates_component(self):
        manager, catalog, installed, payload, archives = self._load_and_install()

        self.assertEqual(installed.version, "0.0")
        component_root = manager.root / installed.relative_path
        self.assertEqual(
            (component_root / installed.entrypoint).read_bytes(), b"builder-0.0"
        )
        pointer = json.loads(
            manager.active_pointer_path("builder").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["current"]["sha256"], installed.sha256)
        self.assertIsNone(pointer["previous"])
        self.assertEqual(
            manager.installed_registry()["components"]["builder"]["version"],
            "0.0",
        )
        self.assertFalse(any(manager.staging_dir.iterdir()))
        self.assertFalse(manager.available_updates(catalog)["builder"])

        repeated = manager.install_component(catalog, "builder")
        self.assertEqual(repeated, installed)

    def test_download_size_content_length_and_hash_are_all_verified(self):
        cases = ("short", "header", "hash")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                root = self.root / case
                fetcher = FakeFetcher()
                raw, signature, payload, archives = catalog_fixture(self.private_key)
                fetcher.add(CATALOG_URL, FetchResponse(200, {}, raw))
                fetcher.add(SIGNATURE_URL, FetchResponse(200, {}, signature))
                manager = self.manager(fetcher=fetcher, root=root)
                catalog = manager.load_catalog()
                definition = payload["components"]["builder"]
                if case == "short":
                    response = FetchResponse(200, {}, archives["builder"][:-1])
                elif case == "header":
                    response = FetchResponse(
                        200,
                        {"Content-Length": str(len(archives["builder"]) + 1)},
                        archives["builder"],
                    )
                else:
                    tampered = bytearray(archives["builder"])
                    tampered[-1] ^= 1
                    response = FetchResponse(
                        200,
                        {"Content-Length": str(len(tampered))},
                        bytes(tampered),
                    )
                fetcher.add(definition["url"], response)
                with self.assertRaises(DownloadVerificationError):
                    manager.install_component(catalog, "builder")
                self.assertIsNone(manager.get_installed_component("builder"))

    def test_archive_rejects_traversal_case_collision_and_symlink(self):
        symlink = zipfile.ZipInfo("linked.exe")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = {
            "traversal": archive_bytes(
                "Builder.exe", extra_entries=[("../escaped.txt", b"bad")]
            ),
            "case_collision": archive_bytes(
                "Builder.exe", extra_entries=[("builder.EXE", b"other")]
            ),
            "symlink": archive_bytes(
                "Builder.exe", extra_entries=[(symlink, b"Builder.exe")]
            ),
            "device_name": archive_bytes(
                "Builder.exe", extra_entries=[("payload/NUL.txt", b"bad")]
            ),
            "alternate_stream": archive_bytes(
                "Builder.exe", extra_entries=[("payload/file.txt:stream", b"bad")]
            ),
            "trailing_dot": archive_bytes(
                "Builder.exe", extra_entries=[("payload/trailing.", b"bad")]
            ),
        }
        for name, malicious_archive in cases.items():
            with self.subTest(name=name):
                root = self.root / name
                fetcher = FakeFetcher()
                raw, signature, payload, archives = catalog_fixture(
                    self.private_key,
                    archive_overrides={"builder": malicious_archive},
                )
                fetcher.add(CATALOG_URL, FetchResponse(200, {}, raw))
                fetcher.add(SIGNATURE_URL, FetchResponse(200, {}, signature))
                manager = self.manager(fetcher=fetcher, root=root)
                catalog = manager.load_catalog()
                fetcher.add(
                    payload["components"]["builder"]["url"],
                    FetchResponse(
                        200,
                        {"Content-Length": str(len(malicious_archive))},
                        malicious_archive,
                    ),
                )
                with self.assertRaises(UnsafeComponentArchiveError):
                    manager.install_component(catalog, "builder")
                self.assertFalse((root / "escaped.txt").exists())

    def test_archive_member_and_expanded_size_limits_are_enforced(self):
        large_archive = archive_bytes("Builder.exe", b"x" * 64)
        raw, signature, payload, _ = catalog_fixture(
            self.private_key, archive_overrides={"builder": large_archive}
        )
        self.queue_catalog(raw, signature)
        manager = self.manager(max_member_bytes=32, max_expanded_bytes=128)
        catalog = manager.load_catalog()
        self.fetcher.add(
            payload["components"]["builder"]["url"],
            FetchResponse(
                200,
                {"Content-Length": str(len(large_archive))},
                large_archive,
            ),
        )
        with self.assertRaises(UnsafeComponentArchiveError):
            manager.install_component(catalog, "builder")

    def test_missing_signed_entrypoint_is_rejected(self):
        wrong_archive = archive_bytes("Wrong.exe")
        raw, signature, payload, _ = catalog_fixture(
            self.private_key, archive_overrides={"builder": wrong_archive}
        )
        self.queue_catalog(raw, signature)
        manager = self.manager()
        catalog = manager.load_catalog()
        self.fetcher.add(
            payload["components"]["builder"]["url"],
            FetchResponse(
                200,
                {"Content-Length": str(len(wrong_archive))},
                wrong_archive,
            ),
        )
        with self.assertRaises(UnsafeComponentArchiveError):
            manager.install_component(catalog, "builder")

    def test_component_descriptor_must_match_signed_catalog(self):
        mismatched = archive_bytes(
            "Builder.exe", component="runner", version="0.0"
        )
        raw, signature, payload, _ = catalog_fixture(
            self.private_key, archive_overrides={"builder": mismatched}
        )
        self.queue_catalog(raw, signature)
        manager = self.manager()
        catalog = manager.load_catalog()
        self.fetcher.add(
            payload["components"]["builder"]["url"],
            FetchResponse(
                200,
                {"Content-Length": str(len(mismatched))},
                mismatched,
            ),
        )
        with self.assertRaises(UnsafeComponentArchiveError):
            manager.install_component(catalog, "builder")

    def test_previous_version_can_be_rolled_back_atomically(self):
        manager, _, first, _, _ = self._load_and_install(
            sequence=1, versions={"builder": "0.1"}
        )

        raw, signature, payload, archives = catalog_fixture(
            self.private_key, sequence=2, versions={"builder": "0.2"}
        )
        self.queue_catalog(raw, signature, etag='"two"')
        second_catalog = manager.load_catalog()
        self.queue_archive(payload, archives, "builder")
        second = manager.install_component(second_catalog, "builder")
        self.assertEqual(second.version, "0.2")

        rolled_back = manager.rollback_component("builder")

        self.assertEqual(rolled_back.version, first.version)
        self.assertEqual(
            manager.installed_registry()["components"]["builder"]["version"],
            "0.1",
        )
        self.assertTrue((manager.root / second.relative_path).exists())

    def test_catalog_cannot_downgrade_an_installed_component(self):
        manager, _, _, _, _ = self._load_and_install(
            sequence=1, versions={"builder": "1.0"}
        )
        raw, signature, _, _ = catalog_fixture(
            self.private_key, sequence=2, versions={"builder": "0.9"}
        )
        self.queue_catalog(raw, signature, etag='"two"')
        with self.assertRaises(ComponentDowngradeError):
            manager.load_catalog()

    def test_same_version_cannot_be_replaced_by_different_bytes(self):
        manager, _, _, _, _ = self._load_and_install()
        changed = archive_bytes("Builder.exe", b"changed")
        raw, signature, payload, archives = catalog_fixture(
            self.private_key,
            sequence=2,
            archive_overrides={"builder": changed},
        )
        self.queue_catalog(raw, signature, etag='"two"')
        catalog = manager.load_catalog()
        self.queue_archive(payload, archives, "builder")
        with self.assertRaises(RollbackProtectionError):
            manager.install_component(catalog, "builder")

    def test_minimum_bootstrap_version_is_enforced_before_download(self):
        raw, signature, _, _ = catalog_fixture(
            self.private_key, min_bootstrap={"builder": "0.1"}
        )
        self.queue_catalog(raw, signature)
        manager = self.manager(bootstrap_version="0.0")
        catalog = manager.load_catalog()
        with self.assertRaises(CompatibilityError):
            manager.install_component(catalog, "builder")

    def test_cross_process_lock_has_bounded_wait(self):
        manager = self.manager(lock_timeout=0.05)
        contender = self.manager(lock_timeout=0.05)
        errors = []

        with manager._update_lock():
            thread = threading.Thread(
                target=lambda: self._capture_lock_error(contender, errors)
            )
            thread.start()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], UpdateLockError)

    @staticmethod
    def _capture_lock_error(manager, errors):
        try:
            with manager._update_lock():
                pass
        except Exception as exc:
            errors.append(exc)

    def test_corrupt_registry_is_not_silently_discarded(self):
        manager = self.manager()
        manager.registry_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(InstallStateError):
            manager.installed_registry()


if __name__ == "__main__":
    unittest.main()
