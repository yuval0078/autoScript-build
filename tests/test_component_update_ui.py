import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

import autoscript_launcher
from component_runtime import component_launch_command, installed_component_entrypoint
from component_updates_dialog import ComponentUpdateCoordinator, ComponentUpdatesDialog
from component_versions import COMPONENT_NAMES, COMPONENT_VERSIONS


class FakeManager:
    def __init__(self, root, installed=None, latest=None):
        self.root = Path(root)
        self.components_dir = self.root / "components"
        self.components_dir.mkdir(parents=True, exist_ok=True)
        self.installed = dict(installed or {})
        versions = dict(COMPONENT_VERSIONS) | (latest or {})
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

    def install_component(self, catalog, component, progress=None):
        self.install_calls.append((catalog.sequence, component))
        if progress is not None:
            progress(35)
        version = catalog.components[component].version
        self.installed[component] = version
        if progress is not None:
            progress(100)
        return self.get_installed_component(component)


class ComponentUpdateUiTests(unittest.TestCase):
    def test_runner_requires_a_verified_compatible_installation(self):
        from component_update_manager import CompatibilityError
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Runner.exe").touch()
            for installed in ({}, {"runner": "0.0"}):
                with self.subTest(installed=installed), self.assertRaises(CompatibilityError):
                    component_launch_command("runner", manager=FakeManager(root, installed),
                        frozen=True, executable=root / "Interface.exe",
                        legacy_executable="Runner.exe", minimum_version="0.1")
            manager = FakeManager(root, {"runner": "0.1"})
            entrypoint = root / "components/runner/0.1/Runner.exe"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.touch()
            command = component_launch_command("runner", ["--test-run"], manager=manager,
                frozen=True, minimum_version="0.1")
            self.assertEqual(command, [str(entrypoint), "--test-run"])

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
                self.assertEqual(
                    interface["installed"],
                    f"{COMPONENT_VERSIONS['interface']} (bundled)",
                )
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

            class ImmediateThread:
                def __init__(self, *, target, **_kwargs):
                    self.target = target

                def start(self):
                    self.target()

            try:
                with (
                    patch("component_updates_dialog.QMessageBox.information"),
                    patch("component_updates_dialog.threading.Thread", ImmediateThread),
                ):
                    dialog.install_component("runner")
                self.assertEqual(manager.install_calls, [(1, "runner")])
                self.assertEqual(installed, [("runner", "0.2")])
                self.assertEqual(dialog.row_snapshot("runner")["status"], "Up to date")
                self.assertEqual(dialog.row_snapshot("runner")["progress"], 100)
                self.assertEqual(dialog.row_snapshot("builder")["status"], "Not installed")
            finally:
                dialog.close()

    def test_non_interface_update_continues_after_dialog_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = FakeManager(temporary, latest={"runner": "0.2"})
            started = threading.Event()
            release = threading.Event()
            original_install = manager.install_component

            def blocking_install(catalog, component, progress=None):
                if progress is not None:
                    progress(25)
                started.set()
                release.wait(timeout=5)
                return original_install(catalog, component, progress)

            manager.install_component = blocking_install
            coordinator = ComponentUpdateCoordinator(manager)
            dialog = ComponentUpdatesDialog(
                manager=manager, coordinator=coordinator
            )
            dialog.catalog = manager.catalog
            dialog.show()
            try:
                dialog.install_component("runner")
                self.assertTrue(started.wait(timeout=2))
                self.app.processEvents()
                self.assertTrue(coordinator.is_installing("runner"))
                self.assertEqual(dialog.progress_bar.value(), 25)

                dialog.accept()
                self.app.processEvents()
                self.assertFalse(dialog.isVisible())
                self.assertTrue(coordinator.is_installing("runner"))

                release.set()
                coordinator._thread.join(timeout=2)
                self.app.processEvents()
                self.assertFalse(coordinator.is_installing())
                self.assertEqual(manager.installed["runner"], "0.2")
            finally:
                release.set()
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
