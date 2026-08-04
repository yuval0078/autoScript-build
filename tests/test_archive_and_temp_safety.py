import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from archive_utils import UnsafeArchiveError, safe_extract_zip, unique_temp_path


class SafeZipExtractionTests(unittest.TestCase):
    def test_extracts_normal_nested_files(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            archive_path = root / "normal.zip"
            destination = root / "output"

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("experiment/config.json", "{}")
                archive.writestr("experiment/audio/word.wav", b"audio")

            safe_extract_zip(archive_path, destination)

            self.assertEqual(
                (destination / "experiment" / "config.json").read_text(encoding="utf-8"),
                "{}",
            )
            self.assertEqual(
                (destination / "experiment" / "audio" / "word.wav").read_bytes(),
                b"audio",
            )

    def test_rejects_parent_directory_traversal_before_extracting(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            archive_path = root / "traversal.zip"
            destination = root / "output"
            escaped_path = root / "escaped.txt"

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("safe.txt", "safe")
                archive.writestr("../escaped.txt", "escaped")

            with self.assertRaises(UnsafeArchiveError):
                safe_extract_zip(archive_path, destination)

            self.assertFalse(escaped_path.exists())
            self.assertFalse((destination / "safe.txt").exists())

    def test_rejects_absolute_drive_and_symlink_entries(self):
        unsafe_names = ("/absolute.txt", r"C:\\drive.txt")

        for unsafe_name in unsafe_names:
            with self.subTest(unsafe_name=unsafe_name):
                with tempfile.TemporaryDirectory() as temp_root:
                    root = Path(temp_root)
                    archive_path = root / "unsafe.zip"
                    with zipfile.ZipFile(archive_path, "w") as archive:
                        archive.writestr(unsafe_name, "unsafe")

                    with self.assertRaises(UnsafeArchiveError):
                        safe_extract_zip(archive_path, root / "output")

        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            archive_path = root / "symlink.zip"
            symlink = zipfile.ZipInfo("link")
            symlink.create_system = 3
            symlink.external_attr = (stat.S_IFLNK | 0o777) << 16

            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(symlink, "../outside")

            with self.assertRaises(UnsafeArchiveError):
                safe_extract_zip(archive_path, root / "output")


class UniqueTemporaryPathTests(unittest.TestCase):
    def test_reserves_distinct_paths_with_safe_names(self):
        with tempfile.TemporaryDirectory() as temp_root:
            first = unique_temp_path(temp_root, prefix="preview/main ")
            second = unique_temp_path(temp_root, prefix="preview/main ")

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(first.parent, Path(temp_root))
            self.assertEqual(second.parent, Path(temp_root))
            self.assertEqual(first.suffix, ".wav")
            self.assertNotIn("/", first.name)
            self.assertNotIn("\\", first.name)


if __name__ == "__main__":
    unittest.main()
