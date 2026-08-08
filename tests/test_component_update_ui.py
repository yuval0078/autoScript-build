import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

import autoscript_launcher
from component_runtime import component_launch_command, installed_component_entrypoint
from component_updates_dialog import ComponentUpdatesDialog
from component_versions import COMPONENT_NAMES


class FakeManager:
    def __init__(self, root, installed=None, latest=None):
        self.root = Path(root)
        self.components_dir = self.root / "components"
        self.components_dir.mkdir(parents=True, exist_ok=True)
        self.installed = dict(installed or {})
        versions = {name: "0.0" for name in COMPONENT_NAMES} | (latest or {})
        self.catalog = SimpleNamespace(
            sequence=1,
            source="network",
            components={
                name: SimpleNamespace(version=version) for name, version in versions.items()
            },
        )
        self.install_calls = []

    def get_installed_component(self, component):
        version = self.installed.get(component)
        if version is None:
            return None
        return SimpleNamespace(
            version=version,
            relative_path=f"components/{component}/{version}",
            entrypoint=f"{component.title()}.exe",
        )

    def load_catalog(self):
        return self.catalog

    def install_component(self, catalog, component):
        self.install_calls.append((catalog.sequence, component))
        version = catalog.components[component].version
        self.installed[component] = version
        return self.get_installed_component(component)


class ComponentUpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_shows_bundled_interface_and_missing_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = FakeManager(temporary, latest={"builder": "0.1"})
            dialog = ComponentUpdatesDialog(manager=manager)
            dialog.catalog = manager.catalog
            dialog._render_rows()
            try:
                interface = dialog.row_snapshot("interface")
                builder = dialog.row_snapshot("builder")
                self.assertEqual(interface["installed"], "0.0 (bundled)")
                self.assertEqual(interface["status"], "Up to date")
                self.assertEqual(builder["installed"], "Not installed")
                self.assertEqual(builder["latest"], "0.1")
                self.assertEqual(builder["action"], "Install")
                self.assertTrue(builder["enabled"])
            finally:
                dialog.close()

    def test_install_updates_one_component_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = FakeManager(temporary, latest={"runner": "0.2"})
            dialog = ComponentUpdatesDialog(manager=manager)
            dialog.catalog = manager.catalog
            dialog._render_rows()
            installed = []
            dialog.component_installed.connect(
                lambda component, version: installed.append((component, version))
            )
            try:
                with patch("component_updates_dialog.QMessageBox.information"):
                    dialog.install_component("runner")
                self.assertEqual(manager.install_calls, [(1, "runner")])
                self.assertEqual(installed, [("runner", "0.2")])
                self.assertEqual(dialog.row_snapshot("runner")["status"], "Up to date")
                self.assertEqual(dialog.row_snapshot("builder")["status"], "Not installed")
            finally:
                dialog.close()

    def test_runtime_prefers_managed_component_and_keeps_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = FakeManager(root, installed={"builder": "0.1"})
            managed = root / "components" / "builder" / "0.1" / "Builder.exe"
            managed.parent.mkdir(parents=True)
            managed.write_bytes(b"managed")
            self.assertEqual(
                installed_component_entrypoint("builder", manager), managed.resolve()
            )
            command = component_launch_command(
                "builder", ["--new"], manager=manager, frozen=True,
                executable=root / "AutoScriptLauncher.exe",
                legacy_executable="Builder.exe",
            )
            self.assertEqual(command, [str(managed.resolve()), "--new"])

            empty = FakeManager(root / "empty")
            legacy = empty.root / "Builder.exe"
            legacy.write_bytes(b"legacy")
            fallback = component_launch_command(
                "builder", ["--new"], manager=empty, frozen=True,
                executable=empty.root / "AutoScriptLauncher.exe",
                legacy_executable="Builder.exe",
            )
            self.assertEqual(fallback[1:], ["--new"])
            self.assertTrue(Path(fallback[0]).samefile(legacy))

    def test_launcher_delegates_without_embedding_credentials(self):
        with patch(
            "autoscript_launcher.component_launch_command",
            return_value=["AutoScriptInterface.exe", "--example"],
        ), patch("autoscript_launcher.subprocess.call", return_value=0) as call:
            exit_code = autoscript_launcher.launch_interface(["--example"])
        self.assertEqual(exit_code, 0)
        call.assert_called_once_with(["AutoScriptInterface.exe", "--example"])


if __name__ == "__main__":
    unittest.main()
