from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication, QWidget

import builder_main
import main_interface


ROOT = Path(__file__).resolve().parents[1]


class FakeApi:
    def __init__(self, experiments=None):
        self.experiments = list(experiments or [])

    def list_experiments(self):
        return list(self.experiments)

    def get_experiment(self, experiment_id):
        for experiment in self.experiments:
            if str(experiment.get("id")) == str(experiment_id):
                return dict(experiment)
        raise RuntimeError("experiment not found")


class FakeWorkspace(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.api = None
        self.blocks = {}
        self.new_calls = 0
        self.loaded = None
        self.added = None

    def new_experiment(self):
        self.new_calls += 1
        self.blocks = {}

    def load_experiment(self, experiment):
        self.loaded = experiment

    def add_or_replace_block(self, package_path, block_name, editing_key=None):
        self.added = (package_path, block_name, editing_key)


class FakeWizard(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.reset_calls = 0
        self.imported = None

    def reset_draft(self):
        self.reset_calls += 1

    def import_experiment_zip(self, path, replace_without_prompt=False):
        self.imported = (path, replace_without_prompt)
        return True


class FakeProperties(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.defaults = None
        self.data = None

    def reset_defaults(self, value):
        self.defaults = value

    def set_data(self, audio_groups, loaded_properties=None):
        self.data = (audio_groups, loaded_properties)


class FakeMenu(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.refresh_count = 0

    def refresh_experiments(self):
        self.refresh_count += 1


class FakeResults(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.experiment = None

    def set_experiment(self, experiment):
        self.experiment = experiment


class BuilderProcessSeparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _builder(self, *, experiment_id=None, experiments=None):
        with (
            patch.object(builder_main, "ExperimentBuilderWorkspace", FakeWorkspace),
            patch.object(builder_main, "NewExperimentWizard", FakeWizard),
            patch.object(builder_main, "ExperimentPropertiesPage", FakeProperties),
        ):
            return builder_main.BuilderInterface(
                experiment_id=experiment_id,
                api=FakeApi(experiments),
                authenticate=False,
            )

    def test_main_has_no_builder_ui_imports(self):
        source = (ROOT / "main_interface.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("builder_workspace", imported_modules)
        self.assertNotIn("exp_initializer", imported_modules)
        self.assertNotIn("ExperimentBuilderWorkspace", source)
        self.assertNotIn("NewExperimentWizard", source)
        self.assertNotIn("ExperimentPropertiesPage", source)

    def test_builder_commands_are_component_process_arguments(self):
        expected = [
            "C:/Python/python.exe",
            str(ROOT / "builder_main.py"),
            "--experiment-id",
            "experiment-1",
        ]
        with patch.object(
            main_interface,
            "component_launch_command",
            return_value=expected,
        ) as resolve:
            source_command = main_interface.builder_launch_command(
                ["--experiment-id", "experiment-1"],
                frozen=False,
                executable="C:/Python/python.exe",
            )
        self.assertEqual(source_command, expected)
        resolve.assert_called_once_with(
            "builder",
            ["--experiment-id", "experiment-1"],
            source_script="builder_main.py",
            legacy_executable="Builder.exe",
            manager=None,
            frozen=False,
            executable="C:/Python/python.exe",
        )

    def test_builder_token_is_environment_only(self):
        with patch.object(main_interface, "get_session_token", return_value="secret-token"):
            environment = main_interface.builder_child_environment({"PATH": "test"})
        self.assertEqual(environment["AUTOSCRIPT_API_TOKEN"], "secret-token")
        command = main_interface.builder_launch_command(
            ["--new"], frozen=False, executable=sys.executable
        )
        self.assertNotIn("secret-token", command)

        with patch.object(main_interface, "get_session_token", return_value=None):
            environment = main_interface.builder_child_environment(
                {"AUTOSCRIPT_API_TOKEN": "stale"}
            )
        self.assertNotIn("AUTOSCRIPT_API_TOKEN", environment)

    def test_standalone_builder_loads_by_id_and_owns_edit_callbacks(self):
        experiment = {"id": "experiment-1", "name": "Example", "blocks": []}
        window = self._builder(
            experiment_id="experiment-1",
            experiments=[experiment],
        )
        try:
            self.assertEqual(window.builder_workspace.loaded, experiment)
            self.assertIs(window.builder_workspace.api, window.api)

            window.builder_workspace.blocks = {"one": {}}
            window.start_new_block(window.builder_workspace)
            self.assertEqual(window.experiment_properties.defaults, "Block 2")
            self.assertIs(window.stack.currentWidget(), window.new_experiment)

            window.start_edit_block(
                window.builder_workspace,
                "block-key",
                ROOT / "block.zip",
            )
            self.assertEqual(
                window.new_experiment.imported,
                (str(ROOT / "block.zip"), True),
            )
            window.finish_block_edit(ROOT / "edited.zip", "Edited")
            self.assertEqual(
                window.builder_workspace.added,
                (ROOT / "edited.zip", "Edited", "block-key"),
            )
        finally:
            window.close()

    def test_builder_save_closes_and_main_refreshes_after_child_exit(self):
        builder = self._builder()
        try:
            with (
                patch.object(builder, "close") as close,
                patch.object(
                    builder_main.QTimer,
                    "singleShot",
                    side_effect=lambda _delay, callback: callback(),
                ),
            ):
                builder.experiment_saved("experiment-9")
            self.assertEqual(builder.saved_experiment_id, "experiment-9")
            close.assert_called_once_with()
        finally:
            builder.close()

        with (
            patch.object(main_interface.MainInterface, "_authenticate_if_required", return_value=None),
            patch.object(main_interface, "MainMenu", FakeMenu),
            patch.object(main_interface, "ExperimentResultsPage", FakeResults),
            patch.object(main_interface.QTimer, "singleShot"),
        ):
            main_window = main_interface.MainInterface()
        process = MagicMock()
        process.poll.return_value = 0
        try:
            with (
                patch.object(
                    main_interface,
                    "builder_launch_command",
                    return_value=[sys.executable, str(ROOT / "builder_main.py"), "--new"],
                ),
                patch.object(main_interface.subprocess, "Popen", return_value=process) as popen,
                patch.object(
                    main_interface,
                    "builder_child_environment",
                    return_value={"AUTOSCRIPT_API_TOKEN": "token"},
                ),
            ):
                launched = main_window.open_new_experiment()
            self.assertIs(launched, process)
            popen.assert_called_once_with(
                [sys.executable, str(ROOT / "builder_main.py"), "--new"],
                env={"AUTOSCRIPT_API_TOKEN": "token"},
            )

            main_window._poll_builder_processes()
            self.assertEqual(main_window.builder_processes, [])
            self.assertEqual(main_window.main_menu.refresh_count, 1)
        finally:
            main_window.builder_process_timer.stop()
            main_window.close()

    def test_builder_cli_modes_are_mutually_exclusive(self):
        self.assertTrue(builder_main.parse_args(["--new"]).new)
        self.assertEqual(
            builder_main.parse_args(["--experiment-id", "experiment-2"]).experiment_id,
            "experiment-2",
        )
        with self.assertRaises(SystemExit):
            builder_main.parse_args(["--new", "--experiment-id", "experiment-2"])


if __name__ == "__main__":
    unittest.main()
