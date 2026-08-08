import shutil
import uuid
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_paths import ensure_dir, user_data_dir
from autoscript_api import APIError, AutoScriptAPI
from experiment_packages import inspect_block


def _safe_filename(value, fallback="block"):
    cleaned = "".join(
        character if character.isalnum() or character in "-_ " else "_"
        for character in str(value)
    ).strip(" ._")
    return cleaned or fallback


def _block_layout_metrics(config):
    """Return the page-capacity values used by the Runner layout planner."""
    grid = config.get("grid", {}) or {}
    try:
        rows = int(grid.get("rows", 0))
        cols = int(grid.get("cols", 0))
    except (TypeError, ValueError):
        rows = cols = 0

    if config.get("order") == "stiff":
        word_count = len(config.get("sequence", []) or [])
    else:
        repetitions = config.get("repetitions", {}) or {}
        word_count = 0
        for group in config.get("groups", []) or []:
            group_name = group.get("name", "")
            try:
                repeat_count = int(repetitions.get(group_name, 1))
            except (TypeError, ValueError):
                repeat_count = 1
            word_count += len(group.get("words", []) or []) * max(0, repeat_count)

    return {
        "rows": rows,
        "cols": cols,
        "word_count": word_count,
    }


class BlockRow(QWidget):
    def __init__(
        self,
        block,
        download_callback,
        edit_callback,
        delete_callback,
        same_page_callback=None,
        is_first=False,
    ):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
        layout.setSpacing(8)

        drag_handle = QLabel("☰")
        drag_handle.setStyleSheet("color: #7b8794; font-size: 18px;")
        drag_handle.setToolTip("Drag to reorder")
        layout.addWidget(drag_handle)

        if is_first:
            page_button = QPushButton("Page start")
            page_button.setToolTip("The first Block always starts a new page")
            page_button.setEnabled(False)
            page_button.setFixedWidth(122)
            page_button.setStyleSheet(
                "QPushButton { color: #667483; background: #eef1f4; border: 1px solid #d4dbe2; "
                "border-radius: 6px; padding: 7px 9px; font-weight: 600; }"
            )
            layout.addWidget(page_button)
        else:
            same_page = bool(block.get("same_page_as_previous", False))
            page_button = QPushButton(
                "⛓  Same page" if same_page else "+  Same page"
            )
            page_button.setToolTip(
                "Start this Block on a new page"
                if same_page
                else "Keep this Block on the same page as the previous Block"
            )
            page_button.setFixedWidth(122)
            page_button.setStyleSheet(
                "QPushButton { color: white; background: #2e7d32; border: 0; "
                "border-radius: 6px; padding: 7px 9px; font-weight: 600; } "
                "QPushButton:hover { background: #256628; }"
                if same_page
                else
                "QPushButton { color: #2e7d32; background: white; border: 1px solid #72a876; "
                "border-radius: 6px; padding: 7px 9px; font-weight: 600; } "
                "QPushButton:hover { background: #edf7ee; }"
            )
            page_button.clicked.connect(same_page_callback)
            layout.addWidget(page_button)

        name = QLabel(block["name"])
        name.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(name, 1)

        status = QLabel("Edited" if block.get("dirty") else "Cloud")
        status.setStyleSheet(
            "color: #b85c00; font-size: 11px;" if block.get("dirty")
            else "color: #617080; font-size: 11px;"
        )
        if not block.get("id"):
            status.setText("New")
        layout.addWidget(status)

        for text, tooltip, callback, color in (
            ("↓", "Download ZIP", download_callback, "#2463a8"),
            ("✎", "Edit block", edit_callback, "#6b4ca5"),
            ("✕", "Delete block", delete_callback, "#c62828"),
        ):
            button = QToolButton()
            button.setText(text)
            button.setToolTip(tooltip)
            button.setFixedSize(36, 34)
            button.setStyleSheet(
                f"QToolButton {{ color: {color}; font-size: 19px; border: 1px solid #d8dee6; "
                "border-radius: 6px; background: white; }} "
                "QToolButton:hover { background: #eef4fb; border-color: #9fb4cc; }"
            )
            button.clicked.connect(callback)
            layout.addWidget(button)


