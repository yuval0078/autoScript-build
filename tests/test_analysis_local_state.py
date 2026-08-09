import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis_local_state import (
    load_local_analysis_state,
    save_local_analysis_state,
    workspace_identity,
)


class LocalAnalysisStateTests(unittest.TestCase):
    def test_identity_follows_bytes_and_order_not_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "old-array.json"
            renamed = root / "renamed.json"
            other = root / "other.json"
            first.write_text('[{"word":"old"}]', encoding="utf-8")
            renamed.write_bytes(first.read_bytes())
            other.write_text('[{"word":"changed"}]', encoding="utf-8")
            self.assertEqual(workspace_identity([first]), workspace_identity([renamed]))
            self.assertNotEqual(workspace_identity([first]), workspace_identity([other]))

    def test_round_trip_and_retention(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            source = base / "legacy.json"
            source.write_text('[{"word":"legacy"}]', encoding="utf-8")
            data_root = base / "data"
            with patch("analysis_local_state.user_data_dir", return_value=data_root):
                for revision in range(4):
                    save_local_analysis_state(
                        [source], {"schema_version": "1.1", "value": revision}, retention=2
                    )
                loaded = load_local_analysis_state([source])
                workspace = next((data_root / "analysis_state" / "local").iterdir())
            self.assertEqual(loaded["state"]["value"], 3)
            self.assertEqual(len(list(workspace.glob("*.state.json"))), 2)


if __name__ == "__main__":
    unittest.main()
