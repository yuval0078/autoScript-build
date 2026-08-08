"""Static regression checks for recovered runner behavior."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _source(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _function_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class RunnerRecoveryContractsTests(unittest.TestCase):
    def test_block_session_recalibration_only_occurs_at_block_boundaries(self):
        from gui_menu import build_block_session_layout

        blocks = [
            {
                "config_path": f"block-{index}.json",
                "display_name": f"Block {index}",
                "word_count": 1,
                "rows": 1,
                "cols": 1,
            }
            for index in (1, 2)
        ]
        plan = build_block_session_layout(blocks, recalibrate_between_blocks=True)
        entries = plan["experiments"]
        self.assertFalse(entries[0]["recalibrate_before_start"])
        self.assertTrue(entries[1]["recalibrate_before_start"])
        self.assertTrue(plan["recalibrate_between_pages"])
        self.assertTrue(
            all(not entry["recalibrate_during_page_refresh"] for entry in entries)
        )

    def test_arranged_blocks_can_share_a_page_without_extra_recalibration(self):
        from gui_menu import build_block_session_layout

        blocks = [
            {
                "config_path": f"block-{index}.json",
                "display_name": f"Block {index}",
                "word_count": 1,
                "rows": 1,
                "cols": 2,
            }
            for index in (1, 2, 3)
        ]

        plan = build_block_session_layout(
            blocks,
            recalibrate_between_blocks=True,
            joined_boundaries={0},
        )
        entries = plan["experiments"]

        self.assertFalse(entries[0]["recalibrate_before_start"])
        self.assertTrue(entries[1]["same_page_as_previous"])
        self.assertFalse(entries[1]["recalibrate_before_start"])
        self.assertTrue(entries[2]["recalibrate_before_start"])
        self.assertTrue(
            all(not entry["recalibrate_during_page_refresh"] for entry in entries)
        )

    def test_runner_supports_session_plan_and_cell_offsets(self):
        source = _source("tablet_experiment.py")
        functions = _function_names(source)

        self.assertIn("prepare_experiment_runtime", functions)
        self.assertIn("load_session_plan", functions)
        self.assertIn("apply_session_plan", functions)
        self.assertIn("_current_page_cell_index", functions)
        self.assertIn("finish_current_and_start_next", functions)
        self.assertIn('"--session-plan"', source)
        self.assertIn("start_cell_offset", source)
        self.assertIn("same_page_as_previous", source)

    def test_launcher_builds_and_passes_session_layout(self):
        source = _source("gui_menu.py")
        functions = _function_names(source)
        package_source = _source("experiment_packages.py")
        package_functions = _function_names(package_source)

        self.assertIn("build_session_layout", functions)
        self.assertIn("unpack_experiment_package", package_functions)
        self.assertIn("unpack_experiment_package(file_path, destination)", source)
        self.assertIn("safe_extract_zip(block_archive, extracted_dir)", package_source)
        self.assertIn('source_kind="block"', package_source)
        self.assertIn('source_kind="experiment"', package_source)
        self.assertIn('component_launch_command(', source)
        self.assertIn('"runner",', source)
        self.assertIn('source_script="tablet_experiment.py"', source)
        self.assertIn('legacy_executable="ExperimentRunner.exe"', source)
        self.assertIn('arguments.extend(["--session-plan", str(session_plan_path)])', source)

    def test_local_legacy_runs_use_the_block_arrangement_dialog(self):
        source = _source("gui_menu.py")

        self.assertIn(
            "if len(experiments) > 1 and parent_experiment is None:",
            source,
        )
        self.assertIn(
            "arrange_dialog = ArrangeExperimentsDialog(",
            source,
        )
        self.assertIn('config["block_index"] = final_block_index', source)

    def test_audio_playback_cache_is_present_without_removing_safe_temp_paths(self):
        source = _source("audio_processor.py")
        functions = _function_names(source)

        self.assertIn("_audio_cache_dir", functions)
        self.assertIn("_source_cache_key", functions)
        self.assertIn("get_cached_wav_file", functions)
        self.assertIn("get_playback_file", functions)
        self.assertIn("unique_temp_path", source)


if __name__ == "__main__":
    unittest.main()
