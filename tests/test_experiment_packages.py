import hashlib
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_utils import UnsafeArchiveError
from experiment_packages import (
    BlockSource,
    ExperimentPackageError,
    build_experiment_bundle,
    inspect_block,
    unpack_experiment_package,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = REPO_ROOT / "schemas" / "data-contracts"


def block_config(name="Block One", **metadata):
    config = {
        "app_version": "1.0.3.1",
        "name": name,
        "grid": {"rows": 1, "cols": 1},
        "order": "stiff",
        "sequence": ["word-1"],
        "repetitions": {"group-a": 1},
        "active_block_sequence": [["group-a", 1]],
        "proceed_condition": {"type": "key", "delay_ms": 0},
        "beeps": {
            "before": {"enabled": False, "delay_ms": 0},
            "after": {"enabled": False, "delay_ms": 0},
        },
        "files": [
            {
                "file_name": "word.wav",
                "path": "media/word.wav",
                "original_name": "word.wav",
                "owner_group": "group-a",
                "auto_slice_word": True,
            }
        ],
        "groups": [
            {
                "name": "group-a",
                "words": [
                    {
                        "id": "word-1",
                        "text": "test",
                        "source_file": "word.wav",
                        "start_ms": 0,
                        "end_ms": 250,
                    }
                ],
            }
        ],
    }
    config.update(metadata)
    return config


def write_block(path, name="Block One", **metadata):
    config = block_config(name, **metadata)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}.json", json.dumps(config, ensure_ascii=False))
        archive.writestr("media/word.wav", b"RIFF-block-audio")
    return path