class ExperimentBuilderWorkspace(QWidget):
    """Parent Builder screen that owns an experiment and its ordered Blocks."""

    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.api = AutoScriptAPI()
        self.experiment_id = None
        self.blocks = {}
        self._remote_block_ids = set()
        self._dirty = False
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(38, 28, 38, 28)
        root.setSpacing(18)

        header = QHBoxLayout()
        title_area = QVBoxLayout()
        title = QLabel("Experiment Builder")
        title.setStyleSheet("font-size: 28px; font-weight: 700; color: #172230;")
        subtitle = QLabel("An experiment contains one or more ordered Blocks.")
        subtitle.setStyleSheet("color: #647382; font-size: 13px;")
        title_area.addWidget(title)
        title_area.addWidget(subtitle)
        header.addLayout(title_area)
        header.addStretch()

        cancel = QPushButton("Back to Experiments")
        cancel.clicked.connect(self._request_close)
        header.addWidget(cancel)
        root.addLayout(header)

        details = QFrame()
        details.setObjectName("experimentDetails")
        details.setStyleSheet(
            "QFrame#experimentDetails { background: #f7f9fc; border: 1px solid #dce3eb; "
            "border-radius: 10px; }"
        )
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(18, 14, 18, 14)
        details_layout.addWidget(QLabel("Experiment name"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Enter the experiment name")
        self.name_input.setMinimumHeight(38)
        self.name_input.textChanged.connect(self._mark_dirty)
        details_layout.addWidget(self.name_input)
        root.addWidget(details)

        list_header = QHBoxLayout()
        blocks_title = QLabel("Blocks")
        blocks_title.setStyleSheet("font-size: 19px; font-weight: 650;")
        list_header.addWidget(blocks_title)
        list_header.addStretch()

        self.upload_block_button = QPushButton("↑  Upload Blocks")
        self.upload_block_button.setToolTip(
            "Upload one or more Block or legacy experiment ZIP files"
        )
        self.upload_block_button.setStyleSheet(
            "QPushButton { background: white; color: #315f88; border: 1px solid #aac0d4; "
            "border-radius: 7px; padding: 9px 17px; font-weight: 600; } "
            "QPushButton:hover { background: #edf5fb; border-color: #6f99bd; }"
        )
        self.upload_block_button.clicked.connect(self.upload_block_zip)
        list_header.addWidget(self.upload_block_button)

        add_block = QPushButton("+  Add Block")
        add_block.setToolTip("Add a new block")
        add_block.setStyleSheet(
            "QPushButton { background: #2d74b8; color: white; border: 0; border-radius: 7px; "
            "padding: 10px 18px; font-weight: 600; } "
            "QPushButton:hover { background: #245f98; }"
        )
        add_block.clicked.connect(lambda: self.parent.start_new_block(self))
        list_header.addWidget(add_block)
        root.addLayout(list_header)

        self.block_list = QListWidget()
        self.block_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.block_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.block_list.setDefaultDropAction(Qt.MoveAction)
        self.block_list.setSpacing(5)
        self.block_list.setStyleSheet(
            "QListWidget { background: #f3f6f9; border: 1px solid #d5dde6; border-radius: 9px; "
            "padding: 7px; } QListWidget::item { background: white; border: 1px solid #e0e5eb; "
            "border-radius: 7px; } QListWidget::item:selected { border-color: #5f91c5; }"
        )
        self.block_list.model().rowsMoved.connect(self._blocks_reordered)
        root.addWidget(self.block_list, 1)

        footer = QHBoxLayout()
        self.summary = QLabel("No blocks yet")
        self.summary.setStyleSheet("color: #667483;")
        footer.addWidget(self.summary)
        footer.addStretch()
        self.save_button = QPushButton("Save Experiment")
        self.save_button.setMinimumSize(180, 44)
        self.save_button.setStyleSheet(
            "QPushButton { background: #238447; color: white; border: 0; border-radius: 8px; "
            "font-size: 15px; font-weight: 650; padding: 9px 20px; } "
            "QPushButton:hover { background: #1c6c3a; }"
        )
        self.save_button.clicked.connect(self.save_experiment)
        footer.addWidget(self.save_button)
        root.addLayout(footer)

    def new_experiment(self):
        self.experiment_id = None
        self.blocks = {}
        self._remote_block_ids = set()
        self.name_input.blockSignals(True)
        self.name_input.clear()
        self.name_input.blockSignals(False)
        self._dirty = False
        self._refresh_blocks()
        self.name_input.setFocus()

    def load_experiment(self, experiment):
        self.experiment_id = experiment["id"]
        self.blocks = {}
        ordered_blocks = sorted(
            experiment.get("blocks", []),
            key=lambda block: block.get("position", 0),
        )
        for block in ordered_blocks:
            key = str(uuid.uuid4())
            self.blocks[key] = {
                **block,
                "key": key,
                "local_path": None,
                "dirty": False,
                "same_page_as_previous": bool(
                    block.get("same_page_as_previous", False)
                ),
            }
        self._remote_block_ids = {
            str(block["id"]) for block in ordered_blocks if block.get("id")
        }
        self.name_input.blockSignals(True)
        self.name_input.setText(experiment["name"])
        self.name_input.blockSignals(False)
        self._dirty = False
        self._refresh_blocks()

    def _ordered_keys(self):
        return [
            self.block_list.item(row).data(Qt.UserRole)
            for row in range(self.block_list.count())
        ]

    def _refresh_blocks(self):
        existing_order = self._ordered_keys()
        keys = [key for key in existing_order if key in self.blocks]
        keys.extend(key for key in self.blocks if key not in keys)
        if keys:
            self.blocks[keys[0]]["same_page_as_previous"] = False
        self.block_list.clear()
        for position, key in enumerate(keys):
            block = self.blocks[key]
            item = QListWidgetItem()
            item.setData(Qt.UserRole, key)
            row = BlockRow(
                block,
                lambda checked=False, block_key=key: self.download_block(block_key),
                lambda checked=False, block_key=key: self.edit_block(block_key),
                lambda checked=False, block_key=key: self.delete_block(block_key),
                lambda checked=False, block_key=key: self.toggle_same_page(block_key),
                is_first=position == 0,
            )
            item.setSizeHint(row.sizeHint())
            self.block_list.addItem(item)
            self.block_list.setItemWidget(item, row)
        count = len(self.blocks)
        if count == 0:
            self.summary.setText("No blocks yet")
        else:
            page_groups = 1 + sum(
                not self.blocks[key].get("same_page_as_previous", False)
                for key in keys[1:]
            )
            block_label = "1 Block" if count == 1 else f"{count} Blocks"
            page_label = (
                "1 page group" if page_groups == 1 else f"{page_groups} page groups"
            )
            self.summary.setText(f"{block_label} · {page_label}")

    def _mark_dirty(self, *args):
        self._dirty = True

    def _blocks_reordered(self, *args):
        keys = self._ordered_keys()
        if keys:
            self.blocks[keys[0]]["same_page_as_previous"] = False
        self._dirty = True
        QTimer.singleShot(0, self._refresh_blocks)

    def _layout_metrics(self, key):
        block = self.blocks[key]
        cached = block.get("layout_metrics")
        if cached is not None:
            return cached
        package_path = self._materialize_block(key)
        metrics = _block_layout_metrics(inspect_block(package_path).config)
        block["layout_metrics"] = metrics
        return metrics

    @staticmethod
    def _cluster_bounds(keys, joined_boundaries, item_index):
        start = item_index
        while start > 0 and (start - 1) in joined_boundaries:
            start -= 1
        end = item_index
        while end < len(keys) - 1 and end in joined_boundaries:
            end += 1
        return start, end

    def _can_join_boundary(self, keys, boundary_index):
        if boundary_index < 0 or boundary_index >= len(keys) - 1:
            return False
        joined_boundaries = {
            index - 1
            for index, key in enumerate(keys)
            if index > 0 and self.blocks[key].get("same_page_as_previous", False)
        }
        left_start, _ = self._cluster_bounds(
            keys, joined_boundaries, boundary_index
        )
        _, right_end = self._cluster_bounds(
            keys, joined_boundaries, boundary_index + 1
        )
        metrics = [
            self._layout_metrics(key)
            for key in keys[left_start:right_end + 1]
        ]
        rows = metrics[0]["rows"] if metrics else 0
        cols = metrics[0]["cols"] if metrics else 0
        if rows <= 0 or cols <= 0:
            return False
        if any(item["rows"] != rows or item["cols"] != cols for item in metrics):
            return False
        return sum(item["word_count"] for item in metrics) <= rows * cols

    def toggle_same_page(self, key):
        keys = self._ordered_keys()
        try:
            index = keys.index(key)
        except ValueError:
            return
        if index == 0:
            return
        block = self.blocks[key]
        if block.get("same_page_as_previous", False):
            block["same_page_as_previous"] = False
        else:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                can_join = self._can_join_boundary(keys, index - 1)
            except Exception as exc:
                QMessageBox.critical(self, "Page Layout Failed", str(exc))
                return
            finally:
                QApplication.restoreOverrideCursor()
            if not can_join:
                previous = self.blocks[keys[index - 1]]
                QMessageBox.information(
                    self,
                    "Blocks Cannot Share a Page",
                    f"‘{previous['name']}’ and ‘{block['name']}’ cannot share a page.\n\n"
                    "Blocks must use the same grid, and the combined words must fit "
                    "within that grid.",
                )
                return
            block["same_page_as_previous"] = True
        self._dirty = True
        self._refresh_blocks()

    def _validate_page_layout(self, ordered_keys):
        if ordered_keys:
            self.blocks[ordered_keys[0]]["same_page_as_previous"] = False
        for index, key in enumerate(ordered_keys[1:], start=1):
            if not self.blocks[key].get("same_page_as_previous", False):
                continue
            if not self._can_join_boundary(ordered_keys, index - 1):
                previous = self.blocks[ordered_keys[index - 1]]["name"]
                current = self.blocks[key]["name"]
                raise ValueError(
                    f"Blocks ‘{previous}’ and ‘{current}’ no longer fit on the same page."
                )

    def upload_block_zip(self):
        package_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Upload Blocks",
            "",
            "ZIP Files (*.zip)",
        )
        if not package_paths:
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            inspected_blocks = []
            for package_path in package_paths:
                try:
                    inspection = inspect_block(package_path)
                except Exception as exc:
                    raise ValueError(f"{Path(package_path).name}: {exc}") from exc
                inspected_blocks.append((package_path, inspection))

            for package_path, inspection in inspected_blocks:
                self.add_or_replace_block(
                    package_path,
                    inspection.name,
                    inspection=inspection,
                )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Upload Blocks Failed",
                f"The selected ZIP files could not be uploaded:\n\n{exc}",
            )
        finally:
            QApplication.restoreOverrideCursor()

    def add_or_replace_block(
        self,
        package_path,
        block_name,
        editing_key=None,
        inspection=None,
    ):
        inspection = inspection or inspect_block(package_path)
        layout_metrics = _block_layout_metrics(inspection.config)
        drafts = ensure_dir(user_data_dir() / "builder_drafts")
        stored_path = drafts / f"{uuid.uuid4().hex}_{_safe_filename(block_name)}.zip"
        shutil.copy2(package_path, stored_path)
        if editing_key and editing_key in self.blocks:
            old = self.blocks[editing_key]
            self.blocks[editing_key] = {
                **old,
                "name": block_name,
                "local_path": str(stored_path),
                "dirty": True,
                "layout_metrics": layout_metrics,
            }
        else:
            key = str(uuid.uuid4())
            self.blocks[key] = {
                "key": key,
                "id": None,
                "name": block_name,
                "local_path": str(stored_path),
                "dirty": True,
                "original_filename": stored_path.name,
                "same_page_as_previous": False,
                "layout_metrics": layout_metrics,
            }
        self._dirty = True
        self._refresh_blocks()

    def _materialize_block(self, key):
        block = self.blocks[key]
        local_path = block.get("local_path")
        if local_path and Path(local_path).is_file():
            return Path(local_path)
        if not block.get("id"):
            raise FileNotFoundError("The local Block ZIP could not be found.")
        downloads = ensure_dir(user_data_dir() / "builder_downloads")
        destination = downloads / f"{block['id']}_{_safe_filename(block['name'])}.zip"
        self.api.download_block(block, destination)
        block["local_path"] = str(destination)
        return destination

    def download_block(self, key):
        block = self.blocks[key]
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Download Block ZIP",
            f"{_safe_filename(block['name'])}.zip",
            "ZIP Files (*.zip)",
        )
        if not save_path:
            return
        try:
            source = self._materialize_block(key)
            destination = Path(save_path).with_suffix(".zip")
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
        except Exception as exc:
            QMessageBox.critical(self, "Download Failed", str(exc))

    def edit_block(self, key):
        try:
            package_path = self._materialize_block(key)
            self.parent.start_edit_block(self, key, package_path)
        except Exception as exc:
            QMessageBox.critical(self, "Edit Block Failed", str(exc))

    def delete_block(self, key):
        block = self.blocks[key]
        answer = QMessageBox.warning(
            self,
            "Delete Block",
            f"Delete the Block ‘{block['name']}’ from this experiment?\n\n"
            "The cloud copy is removed when you save the experiment.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.blocks.pop(key)
        self._dirty = True
        self._refresh_blocks()

    def save_experiment(self):
        experiment_name = self.name_input.text().strip()
        if not experiment_name:
            QMessageBox.warning(self, "Missing Name", "Enter an experiment name.")
            return
        ordered_keys = self._ordered_keys()
        if not ordered_keys:
            QMessageBox.warning(self, "No Blocks", "Add at least one Block before saving.")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        original_text = self.save_button.text()
        self.save_button.setEnabled(False)
        try:
            self._validate_page_layout(ordered_keys)
            if self.experiment_id is None:
                experiment = self.api.create_experiment(experiment_name)
                self.experiment_id = experiment["id"]
            else:
                self.api.update_experiment(self.experiment_id, experiment_name)

            final_block_ids = []
            replaced_ids = set()
            for position, key in enumerate(ordered_keys):
                block = self.blocks[key]
                if block.get("dirty") or not block.get("id"):
                    same_page_as_previous = bool(
                        block.get("same_page_as_previous", False)
                    )
                    package_path = self._materialize_block(key)
                    uploaded = self.api.upload_block(
                        self.experiment_id,
                        package_path,
                        block["name"],
                        position,
                    )
                    if block.get("id"):
                        replaced_ids.add(str(block["id"]))
                    block.update(uploaded)
                    block["same_page_as_previous"] = same_page_as_previous
                    block["dirty"] = False
                final_block_ids.append(str(block["id"]))

            removed_ids = self._remote_block_ids.difference(final_block_ids)
            removed_ids.update(replaced_ids)
            for block_id in removed_ids:
                self.api.delete_block(block_id)
            same_page_block_ids = [
                str(self.blocks[key]["id"])
                for key in ordered_keys[1:]
                if self.blocks[key].get("same_page_as_previous", False)
            ]
            self.api.reorder_blocks(
                self.experiment_id,
                final_block_ids,
                same_page_block_ids=same_page_block_ids,
            )

            self._remote_block_ids = set(final_block_ids)
            self._dirty = False
            self.save_button.setText("Saved")
            QApplication.processEvents()
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
            return
        finally:
            self.save_button.setText(original_text)
            self.save_button.setEnabled(True)
            QApplication.restoreOverrideCursor()

        self.parent.experiment_saved(self.experiment_id)

    def _request_close(self):
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Discard Changes",
                "Return to the experiment list and discard unsaved changes?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.parent.show_main_menu(refresh=True)
