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

        self.assertIn("build_session_layout", functions)
        self.assertIn("safe_extract_zip(zip_ref, extract_dir)", source)
        self.assertIn('source_script_path("tablet_experiment.py")', source)
        self.assertIn('command.extend(["--session-plan", str(session_plan_path)])', source)

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
