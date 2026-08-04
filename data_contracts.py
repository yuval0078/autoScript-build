"""Canonical AutoScript data-contract identifiers and helper functions.

The schema versions here describe persisted data structures. They are separate
from the desktop application's release version in ``project_version.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4


@dataclass(frozen=True)
class DataContract:
    """One versioned persisted-data contract."""

    schema_name: str
    schema_version: str
    schema_filename: str


EXPERIMENT_PACKAGE_CONTRACT = DataContract(
    schema_name="autoscript.experiment-package",
    schema_version="1.0.0",
    schema_filename="experiment-package.schema.json",
)
RAW_RUN_CONTRACT = DataContract(
    schema_name="autoscript.raw-run",
    schema_version="1.0.0",
    schema_filename="raw-run.schema.json",
)
TRAINABLE_EXPORT_CONTRACT = DataContract(
    schema_name="autoscript.trainable-export",
    schema_version="1.0.0",
    schema_filename="trainable-export.schema.json",
)
ANALYSIS_EXPORT_CONTRACT = DataContract(
    schema_name="autoscript.analysis-export",
    schema_version="1.0.0",
    schema_filename="analysis-export.schema.json",
)

ALL_DATA_CONTRACTS = (
    EXPERIMENT_PACKAGE_CONTRACT,
    RAW_RUN_CONTRACT,
    TRAINABLE_EXPORT_CONTRACT,
    ANALYSIS_EXPORT_CONTRACT,
)


def new_contract_id() -> str:
    """Return a lowercase UUID4 string suitable for persisted identities."""

    return str(uuid4())


def is_contract_id(value: object) -> bool:
    """Return whether ``value`` is a canonical lowercase UUID string."""

    if not isinstance(value, str) or value != value.lower():
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def utc_now_rfc3339() -> str:
    """Return the current UTC timestamp in RFC 3339 form with trailing ``Z``."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
