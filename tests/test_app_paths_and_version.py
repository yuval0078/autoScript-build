import os
import re
import tempfile
import unittest
from pathlib import Path

import project_version
from app_paths import app_dir, source_script_path

RELEASE_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?$")


class AppPathsAndVersionTests(unittest.TestCase):
    def test_source_entry_scripts_are_absolute_and_exist(self):
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            os.chdir(temporary_directory)
            try:
                for script_name in ("tablet_experiment.py", "analyzer_refactored.py"):
                    script_path = source_script_path(script_name)
                    self.assertTrue(script_path.is_absolute())
                    self.assertEqual(script_path.parent, app_dir())
                    self.assertTrue(script_path.is_file(), script_path)
            finally:
                os.chdir(original_cwd)

    def test_project_version_uses_supported_release_versioning(self):
        self.assertRegex(project_version.APP_VERSION, RELEASE_VERSION_PATTERN)
        self.assertEqual(project_version.APP_VERSION, "1.0.3.1")
        self.assertEqual(
            project_version.APP_VERSION_LABEL,
            f"Version {project_version.APP_VERSION}",
        )
        self.assertEqual(
            project_version.RELEASE_TAG,
            f"v{project_version.APP_VERSION}",
        )
        self.assertEqual(
            project_version.PORTABLE_ARCHIVE_NAME,
            f"TouchpadExperiment-{project_version.RELEASE_TAG}-portable.zip",
        )


if __name__ == "__main__":
    unittest.main()
