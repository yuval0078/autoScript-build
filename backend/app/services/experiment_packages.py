import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator


class PackageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BlockPackageMetadata:
    name: str
    app_version: str | None
    schema_version: str | None
    expected_word_count: int
    grid_rows: int
    grid_cols: int


def _expected_word_count(config):
    """Mirror the Runner's deterministic prompt-count rules without shuffling."""
    groups = config.get("groups", [])
    if config.get("order") == "stiff":
        known_ids = {
            word.get("id")
            for group in groups
            for word in group.get("words", [])
        }
        return sum(word_id in known_ids for word_id in config.get("sequence", []))

    repetitions = config.get("repetitions", {})
    return sum(
        len(group.get("words", [])) * int(repetitions.get(group.get("name", ""), 1))
        for group in groups
    )


def _schema_path():
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = parent / "schemas" / "data-contracts" / "experiment-package.schema.json"
        if candidate.is_file():
            return candidate
    raise RuntimeError("Experiment-package schema is not available to the API.")


def _safe_member_path(member_name):
    if not member_name or "\\" in member_name or re.match(r"^[A-Za-z]:", member_name):
        return False
    path = PurePosixPath(member_name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _is_symlink(info):
    unix_mode = info.external_attr >> 16
    return (unix_mode & 0o170000) == 0o120000


def validate_block_package(package_path, max_uncompressed_bytes):
    try:
        archive = zipfile.ZipFile(package_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackageValidationError("The uploaded file is not a valid ZIP archive.") from exc

    with archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        if not files:
            raise PackageValidationError("The block package is empty.")

        for info in archive.infolist():
            if not _safe_member_path(info.filename) or _is_symlink(info):
                raise PackageValidationError(
                    f"Unsafe path in block package: {info.filename!r}."
                )

        total_uncompressed = sum(info.file_size for info in files)
        if total_uncompressed > max_uncompressed_bytes:
            raise PackageValidationError("The uncompressed block package is too large.")

        root_json_files = [
            info
            for info in files
            if len(PurePosixPath(info.filename).parts) == 1
            and PurePosixPath(info.filename).suffix.lower() == ".json"
        ]
        if len(root_json_files) != 1:
            raise PackageValidationError(
                "A block package must contain exactly one JSON file at its root."
            )

        config_info = root_json_files[0]
        if config_info.file_size > 10 * 1024 * 1024:
            raise PackageValidationError("The block JSON file is too large.")
        try:
            config = json.loads(archive.read(config_info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackageValidationError("The block JSON is not valid UTF-8 JSON.") from exc

        with _schema_path().open("r", encoding="utf-8") as schema_file:
            schema = json.load(schema_file)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(config),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.absolute_path) or "root"
            raise PackageValidationError(
                f"Block JSON does not match the data contract at {location}: {first.message}"
            )

        member_names = {info.filename for info in files}
        missing_media = [
            item["path"] for item in config["files"] if item["path"] not in member_names
        ]
        if missing_media:
            raise PackageValidationError(
                f"Block package is missing referenced media: {missing_media[0]}."
            )

        return BlockPackageMetadata(
            name=config["name"],
            app_version=config.get("app_version"),
            schema_version=config.get("schema_version"),
            expected_word_count=_expected_word_count(config),
            grid_rows=int(config["grid"]["rows"]),
            grid_cols=int(config["grid"]["cols"]),
        )


# Compatibility for code written before package ZIPs were renamed to blocks.
ExperimentPackageMetadata = BlockPackageMetadata
validate_experiment_package = validate_block_package
