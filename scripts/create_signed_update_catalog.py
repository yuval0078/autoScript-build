"""Create and sign the exact catalog consumed by ComponentUpdateManager."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Direct execution sets sys.path[0] to ``scripts`` rather than the repository
# root.  The release workflow intentionally invokes this file directly, so
# make the shared version contract importable in that mode as well.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from component_versions import COMPONENT_NAMES, parse_component_version


KEY_ID = "release-2026-08"
DEFAULT_VALIDITY_DAYS = 180
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TAG_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _load_artifacts(root: Path):
    components = {}
    source_commits = set()
    for manifest_path in sorted(root.rglob("component-artifacts.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if payload.get("schema_version") != 1:
            raise ValueError(f"Unsupported artifact manifest: {manifest_path}")
        source_commit = payload.get("source_commit")
        if not isinstance(source_commit, str) or not _COMMIT_PATTERN.fullmatch(source_commit):
            raise ValueError(f"Invalid source commit in {manifest_path}")
        source_commits.add(source_commit)
        definitions = payload.get("components")
        if not isinstance(definitions, dict):
            raise ValueError(f"Missing component definitions in {manifest_path}")
        for name, definition in definitions.items():
            if name not in COMPONENT_NAMES or name in components:
                raise ValueError(f"Unexpected or duplicate component {name!r}")
            if not isinstance(definition, dict):
                raise ValueError(f"Invalid artifact definition for {name}")
            version = definition.get("version")
            parse_component_version(version)
            filename = definition.get("filename")
            if not isinstance(filename, str) or not _FILENAME_PATTERN.fullmatch(filename):
                raise ValueError(f"Unsafe artifact filename for {name}")
            archive_path = manifest_path.parent / filename
            if not archive_path.is_file():
                raise ValueError(f"Missing archive for {name}: {archive_path}")
            content = archive_path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if digest != definition.get("sha256") or len(content) != definition.get("size"):
                raise ValueError(f"Artifact checksum or size mismatch for {name}")
            components[name] = {
                "version": version,
                "filename": filename,
                "sha256": digest,
                "size": len(content),
                "entrypoint": definition.get("entrypoint"),
                "protocol": definition.get("protocol"),
            }
    if set(components) != set(COMPONENT_NAMES):
        raise ValueError("Artifacts must define interface, builder, runner, and analyzer exactly once")
    if len(source_commits) != 1:
        raise ValueError("Every component must be built from the same Git commit")
    return components, source_commits.pop()


def create_signed_catalog(
    artifacts_root: Path,
    *,
    repository: str,
    tag: str,
    sequence: int,
    private_seed_b64: str,
    trust_path: Path,
    now: datetime | None = None,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
):
    if not _REPOSITORY_PATTERN.fullmatch(repository):
        raise ValueError("Repository must use the GitHub owner/name form")
    if not _TAG_PATTERN.fullmatch(tag):
        raise ValueError("Release tag contains unsafe characters")
    if isinstance(sequence, bool) or sequence < 1:
        raise ValueError("Catalog sequence must be a positive integer")
    if (
        isinstance(validity_days, bool)
        or not isinstance(validity_days, int)
        or not 1 <= validity_days <= 365
    ):
        raise ValueError("Catalog validity must be between 1 and 365 days")
    components, _source_commit = _load_artifacts(artifacts_root)

    try:
        seed = base64.b64decode(private_seed_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("AUTOSCRIPT_UPDATE_SIGNING_KEY is not valid base64") from exc
    if len(seed) != 32:
        raise ValueError("AUTOSCRIPT_UPDATE_SIGNING_KEY must contain a 32-byte Ed25519 seed")
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    trust = json.loads(trust_path.read_text(encoding="utf-8"))
    configured_key = trust.get("keys", {}).get(KEY_ID, {}).get("public_key")
    try:
        trusted_public_key = base64.b64decode(configured_key, validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError(f"Trust configuration does not contain {KEY_ID}") from exc
    if public_key != trusted_public_key:
        raise ValueError("Signing key does not match the public key pinned in update_trust.json")

    published_at = now or datetime.now(timezone.utc)
    catalog_components = {}
    for name in COMPONENT_NAMES:
        artifact = components[name]
        catalog_components[name] = {
            "version": artifact["version"],
            "url": (
                f"https://github.com/{repository}/releases/download/{tag}/"
                f"{artifact['filename']}"
            ),
            "sha256": artifact["sha256"],
            "size": artifact["size"],
            "entrypoint": artifact["entrypoint"],
            "min_bootstrap": "0.0",
            "protocol": artifact["protocol"],
        }
    catalog = {
        "schema_version": 1,
        "sequence": sequence,
        "channel": "stable",
        "published_at": _rfc3339(published_at),
        "expires_at": _rfc3339(published_at + timedelta(days=validity_days)),
        "components": catalog_components,
    }
    catalog_raw = json.dumps(
        catalog,
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    signature = private_key.sign(catalog_raw)
    envelope = {
        "schema_version": 1,
        "signatures": [
            {
                "key_id": KEY_ID,
                "algorithm": "ed25519",
                "signature": base64.b64encode(signature).decode("ascii"),
            }
        ],
    }
    signature_raw = json.dumps(envelope, sort_keys=True).encode("utf-8")
    return catalog_raw, signature_raw


def main(arguments=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts-root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--sequence", required=True, type=int)
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--output-directory", required=True, type=Path)
    parser.add_argument(
        "--validity-days",
        type=int,
        default=DEFAULT_VALIDITY_DAYS,
        help="Signed catalog lifetime in days (1-365; default: 180)",
    )
    args = parser.parse_args(arguments)

    signing_seed = os.environ.get("AUTOSCRIPT_UPDATE_SIGNING_KEY", "").strip()
    if not signing_seed:
        parser.error(
            "AUTOSCRIPT_UPDATE_SIGNING_KEY is required for a signed draft release"
        )
    catalog_raw, signature_raw = create_signed_catalog(
        args.artifacts_root,
        repository=args.repository,
        tag=args.tag,
        sequence=args.sequence,
        private_seed_b64=signing_seed,
        trust_path=args.trust,
        validity_days=args.validity_days,
    )
    args.output_directory.mkdir(parents=True, exist_ok=True)
    (args.output_directory / "autoscript-update-catalog.json").write_bytes(catalog_raw)
    (args.output_directory / "autoscript-update-catalog.json.sig").write_bytes(
        signature_raw
    )
    print("Created signed update catalog for four verified component archives.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