class ExperimentPackageTests(unittest.TestCase):
    def test_legacy_experiment_zip_is_inspected_as_one_block(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            block_path = write_block(Path(temp_dir) / "legacy.zip", "Legacy Name")

            inspected = inspect_block(block_path)

            self.assertEqual(inspected.name, "Legacy Name")
            self.assertEqual(inspected.package_type, "block")
            self.assertIsNone(inspected.block_id)
            self.assertIsNone(inspected.schema_version)
            self.assertEqual(inspected.sha256, hashlib.sha256(block_path.read_bytes()).hexdigest())

    def test_new_block_metadata_is_optional_and_backwards_compatible(self):
        schema = json.loads(
            (SCHEMA_ROOT / "experiment-package.schema.json").read_text(
                encoding="utf-8"
            )
        )
        legacy = block_config("Legacy")
        modern = block_config(
            "Modern",
            schema_version="1.0",
            package_type="block",
            block_id="block-123",
        )
        legacy_without_version = block_config("Older Legacy")
        legacy_without_version.pop("app_version")
        legacy_without_version["files"][0].pop("auto_slice_word")
        legacy_without_version["files"][0].pop("owner_group")

        self.assertFalse(
            set(schema["required"])
            & {"schema_version", "package_type", "block_id", "app_version"}
        )
        media_required = set(schema["$defs"]["mediaFile"]["required"])
        self.assertNotIn("auto_slice_word", media_required)
        self.assertNotIn("owner_group", media_required)
        self.assertEqual(schema["properties"]["package_type"]["const"], "block")
        self.assertTrue(set(schema["required"]).issubset(legacy))
        self.assertTrue(set(schema["required"]).issubset(modern))
        self.assertTrue(set(schema["required"]).issubset(legacy_without_version))

    def test_bundle_is_schema_valid_ordered_and_byte_deterministic(self):
        bundle_schema = json.loads(
            (SCHEMA_ROOT / "experiment-bundle.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = write_block(root / "first.zip", "First")
            second = write_block(
                root / "second.zip",
                "Second",
                schema_version="1.0",
                package_type="block",
                block_id="second-id",
            )
            output_a = root / "experiment-a.zip"
            output_b = root / "experiment-b.zip"
            sources = [
                first,
                BlockSource(
                    second,
                    block_id="second-id",
                    same_page_as_previous=True,
                ),
            ]

            build_experiment_bundle(
                output_a,
                "Ordered Experiment",
                sources,
                experiment_id="experiment-id",
            )
            build_experiment_bundle(
                output_b,
                "Ordered Experiment",
                sources,
                experiment_id="experiment-id",
            )

            self.assertEqual(output_a.read_bytes(), output_b.read_bytes())
            with zipfile.ZipFile(output_a) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "experiment.json",
                        "blocks/001_First.zip",
                        "blocks/002_Second.zip",
                    ],
                )
                manifest = json.loads(archive.read("experiment.json"))
                self.assertEqual(
                    set(bundle_schema["required"]),
                    {"schema_version", "package_type", "name", "blocks"},
                )
                self.assertEqual(manifest["package_type"], "experiment")
                self.assertEqual(
                    [entry["name"] for entry in manifest["blocks"]],
                    ["First", "Second"],
                )
                self.assertEqual(
                    [entry["same_page_as_previous"] for entry in manifest["blocks"]],
                    [False, True],
                )
                self.assertEqual(archive.read(manifest["blocks"][0]["path"]), first.read_bytes())
                self.assertEqual(archive.read(manifest["blocks"][1]["path"]), second.read_bytes())

            resolved = unpack_experiment_package(
                output_a,
                root / "resolved-layout",
            )
            self.assertEqual(
                [block.same_page_as_previous for block in resolved.blocks],
                [False, True],
            )

    def test_unpack_resolves_legacy_and_bundle_to_same_block_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = write_block(root / "first.zip", "First")
            second = write_block(root / "second.zip", "Second")
            bundle = build_experiment_bundle(
                root / "experiment.zip", "Two Blocks", [first, second]
            )

            legacy = unpack_experiment_package(first, root / "legacy-resolved")
            resolved = unpack_experiment_package(bundle, root / "bundle-resolved")

            self.assertEqual(legacy.source_kind, "block")
            self.assertEqual(legacy.name, "First")
            self.assertFalse(legacy.blocks[0].same_page_as_previous)
            self.assertEqual(len(legacy.blocks), 1)
            self.assertTrue(legacy.blocks[0].config_path.is_file())
            self.assertEqual(legacy.blocks[0].archive_path.read_bytes(), first.read_bytes())

            self.assertEqual(resolved.source_kind, "experiment")
            self.assertEqual(resolved.name, "Two Blocks")
            self.assertEqual([block.name for block in resolved.blocks], ["First", "Second"])
            self.assertEqual(
                [block.same_page_as_previous for block in resolved.blocks],
                [False, False],
            )
            self.assertEqual(resolved.blocks[0].archive_path.read_bytes(), first.read_bytes())
            self.assertEqual(resolved.blocks[1].archive_path.read_bytes(), second.read_bytes())
            self.assertTrue(resolved.blocks[1].config_path.is_file())

    def test_unpack_rejects_tampered_nested_block_and_cleans_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            block = write_block(root / "block.zip", "Block")
            bundle = build_experiment_bundle(root / "bundle.zip", "Experiment", [block])
            tampered = root / "tampered.zip"
            with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(tampered, "w") as target:
                for member in source.infolist():
                    payload = source.read(member)
                    if member.filename.startswith("blocks/"):
                        payload += b"tampered"
                    target.writestr(member, payload)
            destination = root / "resolved"

            with self.assertRaisesRegex(ExperimentPackageError, "SHA-256 mismatch"):
                unpack_experiment_package(tampered, destination)

            self.assertFalse(destination.exists())

    def test_outer_and_nested_traversal_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            outer_traversal = root / "outer-traversal.zip"
            with zipfile.ZipFile(outer_traversal, "w") as archive:
                archive.writestr("../outside.txt", "unsafe")
                archive.writestr("legacy.json", json.dumps(block_config("Legacy")))
                archive.writestr("media/word.wav", b"audio")

            nested_traversal = root / "nested-traversal.zip"
            with zipfile.ZipFile(nested_traversal, "w") as archive:
                archive.writestr("block.json", json.dumps(block_config("Nested")))
                archive.writestr("media/word.wav", b"audio")
                archive.writestr("../outside.txt", "unsafe")

            symlink = root / "symlink.zip"
            link_info = zipfile.ZipInfo("media/link.wav")
            link_info.create_system = 3
            link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
            config = block_config("Link")
            config["files"][0]["path"] = "media/link.wav"
            with zipfile.ZipFile(symlink, "w") as archive:
                archive.writestr("link.json", json.dumps(config))
                archive.writestr(link_info, "target.wav")

            for package in (outer_traversal, nested_traversal, symlink):
                with self.subTest(package=package.name):
                    with self.assertRaises(UnsafeArchiveError):
                        unpack_experiment_package(
                            package, root / f"resolved-{package.stem}"
                        )

    def test_traversal_inside_nested_block_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unsafe_block = root / "unsafe-block.zip"
            with zipfile.ZipFile(unsafe_block, "w") as archive:
                archive.writestr("block.json", json.dumps(block_config("Unsafe")))
                archive.writestr("media/word.wav", b"audio")
                archive.writestr("../outside.txt", "unsafe")
            block_bytes = unsafe_block.read_bytes()
            member_path = "blocks/001_Unsafe.zip"
            manifest = {
                "schema_version": "1.0",
                "package_type": "experiment",
                "name": "Unsafe Experiment",
                "blocks": [
                    {
                        "name": "Unsafe",
                        "path": member_path,
                        "sha256": hashlib.sha256(block_bytes).hexdigest(),
                    }
                ],
            }
            bundle = root / "unsafe-bundle.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("experiment.json", json.dumps(manifest))
                archive.writestr(member_path, block_bytes)
            destination = root / "resolved"

            with self.assertRaises(UnsafeArchiveError):
                unpack_experiment_package(bundle, destination)

            self.assertFalse(destination.exists())

    def test_bundle_builder_rejects_manifest_name_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            block = write_block(root / "block.zip", "Actual")
            with self.assertRaisesRegex(ExperimentPackageError, "does not match"):
                build_experiment_bundle(
                    root / "bundle.zip",
                    "Experiment",
                    [BlockSource(block, name="Different")],
                )


if __name__ == "__main__":
    unittest.main()
