from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jsonschema import Draft202012Validator
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from PyQt5.QtWidgets import QMessageBox

from gui_menu import MainMenu, experiment_card_metadata
from runner_launch_contract import (
    read_runtime_session_seed,
    session_seed_path,
    write_runtime_session_seed,
)
from scripts.create_signed_update_catalog import KEY_ID, create_signed_catalog


ROOT = Path(__file__).resolve().parents[1]
SPEC_ROOT = ROOT / "packaging" / "pyinstaller"


class RunnerLaunchContractTests(unittest.TestCase):
    def test_main_menu_exposes_persistent_run_settings(self):
        source = (ROOT / "gui_menu.py").read_text(encoding="utf-8")

        self.assertIn('QSettings("AutoScript", "Interface")', source)
        self.assertIn('"recalibrateBetweenBlocksToggle"', source)
        self.assertIn('"saveResultsLocallyToggle"', source)
        self.assertIn('"run/recalibrate_between_blocks"', source)
        self.assertIn('"run/save_results_locally"', source)

    def test_experiment_card_metadata_pluralizes_blocks_and_distinct_participants(self):
        self.assertEqual(
            experiment_card_metadata(
                {
                    "blocks": [{"id": "block-1"}, {"id": "block-2"}],
                    "participant_count": 3,
                    "analyzed_participant_count": 2,
                }
            ),
            "2 Blocks · 3 Participants (2 analyzed)",
        )
        self.assertEqual(
            experiment_card_metadata(
                {
                    "blocks": [{"id": "block-1"}],
                    "participant_count": 1,
                    "analyzed_participant_count": 1,
                }
            ),
            "1 Block · 1 Participant (1 analyzed)",
        )

    def test_seed_sidecar_round_trip_and_legacy_absence(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "Block config.json"
            config.write_text("{}", encoding="utf-8")
            self.assertIsNone(read_runtime_session_seed(str(config)))
            write_runtime_session_seed(str(config), " session-seed ")
            self.assertEqual(read_runtime_session_seed(str(config)), "session-seed")
            self.assertEqual(
                Path(session_seed_path(str(config))),
                Path(f"{config.resolve()}.autoscript_session_seed"),
            )

    def test_interface_launcher_does_not_import_runner_application(self):
        gui_source = (ROOT / "gui_menu.py").read_text(encoding="utf-8")
        gui_tree = ast.parse(gui_source)
        imported_modules = {
            node.module
            for node in ast.walk(gui_tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertIn("runner_launch_contract", imported_modules)
        self.assertNotIn("tablet_experiment", imported_modules)
        self.assertNotIn("load_experiment_config", gui_source)

        runner_source = (ROOT / "tablet_experiment.py").read_text(encoding="utf-8")
        runner_tree = ast.parse(runner_source)
        defined_functions = {
            node.name
            for node in ast.walk(runner_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("write_runtime_session_seed", defined_functions)
        self.assertNotIn("read_runtime_session_seed", defined_functions)
        self.assertIn("from runner_launch_contract import read_runtime_session_seed", runner_source)

    def test_runner_and_analyzer_use_managed_component_resolution(self):
        manager = object()
        parent = SimpleNamespace(component_manager=manager)
        harness = SimpleNamespace(
            parent=parent,
            _api_child_environment=lambda: {"AUTOSCRIPT_API_TOKEN": "token"},
        )
        harness._offer_component_install = lambda *_args: None

        process = MagicMock()
        with (
            patch("gui_menu.component_launch_command", return_value=["Runner.exe", "arg"]) as resolve,
            patch("gui_menu.subprocess.Popen", return_value=process) as popen,
        ):
            launched = MainMenu.launch_runner(
                harness,
                [ROOT / "block.json"],
                ROOT / "plan.json",
                test_mode=True,
            )
        self.assertIs(launched, process)
        call = resolve.call_args
        self.assertEqual(call.args[0], "runner")
        self.assertIn("--test-mode", call.args[1])
        self.assertEqual(call.kwargs["source_script"], "tablet_experiment.py")
        self.assertEqual(call.kwargs["legacy_executable"], "ExperimentRunner.exe")
        self.assertIs(call.kwargs["manager"], manager)
        popen.assert_called_once_with(
            ["Runner.exe", "arg"],
            env={"AUTOSCRIPT_API_TOKEN": "token"},
        )

        with (
            patch("gui_menu.component_launch_command", return_value=["Analyzer.exe"]) as resolve,
            patch("gui_menu.subprocess.Popen", return_value=process),
        ):
            launched = MainMenu.launch_analyzer(
                harness,
                [ROOT / "result.json"],
                ["--autoscript-context", ROOT / "context.json"],
            )
        self.assertIs(launched, process)
        call = resolve.call_args
        self.assertEqual(call.args[0], "analyzer")
        self.assertEqual(call.kwargs["source_script"], "analyzer_refactored.py")
        self.assertEqual(call.kwargs["legacy_executable"], "Analyzer.exe")
        self.assertIs(call.kwargs["manager"], manager)

    def test_missing_component_offers_updates_menu(self):
        parent = SimpleNamespace(
            component_manager=object(),
            show_component_updates=MagicMock(),
        )
        harness = SimpleNamespace(parent=parent)
        harness._api_child_environment = lambda: {}
        harness._offer_component_install = lambda component, display: MainMenu._offer_component_install(
            harness, component, display
        )
        with (
            patch("gui_menu.component_launch_command", return_value=None),
            patch("gui_menu.QMessageBox.question", return_value=QMessageBox.Yes),
        ):
            self.assertIsNone(MainMenu.launch_analyzer(harness, []))
        parent.show_component_updates.assert_called_once_with()


class ComponentSpecTests(unittest.TestCase):
    EXPECTED = {
        "Interface.spec": ("interface", "AutoScriptInterface.exe", "main_interface.py"),
        "Builder.spec": ("builder", "Builder.exe", "builder_main.py"),
        "Runner.spec": ("runner", "ExperimentRunner.exe", "launch_experiment.py"),
        "Analyzer.spec": ("analyzer", "Analyzer.exe", "launch_analyzer.py"),
    }

    @staticmethod
    def _literal_assignment(tree, name):
        for node in tree.body:
            if isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                    return ast.literal_eval(node.value)
        raise AssertionError(f"Missing assignment {name}")

    def test_specs_are_isolated_single_application_builds(self):
        for filename, (component, entrypoint, entry_script) in self.EXPECTED.items():
            with self.subTest(spec=filename):
                path = SPEC_ROOT / filename
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
                call_names = [
                    node.func.id
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                ]
                self.assertEqual(call_names.count("Analysis"), 1)
                self.assertEqual(call_names.count("EXE"), 1)
                self.assertEqual(call_names.count("COLLECT"), 1)
                self.assertNotIn("MERGE", call_names)
                self.assertEqual(self._literal_assignment(tree, "COMPONENT_ID"), component)
                self.assertEqual(self._literal_assignment(tree, "ENTRYPOINT"), entrypoint)
                self.assertIn(entry_script, source)
                self.assertIn("component_versions.json", source)

    def test_external_bootstrap_contains_launcher_and_initial_interface_only(self):
        source = (SPEC_ROOT / "Launcher.spec").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertEqual(self._literal_assignment(tree, "COMPONENT_ID"), "bootstrap")
        self.assertEqual(
            self._literal_assignment(tree, "ENTRYPOINT"),
            "AutoScriptLauncher.exe",
        )
        self.assertIn("autoscript_launcher.py", source)
        self.assertNotIn("MERGE(", source)
        forbidden = set(self._literal_assignment(tree, "FORBIDDEN_COMPONENT_MODULES"))
        self.assertTrue(
            {"main_interface", "builder_main", "tablet_experiment", "analyzer_refactored"}.issubset(
                forbidden
            )
        )
        build_script = (ROOT / "scripts" / "build_components.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn('"AutoScriptLauncher.exe"', build_script)
        self.assertIn('"AutoScriptInterface.exe"', build_script)
        self.assertIn('"bootstrap.json"', build_script)
        self.assertIn('"AutoScript-bootstrap-', build_script)

    def test_interface_inventory_excludes_builder_runner_and_analyzer(self):
        source = (SPEC_ROOT / "Interface.spec").read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden = set(self._literal_assignment(tree, "FORBIDDEN_COMPONENT_MODULES"))
        self.assertTrue(
            {
                "builder_main",
                "builder_workspace",
                "exp_initializer",
                "tablet_experiment",
                "analyzer_refactored",
                "audio_processor",
                "pygame",
                "pydub",
                "numpy",
            }.issubset(forbidden)
        )
        analysis = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Analysis"
        )
        hidden_imports = next(
            ast.literal_eval(keyword.value)
            for keyword in analysis.keywords
            if keyword.arg == "hiddenimports"
        )
        self.assertTrue(forbidden.isdisjoint(hidden_imports))
        self.assertNotIn("ffmpeg", source.casefold())

    def test_only_builder_and_runner_bundle_ffmpeg(self):
        for filename in ("Builder.spec", "Runner.spec"):
            source = (SPEC_ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("binaries=_ffmpeg_binaries()", source)
            self.assertIn('"assets/bin"', source)
        analyzer_source = (SPEC_ROOT / "Analyzer.spec").read_text(encoding="utf-8")
        self.assertIn("binaries=[]", analyzer_source)
        self.assertNotIn("ffmpeg", analyzer_source.casefold())


class ComponentArtifactContractTests(unittest.TestCase):
    def test_component_descriptor_schema_accepts_build_descriptor(self):
        schema_path = ROOT / "packaging" / "component-descriptor.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(
            {
                "format_version": 1,
                "id": "runner",
                "version": "0.0",
                "platform": "windows",
                "arch": "x86_64",
                "entrypoint": "ExperimentRunner.exe",
                "protocol_version": 1,
                "source_commit": "a" * 40,
            }
        )

    def test_catalog_is_signed_by_the_key_pinned_in_trust_config(self):
        private_key = Ed25519PrivateKey.generate()
        seed = private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, entrypoint in {
                "interface": "AutoScriptInterface.exe",
                "builder": "Builder.exe",
                "runner": "ExperimentRunner.exe",
                "analyzer": "Analyzer.exe",
            }.items():
                directory = root / name
                directory.mkdir()
                archive = directory / f"AutoScript-{name}-0.0-windows-x86_64.zip"
                archive.write_bytes(f"archive-{name}".encode("ascii"))
                digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                (directory / "component-artifacts.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "source_commit": "a" * 40,
                            "components": {
                                name: {
                                    "version": "0.0",
                                    "filename": archive.name,
                                    "sha256": digest,
                                    "size": archive.stat().st_size,
                                    "entrypoint": entrypoint,
                                    "protocol": 1,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            trust = root / "trust.json"
            trust.write_text(
                json.dumps(
                    {
                        "keys": {
                            KEY_ID: {
                                "public_key": base64.b64encode(public_key).decode("ascii")
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog_raw, signature_raw = create_signed_catalog(
                root,
                repository="yuval0078/autoScript-build",
                tag="components-r1",
                sequence=1,
                private_seed_b64=base64.b64encode(seed).decode("ascii"),
                trust_path=trust,
                now=datetime(2026, 8, 8, tzinfo=timezone.utc),
            )
        catalog = json.loads(catalog_raw)
        envelope = json.loads(signature_raw)
        signature = base64.b64decode(envelope["signatures"][0]["signature"])
        private_key.public_key().verify(signature, catalog_raw)
        self.assertEqual(envelope["signatures"][0]["key_id"], "release-2026-08")
        self.assertEqual(set(catalog["components"]), set(self.EXPECTED_COMPONENTS))

    EXPECTED_COMPONENTS = ("interface", "builder", "runner", "analyzer")

    def test_build_script_uses_versions_descriptors_and_archive_checksums(self):
        source = (ROOT / "scripts" / "build_components.ps1").read_text(
            encoding="utf-8"
        )
        for component in ("interface", "builder", "runner", "analyzer"):
            self.assertIn(component, source)
        self.assertIn("component_versions.json", source)
        self.assertIn('"component.json"', source)
        self.assertIn("Get-FileHash", source)
        self.assertIn("SHA256", source)
        self.assertIn("Compress-Archive", source)
        self.assertIn('"component-artifacts.json"', source)

    def test_release_workflow_is_matrixed_and_draft_only(self):
        workflow = (
            ROOT / ".github" / "workflows" / "component-release.yml"
        ).read_text(encoding="utf-8")
        for component in ("interface", "builder", "runner", "analyzer"):
            self.assertIn(f"- {component}", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("create_draft_release", workflow)
        self.assertIn("AUTOSCRIPT_UPDATE_SIGNING_KEY", workflow)
        self.assertIn("create_signed_update_catalog.py", workflow)
        self.assertIn("--draft", workflow)
        self.assertIn("gh release list", workflow)
        self.assertNotIn("gh release view $tag", workflow)
        self.assertNotIn("git push", workflow)
        self.assertNotIn("--latest", workflow)

        # The old all-in-one build remains an explicit migration fallback.
        legacy_spec = (ROOT / "TouchpadExperiment.spec").read_text(encoding="utf-8")
        legacy_workflow = (
            ROOT / ".github" / "workflows" / "portable-build.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("MERGE(", legacy_spec)
        self.assertIn("(a_builder, 'Builder', 'Builder')", legacy_spec)
        self.assertIn("name='Builder'", legacy_spec)
        self.assertIn("Portable release build", legacy_workflow)
        self.assertIn('"TouchpadExperiment/Builder.exe"', legacy_workflow)


if __name__ == "__main__":
    unittest.main()
