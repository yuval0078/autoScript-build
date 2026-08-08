import os
import tempfile
import unittest
import json
import zipfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from builder_workspace import ExperimentBuilderWorkspace


class FakeParent:
    def __init__(self):
        self.saved_id = None

    def start_new_block(self, workspace):
        pass

    def start_edit_block(self, workspace, key, path):
        pass

    def experiment_saved(self, experiment_id):
        self.saved_id = experiment_id

    def show_main_menu(self, refresh=False):
        pass


class FakeApi:
    def __init__(self):
        self.uploads = []
        self.reorders = []
        self.deleted = []

    def create_experiment(self, name):
        return {"id": "experiment-1", "name": name, "blocks": []}

    def update_experiment(self, experiment_id, name):
        return {"id": experiment_id, "name": name}

    def upload_block(self, experiment_id, package_path, block_name, position):
        block_id = f"uploaded-{len(self.uploads) + 1}"
        self.uploads.append((experiment_id, block_name, position, Path(package_path)))
        return {
            "id": block_id,
            "experiment_id": experiment_id,
            "name": block_name,
            "position": position,
            "original_filename": Path(package_path).name,
            "sha256": "a" * 64,
            "size_bytes": Path(package_path).stat().st_size,
            "download_url": f"/api/v1/blocks/{block_id}/download",
        }

    def reorder_blocks(
        self,
        experiment_id,
        block_ids,
        same_page_block_ids=None,
    ):
        self.reorders.append(
            (
                experiment_id,
                list(block_ids),
                list(same_page_block_ids or []),
            )
        )

    def delete_block(self, block_id):
        self.deleted.append(block_id)


class BuilderWorkspaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_new_experiment_saves_blocks_in_visible_drag_order(self):
        parent = FakeParent()
        workspace = ExperimentBuilderWorkspace(parent)
        workspace.api = FakeApi()

        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / "first.zip"
            second = Path(temp_dir) / "second.zip"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            workspace.blocks = {
                "first": {
                    "key": "first",
                    "id": None,
                    "name": "First",
                    "local_path": str(first),
                    "dirty": True,
                },
                "second": {
                    "key": "second",
                    "id": None,
                    "name": "Second",
                    "local_path": str(second),
                    "dirty": True,
                },
            }
            workspace._refresh_blocks()
            moved = workspace.block_list.takeItem(1)
            workspace.block_list.insertItem(0, moved)
            workspace.name_input.setText("Parent Experiment")
            workspace.save_experiment()

        self.assertEqual(
            [upload[1] for upload in workspace.api.uploads],
            ["Second", "First"],
        )
        self.assertEqual(
            workspace.api.reorders,
            [("experiment-1", ["uploaded-1", "uploaded-2"], [])],
        )
        self.assertEqual(parent.saved_id, "experiment-1")
        workspace.deleteLater()

    def test_cloud_experiment_blocks_are_loaded_by_position(self):
        parent = FakeParent()
        workspace = ExperimentBuilderWorkspace(parent)
        workspace.load_experiment(
            {
                "id": "experiment-2",
                "name": "Ordered",
                "blocks": [
                    {
                        "id": "b2",
                        "name": "Second",
                        "position": 1,
                        "same_page_as_previous": True,
                    },
                    {"id": "b1", "name": "First", "position": 0},
                ],
            }
        )
        names = [
            workspace.blocks[workspace.block_list.item(row).data(Qt.UserRole)]["name"]
            for row in range(workspace.block_list.count())
        ]
        self.assertEqual(names, ["First", "Second"])
        second_key = workspace.block_list.item(1).data(Qt.UserRole)
        self.assertTrue(workspace.blocks[second_key]["same_page_as_previous"])
        self.assertEqual(workspace.name_input.text(), "Ordered")
        workspace.deleteLater()

    def test_same_page_layout_is_validated_and_saved_with_block_order(self):
        parent = FakeParent()
        workspace = ExperimentBuilderWorkspace(parent)
        workspace.api = FakeApi()
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            with patch("builder_workspace.user_data_dir", return_value=temp_root):
                for filename, name in (("first.zip", "First"), ("second.zip", "Second")):
                    package_path = temp_root / filename
                    with zipfile.ZipFile(package_path, "w") as archive:
                        archive.writestr(
                            f"{name}.json",
                            json.dumps(
                                {
                                    "name": name,
                                    "grid": {"rows": 1, "cols": 2},
                                    "order": "stiff",
                                    "sequence": [name],
                                    "files": [],
                                }
                            ),
                        )
                    workspace.add_or_replace_block(package_path, name)

                second_key = workspace.block_list.item(1).data(Qt.UserRole)
                workspace.toggle_same_page(second_key)
                self.assertTrue(
                    workspace.blocks[second_key]["same_page_as_previous"]
                )

                workspace.name_input.setText("Shared Page")
                workspace.save_experiment()

        self.assertEqual(
            workspace.api.reorders,
            [
                (
                    "experiment-1",
                    ["uploaded-1", "uploaded-2"],
                    ["uploaded-2"],
                )
            ],
        )
        workspace.deleteLater()

    def test_same_page_layout_rejects_blocks_that_exceed_grid_capacity(self):
        workspace = ExperimentBuilderWorkspace(FakeParent())
        workspace.blocks = {
            "first": {
                "key": "first",
                "id": None,
                "name": "First",
                "dirty": True,
                "same_page_as_previous": False,
                "layout_metrics": {"rows": 1, "cols": 1, "word_count": 1},
            },
            "second": {
                "key": "second",
                "id": None,
                "name": "Second",
                "dirty": True,
                "same_page_as_previous": False,
                "layout_metrics": {"rows": 1, "cols": 1, "word_count": 1},
            },
        }
        workspace._refresh_blocks()

        with patch("builder_workspace.QMessageBox.information") as show_error:
            workspace.toggle_same_page("second")

        self.assertFalse(workspace.blocks["second"]["same_page_as_previous"])
        self.assertIn("cannot share a page", show_error.call_args.args[2])
        workspace.deleteLater()

    def test_upload_blocks_accepts_multiple_legacy_zips_in_selected_order(self):
        parent = FakeParent()
        workspace = ExperimentBuilderWorkspace(parent)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            package_paths = []
            for filename, block_name in (
                ("second.zip", "Second Block"),
                ("first.zip", "First Block"),
            ):
                package_path = temp_root / filename
                with zipfile.ZipFile(package_path, "w") as archive:
                    archive.writestr(
                        f"{package_path.stem}.json",
                        json.dumps({"name": block_name, "files": []}),
                    )
                package_paths.append(str(package_path))

            with (
                patch(
                    "builder_workspace.QFileDialog.getOpenFileNames",
                    return_value=(package_paths, "ZIP Files (*.zip)"),
                ),
                patch("builder_workspace.user_data_dir", return_value=temp_root),
            ):
                workspace.upload_block_zip()

            self.assertEqual(workspace.block_list.count(), 2)
            blocks = [
                workspace.blocks[workspace.block_list.item(row).data(Qt.UserRole)]
                for row in range(workspace.block_list.count())
            ]
            self.assertEqual(
                [block["name"] for block in blocks],
                ["Second Block", "First Block"],
            )
            self.assertTrue(all(Path(block["local_path"]).is_file() for block in blocks))
            self.assertTrue(all(block["dirty"] for block in blocks))
        workspace.deleteLater()

    def test_upload_blocks_rejects_the_whole_batch_when_one_zip_is_invalid(self):
        parent = FakeParent()
        workspace = ExperimentBuilderWorkspace(parent)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            valid_path = temp_root / "valid.zip"
            invalid_path = temp_root / "invalid.zip"
            with zipfile.ZipFile(valid_path, "w") as archive:
                archive.writestr(
                    "valid.json",
                    json.dumps({"name": "Valid Block", "files": []}),
                )
            invalid_path.write_bytes(b"not a zip")

            with (
                patch(
                    "builder_workspace.QFileDialog.getOpenFileNames",
                    return_value=(
                        [str(valid_path), str(invalid_path)],
                        "ZIP Files (*.zip)",
                    ),
                ),
                patch("builder_workspace.user_data_dir", return_value=temp_root),
                patch("builder_workspace.QMessageBox.critical") as show_error,
            ):
                workspace.upload_block_zip()

            self.assertEqual(workspace.block_list.count(), 0)
            self.assertEqual(workspace.blocks, {})
            self.assertIn("invalid.zip", show_error.call_args.args[2])
        workspace.deleteLater()


if __name__ == "__main__":
    unittest.main()
