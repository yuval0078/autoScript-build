import os
import json
import subprocess
import zipfile
import shutil
import math
import time
import stat
import uuid
from pathlib import Path
from app_paths import ensure_dir, user_data_dir, asset_path
from archive_utils import safe_extract_zip
from autoscript_api import APIError, AutoScriptAPI, get_session_token
from component_runtime import component_launch_command
from experiment_packages import unpack_experiment_package
from runner_launch_contract import write_runtime_session_seed
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QFileDialog, QMessageBox, QApplication,
                             QDialog, QToolButton, QMenu, QGridLayout,
                             QCheckBox, QInputDialog, QFrame, QScrollArea,
                             QLineEdit, QSizePolicy)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QFont, QIcon, QPixmap, QPainter, QColor


def _normalize_experiment_payload(payload):
    """Return the experiment config payload if this JSON is a saved result wrapper."""
    if not isinstance(payload, dict):
        return {}

    if 'config' in payload and not any(key in payload for key in ('groups', 'properties', 'prompts')):
        config = payload.get('config')
        if isinstance(config, dict):
            return config

    return payload


def _calculate_experiment_info_from_config(config):
    """Return total word count and grid dimensions for any supported config payload."""
    config = _normalize_experiment_payload(config)

    if 'prompts' in config and isinstance(config.get('prompts'), list):
        props = config.get('global_parameters', {}) or {}
        grid = props.get('grid', {}) or {}
        rows = grid.get('rows', 5)
        cols = grid.get('cols', 5)
        order = props.get('order', 'random')
        repetitions = props.get('repetitions', {}) or {}

        if order == 'stiff':
            total_words = len(config.get('sequence', []) or config.get('prompts', []))
        else:
            grouped_counts = {}
            for prompt in config.get('prompts', []):
                group_name = (prompt.get('metadata', {}) or {}).get('group', 'default')
                grouped_counts[group_name] = grouped_counts.get(group_name, 0) + 1

            total_words = 0
            for group_name, count in grouped_counts.items():
                repeat_count = repetitions.get(group_name, 1)
                try:
                    repeat_count = int(repeat_count)
                except Exception:
                    repeat_count = 1
                total_words += count * max(0, repeat_count)

        return total_words, rows, cols

    if 'groups' in config and isinstance(config.get('groups'), list):
        grid = config.get('grid', {}) or {}
        rows = grid.get('rows', 5)
        cols = grid.get('cols', 5)

        order = config.get('order', 'random')
        repetitions = config.get('repetitions', {}) or {}

        if order == 'stiff':
            total_words = len(config.get('sequence', []) or [])
        else:
            total_words = 0
            for group in config.get('groups', []):
                group_name = group.get('name', '')
                group_words = group.get('words', []) or []
                repeat_count = repetitions.get(group_name, 1)
                try:
                    repeat_count = int(repeat_count)
                except Exception:
                    repeat_count = 1
                total_words += len(group_words) * max(0, repeat_count)

        return total_words, rows, cols

    props = config.get('properties', {}) or {}
    grid = props.get('grid', {}) or {}
    rows = grid.get('rows', 5)
    cols = grid.get('cols', 5)
    words_data = config.get('words', {}) or {}
    repetitions = props.get('repetitions', {}) or {}
    order = props.get('order', 'random')

    if isinstance(words_data, list):
        return len(words_data), rows, cols

    if order == 'random':
        total_words = 0
        for group_name, word_list in words_data.items():
            total_words += len(word_list) * repetitions.get(group_name, 1)
        return total_words, rows, cols

    max_repeats = max(repetitions.values()) if repetitions else 1
    total_words = 0
    for rep in range(max_repeats):
        for group_name, word_list in words_data.items():
            if rep < repetitions.get(group_name, 1):
                total_words += len(word_list)

    return total_words, rows, cols


def _cluster_bounds(experiments, joined_boundaries, item_index):
    """Return the inclusive experiment index range for the combined cluster around one item."""
    start = item_index
    while start > 0 and (start - 1) in joined_boundaries:
        start -= 1

    end = item_index
    while end < len(experiments) - 1 and end in joined_boundaries:
        end += 1

    return start, end


def _can_join_boundary(experiments, joined_boundaries, boundary_index):
    """Return True when the two adjacent clusters can share one physical page."""
    if boundary_index < 0 or boundary_index >= len(experiments) - 1:
        return False

    left_start, left_end = _cluster_bounds(experiments, joined_boundaries, boundary_index)
    right_start, right_end = _cluster_bounds(experiments, joined_boundaries, boundary_index + 1)
    cluster_end = max(left_end, right_end)
    merged_cluster = experiments[left_start:cluster_end + 1]
    if not merged_cluster:
        return False

    rows = merged_cluster[0].get('rows', 0)
    cols = merged_cluster[0].get('cols', 0)
    if rows <= 0 or cols <= 0:
        return False

    if any(exp.get('rows') != rows or exp.get('cols') != cols for exp in merged_cluster):
        return False

    total_words = sum(exp.get('word_count', 0) for exp in merged_cluster)
    return total_words <= (rows * cols)


def build_session_layout(experiments, joined_boundaries=None, recalibrate_between_pages=False):
    """Build per-experiment page layout metadata for the session runner."""
    joined_boundaries = set(joined_boundaries or set())
    normalized_joins = {
        boundary_index
        for boundary_index in joined_boundaries
        if _can_join_boundary(experiments, joined_boundaries, boundary_index)
    }

    clusters = []
    if experiments:
        current_cluster = [experiments[0]]
        for index in range(1, len(experiments)):
            if (index - 1) in normalized_joins:
                current_cluster.append(experiments[index])
            else:
                clusters.append(current_cluster)
                current_cluster = [experiments[index]]
        clusters.append(current_cluster)

    layout_entries = []
    total_pages = 0
    for cluster_index, cluster in enumerate(clusters):
        if not cluster:
            continue

        rows = cluster[0].get('rows', 0)
        cols = cluster[0].get('cols', 0)
        cells = rows * cols
        cluster_pages = 1 if len(cluster) > 1 else max(1, math.ceil(cluster[0].get('word_count', 0) / cells)) if cells else 0

        running_offset = 0
        for item_index, experiment in enumerate(cluster):
            same_page_as_previous = item_index > 0
            layout_entries.append({
                'config_path': experiment['config_path'],
                'display_name': experiment.get('display_name', Path(experiment['config_path']).stem),
                'word_count': experiment.get('word_count', 0),
                'rows': rows,
                'cols': cols,
                'cells': cells,
                'start_cell_offset': running_offset,
                'same_page_as_previous': same_page_as_previous,
                'recalibrate_before_start': bool(recalibrate_between_pages and cluster_index > 0 and not same_page_as_previous),
                'recalibrate_during_page_refresh': bool(recalibrate_between_pages),
            })
            running_offset += experiment.get('word_count', 0)

        total_pages += cluster_pages

    refresh_count = max(0, total_pages - 1)
    return {
        'recalibrate_between_pages': bool(recalibrate_between_pages and refresh_count > 0),
        'page_count': total_pages,
        'refresh_count': refresh_count,
        'experiments': layout_entries,
        'joined_boundaries': sorted(normalized_joins),
    }


def build_block_session_layout(
    blocks,
    recalibrate_between_blocks=False,
    joined_boundaries=None,
):
    """Build a fixed-order Block plan with optional boundary-only recalibration."""
    plan = build_session_layout(
        blocks,
        joined_boundaries=joined_boundaries,
        recalibrate_between_pages=recalibrate_between_blocks,
    )
    for entry in plan.get("experiments", []):
        entry["recalibrate_during_page_refresh"] = False
    return plan


class CombineToggleButton(QPushButton):
    """Boundary button that toggles same-page fitting between adjacent Blocks."""

    def __init__(self, boundary_index, parent=None):
        super().__init__(parent)
        self.boundary_index = boundary_index
        self._combined = False
        self._hovered = False
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._refresh_appearance()

    def set_combined(self, combined):
        self._combined = bool(combined)
        self._refresh_appearance()

    def enterEvent(self, event):
        self._hovered = True
        self._refresh_appearance()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._refresh_appearance()
        super().leaveEvent(event)

    def _refresh_appearance(self):
        if self._combined:
            self.setText('X' if self._hovered else '(')
            self.setToolTip('Remove this same-page fit')
            self.setStyleSheet(
                'font-size: 18px; font-weight: bold; color: white; '
                'background-color: #2e7d32; border-radius: 14px;'
            )
        else:
            self.setText('+')
            self.setToolTip('Keep these Blocks on the same page')
            self.setStyleSheet(
                'font-size: 18px; font-weight: bold; color: #2e7d32; '
                'background-color: white; border: 2px solid #2e7d32; border-radius: 14px;'
            )


class ArrangeExperimentsDialog(QDialog):
    """Dialog for ordering local/legacy Blocks before a run."""
    
    def __init__(self, experiments, parent=None, joined_boundaries=None):
        super().__init__(parent)
        self.setWindowTitle("Arrange Blocks")
        self.experiments = [dict(exp) for exp in experiments]
        self.joined_boundaries = set()
        for boundary_index in sorted(set(joined_boundaries or set())):
            if _can_join_boundary(
                self.experiments,
                self.joined_boundaries,
                boundary_index,
            ):
                self.joined_boundaries.add(boundary_index)
        self.selected_index = 0
        self.has_page_refreshes = False
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the Block order for this experiment:"))

        self.session_summary = QLabel()
        self.session_summary.setWordWrap(True)
        self.session_summary.setStyleSheet("color: #46505a; font-size: 12px;")
        layout.addWidget(self.session_summary)

        self.recalibrate_toggle = QCheckBox("Re-calibrate between Blocks")
        self.recalibrate_toggle.setChecked(True)
        self.recalibrate_toggle.setToolTip(
            "If enabled, a fresh calibration opens before the next Block starts."
        )
        self.recalibrate_toggle.toggled.connect(self._refresh_preview)
        layout.addWidget(self.recalibrate_toggle)

        self.order_widget = QWidget()
        self.order_layout = QGridLayout(self.order_widget)
        self.order_layout.setContentsMargins(0, 0, 0, 0)
        self.order_layout.setHorizontalSpacing(12)
        self.order_layout.setVerticalSpacing(4)
        layout.addWidget(self.order_widget)
        
        controls = QHBoxLayout()
        up_btn = QPushButton("Move Up")
        down_btn = QPushButton("Move Down")
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        
        up_btn.clicked.connect(self.move_up)
        down_btn.clicked.connect(self.move_down)
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        
        controls.addWidget(up_btn)
        controls.addWidget(down_btn)
        controls.addStretch()
        controls.addWidget(ok_btn)
        controls.addWidget(cancel_btn)
        layout.addLayout(controls)

        self._rebuild_order_widget()
        self._refresh_preview()

    def _swap_experiments(self, first_index, second_index):
        self.experiments[first_index], self.experiments[second_index] = self.experiments[second_index], self.experiments[first_index]

        lower = min(first_index, second_index)
        affected_boundaries = {lower - 1, lower + 1}
        preserved_joins = {
            boundary_index
            for boundary_index in self.joined_boundaries
            if boundary_index not in affected_boundaries
        }
        if lower in self.joined_boundaries:
            preserved_joins.add(lower)

        self.joined_boundaries = {
            boundary_index
            for boundary_index in preserved_joins
            if _can_join_boundary(self.experiments, preserved_joins, boundary_index)
        }

    def _toggle_boundary(self, boundary_index):
        if boundary_index in self.joined_boundaries:
            self.joined_boundaries.remove(boundary_index)
        elif _can_join_boundary(self.experiments, self.joined_boundaries, boundary_index):
            self.joined_boundaries.add(boundary_index)

        self._rebuild_order_widget()
        self._refresh_preview()

    def _select_index(self, index):
        self.selected_index = max(0, min(index, len(self.experiments) - 1))
        self._rebuild_order_widget()

    def _rebuild_order_widget(self):
        while self.order_layout.count():
            child = self.order_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

        for row_index, experiment in enumerate(self.experiments):
            if row_index > 0:
                boundary_index = row_index - 1
                if _can_join_boundary(self.experiments, self.joined_boundaries, boundary_index):
                    join_button = CombineToggleButton(boundary_index, self.order_widget)
                    join_button.set_combined(boundary_index in self.joined_boundaries)
                    join_button.clicked.connect(lambda checked=False, idx=boundary_index: self._toggle_boundary(idx))
                    self.order_layout.addWidget(join_button, row_index * 2 - 1, 0, alignment=Qt.AlignCenter)
                else:
                    spacer = QLabel("")
                    spacer.setFixedHeight(20)
                    self.order_layout.addWidget(spacer, row_index * 2 - 1, 0)

            row_button = QPushButton(
                f"{row_index + 1}. {experiment['display_name']}\n"
                f"{experiment['word_count']} words | {experiment['rows']}x{experiment['cols']}"
            )
            row_button.setCheckable(True)
            row_button.setChecked(row_index == self.selected_index)
            row_button.setFixedHeight(52)
            row_button.setStyleSheet(
                "text-align: left; padding: 8px 12px; font-size: 13px; border-radius: 8px;"
                "background-color: #dbeafe; border: 2px solid #2563eb;"
                if row_index == self.selected_index else
                "text-align: left; padding: 8px 12px; font-size: 13px; border-radius: 8px;"
                "background-color: #f7f7f7; border: 1px solid #d0d7de;"
            )
            row_button.clicked.connect(lambda checked=False, idx=row_index: self._select_index(idx))
            self.order_layout.addWidget(row_button, row_index * 2, 1)

        self.order_layout.setColumnStretch(1, 1)

    def _refresh_preview(self):
        preview = build_block_session_layout(
            self.experiments,
            recalibrate_between_blocks=self.recalibrate_toggle.isChecked(),
            joined_boundaries=self.joined_boundaries,
        )
        refresh_count = preview.get('refresh_count', 0)
        page_count = preview.get('page_count', 0)
        self.has_page_refreshes = refresh_count > 0
        self.recalibrate_toggle.setVisible(refresh_count > 0)
        self.session_summary.setText(
            f"Session preview: {len(self.experiments)} Blocks, "
            f"{page_count} page{'s' if page_count != 1 else ''}, "
            f"{refresh_count} refresh{'es' if refresh_count != 1 else ''}."
        )
    
    def move_up(self):
        row = self.selected_index
        if row <= 0:
            return
        self._swap_experiments(row - 1, row)
        self.selected_index = row - 1
        self._rebuild_order_widget()
        self._refresh_preview()
    
    def move_down(self):
        row = self.selected_index
        if row < 0 or row >= len(self.experiments) - 1:
            return
        self._swap_experiments(row, row + 1)
        self.selected_index = row + 1
        self._rebuild_order_widget()
        self._refresh_preview()
    
    def ordered_experiments(self):
        return [dict(exp) for exp in self.experiments]

    def session_plan(self):
        return build_block_session_layout(
            self.experiments,
            recalibrate_between_blocks=(
                self.recalibrate_toggle.isChecked() and self.has_page_refreshes
            ),
            joined_boundaries=self.joined_boundaries,
        )


class MainMenu(QWidget):
    """Cloud experiment library and application home screen."""
    
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.api = AutoScriptAPI(timeout=10)
        self.experiments = []
        self.experiment_cards = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(38, 28, 38, 28)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_area = QVBoxLayout()
        title = QLabel("Experiments")
        title.setStyleSheet("font-size: 30px; font-weight: 700; color: #172230;")
        subtitle = QLabel("Cloud experiments are ready to edit, run, download, or analyze.")
        subtitle.setStyleSheet("color: #657585; font-size: 13px;")
        title_area.addWidget(title)
        title_area.addWidget(subtitle)
        header.addLayout(title_area)
        header.addStretch()

        open_local = QToolButton()
        open_local.setText("▣")
        open_local.setToolTip("Open local or legacy ZIP")
        open_local.setFixedSize(42, 42)
        open_local.setStyleSheet(self._icon_button_style("#536578"))
        open_local.clicked.connect(self.load_experiment_zip)
        header.addWidget(open_local)

        refresh = QToolButton()
        refresh.setText("↻")
        refresh.setToolTip("Refresh experiments")
        refresh.setFixedSize(42, 42)
        refresh.setStyleSheet(self._icon_button_style("#356b9b"))
        refresh.clicked.connect(self.refresh_experiments)
        header.addWidget(refresh)

        new_experiment = QPushButton("+  New Experiment")
        new_experiment.setToolTip("Create a new experiment")
        new_experiment.setMinimumHeight(42)
        new_experiment.setStyleSheet(
            "QPushButton { background: #2d74b8; color: white; border: 0; border-radius: 8px; "
            "font-size: 14px; font-weight: 650; padding: 10px 18px; } "
            "QPushButton:hover { background: #245f98; }"
        )
        new_experiment.clicked.connect(parent.open_new_experiment)
        header.addWidget(new_experiment)
        layout.addLayout(header)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search experiments…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(40)
        self.search.setStyleSheet(
            "QLineEdit { border: 1px solid #cfd8e2; border-radius: 8px; padding: 8px 12px; "
            "background: white; font-size: 14px; } QLineEdit:focus { border-color: #4d88bd; }"
        )
        self.search.textChanged.connect(self._filter_experiments)
        layout.addWidget(self.search)

        self.status_label = QLabel("Loading experiments…")
        self.status_label.setStyleSheet("color: #667483; padding: 4px;")
        layout.addWidget(self.status_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 8, 0)
        self.list_layout.setSpacing(9)
        self.list_layout.addStretch()
        scroll.setWidget(self.list_container)
        layout.addWidget(scroll, 1)

        self.refresh_experiments()

    @staticmethod
    def _icon_button_style(color):
        return (
            f"QToolButton {{ color: {color}; background: white; border: 1px solid #d7dfe8; "
            "border-radius: 7px; font-size: 19px; font-weight: 650; }} "
            "QToolButton:hover { background: #eef5fb; border-color: #9db5cc; }"
        )

    def _action_button(self, text, tooltip, callback, color="#315f88"):
        button = QToolButton()
        if text == "▶⚙":
            pixmap = QPixmap(34, 24)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            font = QFont("Segoe UI Symbol", 15)
            painter.setFont(font)
            painter.setPen(QColor("#198a43"))
            painter.drawText(0, 0, 20, 24, Qt.AlignCenter, "▶")
            painter.setPen(QColor("#c58a00"))
            painter.drawText(16, 1, 18, 22, Qt.AlignCenter, "⚙")
            painter.end()
            button.setIcon(QIcon(pixmap))
            button.setIconSize(QSize(34, 24))
        else:
            button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedSize(39, 37)
        button.setStyleSheet(self._icon_button_style(color))
        button.clicked.connect(callback)
        return button

    def _clear_cards(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.experiment_cards = []

    def refresh_experiments(self):
        self.status_label.setText("Refreshing cloud experiments…")
        QApplication.processEvents()
        try:
            self.experiments = self.api.list_experiments()
        except APIError as exc:
            self.experiments = []
            self._clear_cards()
            self.status_label.setText(f"Cloud API unavailable: {exc}")
            return

        self._clear_cards()
        for experiment in self.experiments:
            card = self._build_experiment_card(experiment)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self.experiment_cards.append((experiment, card))
        count = len(self.experiments)
        self.status_label.setText(
            "No experiments in the cloud yet."
            if count == 0
            else ("1 experiment" if count == 1 else f"{count} experiments")
        )
        self._filter_experiments(self.search.text())

    def _build_experiment_card(self, experiment):
        card = QFrame()
        card.setObjectName("experimentCard")
        card.setStyleSheet(
            "QFrame#experimentCard { background: white; border: 1px solid #dce3ea; "
            "border-radius: 9px; } QFrame#experimentCard:hover { border-color: #9eb7ce; }"
        )
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 11, 12, 11)
        row.setSpacing(7)

        text_area = QVBoxLayout()
        name = QLabel(experiment["name"])
        name.setStyleSheet("font-size: 16px; font-weight: 650; color: #172230;")
        blocks = experiment.get("blocks")
        if blocks is None:
            blocks = experiment.get("versions", [])
        count = len(blocks)
        meta = QLabel(f"{count} Block" if count == 1 else f"{count} Blocks")
        meta.setStyleSheet("font-size: 12px; color: #6b7987;")
        text_area.addWidget(name)
        text_area.addWidget(meta)
        row.addLayout(text_area, 1)

        actions = (
            ("↓", "Download experiment ZIP", self._download_cloud_experiment, "#2463a8"),
            ("✎", "Edit experiment", self._edit_cloud_experiment, "#6b4ca5"),
            ("⧉", "Duplicate experiment", self._duplicate_cloud_experiment, "#5c6570"),
            ("✕", "Delete experiment", self._delete_cloud_experiment, "#c62828"),
            ("▶", "Run experiment", lambda exp: self._run_cloud_experiment(exp, False), "#198a43"),
            ("▶⚙", "Test-run experiment", lambda exp: self._run_cloud_experiment(exp, True), "#b07a00"),
            ("⌕", "View and analyze results", self._analyze_cloud_experiment, "#16788c"),
        )
        for text, tooltip, handler, color in actions:
            row.addWidget(
                self._action_button(
                    text,
                    tooltip,
                    lambda checked=False, exp=experiment, action=handler: action(exp),
                    color,
                )
            )
        return card

    def _filter_experiments(self, query):
        normalized = query.strip().casefold()
        visible = 0
        for experiment, card in self.experiment_cards:
            matches = normalized in experiment["name"].casefold()
            card.setVisible(matches)
            visible += int(matches)
        if normalized:
            self.status_label.setText(f"{visible} matching experiments")

    def _download_cloud_experiment(self, experiment):
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Download Experiment",
            f"{experiment['name']}.zip",
            "ZIP Files (*.zip)",
        )
        if not save_path:
            return
        try:
            self.api.download_experiment(
                experiment,
                Path(save_path).with_suffix(".zip"),
            )
        except APIError as exc:
            QMessageBox.critical(self, "Download Failed", str(exc))

    def _edit_cloud_experiment(self, experiment):
        self.parent.open_experiment_builder(experiment)

    def _duplicate_cloud_experiment(self, experiment):
        try:
            duplicate = self.api.duplicate_experiment(experiment["id"])
            self.refresh_experiments()
            QMessageBox.information(
                self,
                "Experiment Duplicated",
                f"Created ‘{duplicate['name']}’.",
            )
        except APIError as exc:
            QMessageBox.critical(self, "Duplicate Failed", str(exc))

    def _delete_cloud_experiment(self, experiment):
        answer = QMessageBox.warning(
            self,
            "Delete Experiment",
            f"Permanently delete ‘{experiment['name']}’ and all of its Blocks?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.api.delete_experiment(experiment["id"])
            self.refresh_experiments()
        except APIError as exc:
            QMessageBox.critical(self, "Delete Failed", str(exc))

    def _run_cloud_experiment(self, experiment, test_mode=False):
        try:
            downloads = ensure_dir(user_data_dir() / "server_downloads")
            safe_name = "".join(
                character if character.isalnum() or character in "-_" else "_"
                for character in experiment["name"]
            ) or "experiment"
            package_path = downloads / f"{safe_name}_{experiment['id'][:8]}.zip"
            self.api.download_experiment(experiment, package_path)
            self.load_experiment_zip(
                test_mode=test_mode,
                file_paths=[str(package_path)],
                parent_experiment=experiment,
            )
        except APIError as exc:
            QMessageBox.critical(self, "Run Failed", str(exc))

    def _analyze_cloud_experiment(self, experiment):
        self.parent.open_experiment_results(experiment)
    
    def load_experiment_zip(
        self,
        checked=False,
        test_mode=False,
        file_paths=None,
        parent_experiment=None,
    ):
        """Load a cloud Experiment bundle or legacy one-Block ZIP and launch it."""
        if file_paths is None:
            file_paths, _ = QFileDialog.getOpenFileNames(
                self,
                "Open Experiment or legacy Block package",
                "",
                "ZIP Files (*.zip)",
            )
        if not file_paths:
            return
            
        work_dir = ensure_dir(user_data_dir() / "current_experiment")
        
        def on_rm_error(func, path, exc_info):
            os.chmod(path, stat.S_IWRITE)
            try:
                func(path)
            except Exception:
                pass

        if work_dir.exists():
            try:
                shutil.rmtree(work_dir, onerror=on_rm_error)
            except Exception as e:
                try:
                    timestamp = int(time.time())
                    trash_dir = Path(f"trash_{timestamp}")
                    os.rename(work_dir, trash_dir)
                    shutil.rmtree(trash_dir, ignore_errors=True)
                except Exception:
                    QMessageBox.warning(self, "Warning", f"Could not clean previous experiment files.\nPlease ensure no experiment is currently running.\n\nError: {e}")
                    return
        
        if not work_dir.exists():
            work_dir.mkdir()
        
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            experiments = []
            session_seed = uuid.uuid4().hex

            resolved_packages = []
            for package_index, file_path in enumerate(file_paths, start=1):
                destination = work_dir / f"package_{package_index:03d}"
                resolved_packages.append(
                    unpack_experiment_package(file_path, destination)
                )

            total_blocks = sum(len(package.blocks) for package in resolved_packages)
            if parent_experiment is not None:
                session_name = parent_experiment["name"]
                session_id = parent_experiment["id"]
            elif len(resolved_packages) == 1:
                session_name = resolved_packages[0].name
                session_id = resolved_packages[0].experiment_id or session_name
            else:
                session_name = "Local Experiment"
                session_id = session_seed

            block_number = 0
            for package in resolved_packages:
                for block in package.blocks:
                    block_number += 1
                    config_file = block.config_path
                    config = dict(block.config)
                    revision_block_id = None
                    if parent_experiment is not None and parent_experiment.get("current_revision"):
                        revision_blocks = sorted(
                            parent_experiment["current_revision"].get("blocks", []),
                            key=lambda item: item.get("position", 0),
                        )
                        if block_number <= len(revision_blocks):
                            revision_block = revision_blocks[block_number - 1]
                            revision_block_id = (
                                revision_block.get("source_block_id")
                                or revision_block.get("id")
                            )
                    config.update({
                        "experiment_name": session_name,
                        "experiment_id": session_id,
                        "block_name": block.name,
                        "block_id": revision_block_id or block.block_id or config.get("block_id") or block.sha256,
                        "block_index": block_number,
                        "block_count": total_blocks,
                    })
                    if parent_experiment is not None and parent_experiment.get("current_revision"):
                        revision = parent_experiment["current_revision"]
                        config["experiment_revision_id"] = revision["id"]
                        config["experiment_revision_number"] = revision["revision_number"]
                    with config_file.open("w", encoding="utf-8") as handle:
                        json.dump(config, handle, ensure_ascii=False, indent=2)

                    total_words, rows, cols = self._calculate_experiment_info(config_file)
                    experiments.append({
                        'display_name': block.name,
                        'config_path': str(config_file),
                        'word_count': total_words,
                        'rows': rows,
                        'cols': cols,
                        'cells': rows * cols,
                        'same_page_as_previous': bool(
                            block.same_page_as_previous
                        ),
                    })

            joined_boundaries = {
                index - 1
                for index, experiment in enumerate(experiments)
                if index > 0 and experiment.get("same_page_as_previous", False)
            }
            if len(experiments) > 1 and parent_experiment is None:
                QApplication.restoreOverrideCursor()
                arrange_dialog = ArrangeExperimentsDialog(
                    experiments,
                    self,
                    joined_boundaries=joined_boundaries,
                )
                if arrange_dialog.exec_() != QDialog.Accepted:
                    return
                experiments = arrange_dialog.ordered_experiments()
                session_plan = arrange_dialog.session_plan()
                QApplication.setOverrideCursor(Qt.WaitCursor)
            else:
                recalibrate_between_blocks = False
                if len(experiments) > 1:
                    QApplication.restoreOverrideCursor()
                    answer = QMessageBox.question(
                        self,
                        "Re-calibrate Between Blocks",
                        "This experiment contains multiple Blocks.\n\n"
                        "Re-calibrate before each next Block?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    )
                    recalibrate_between_blocks = answer == QMessageBox.Yes
                    QApplication.setOverrideCursor(Qt.WaitCursor)

                session_plan = build_block_session_layout(
                    experiments,
                    recalibrate_between_blocks=recalibrate_between_blocks,
                    joined_boundaries=joined_boundaries,
                )

            for final_block_index, experiment in enumerate(experiments, start=1):
                config_path = Path(experiment["config_path"])
                with config_path.open("r", encoding="utf-8") as handle:
                    config = json.load(handle)
                config["block_index"] = final_block_index
                config["block_count"] = len(experiments)
                with config_path.open("w", encoding="utf-8") as handle:
                    json.dump(config, handle, ensure_ascii=False, indent=2)

            config_files = [Path(exp['config_path']) for exp in experiments]
            session_plan_path = work_dir / "_autoscript_session_plan.json"
            with open(session_plan_path, 'w', encoding='utf-8') as handle:
                json.dump(session_plan, handle, ensure_ascii=False, indent=2)

            for config_file in config_files:
                write_runtime_session_seed(str(config_file), session_seed)
            
            try:
                summaries = []
                plan_lookup = {
                    entry['config_path']: entry
                    for entry in session_plan.get('experiments', [])
                }
                for experiment in experiments:
                    total_words = experiment['word_count']
                    rows = experiment['rows']
                    cols = experiment['cols']
                    layout_entry = plan_lookup.get(experiment['config_path'], {})
                    summaries.append(
                        f"- {experiment['display_name']}: {total_words} words, {rows}x{cols}"
                    )
                
                QApplication.restoreOverrideCursor()
                run_label = "test mode" if test_mode else "standard mode"
                refresh_count = session_plan.get('refresh_count', 0)
                page_count = session_plan.get('page_count', 0)
                recalibration_label = "on" if session_plan.get('recalibrate_between_pages') else "off"
                msg = (
                    f"Experiment Loaded: {session_name}\n\n"
                    + "\n".join(summaries)
                    + f"\n\n{len(experiments)} Blocks, {page_count} pages, {refresh_count} refreshes."
                    + f"\nRe-calibrate between Blocks: {recalibration_label}."
                    + f"\n\nClick OK to start in {run_label}."
                )
                QMessageBox.information(self, "Experiment Info", msg)
                
            except Exception as e:
                print(f"Error calculating pages: {e}")
                QApplication.restoreOverrideCursor()
            
            self.launch_runner(config_files, session_plan_path, test_mode=test_mode)
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to load experiment: {e}")

    def load_experiment_from_server(self, checked=False, test_mode=False):
        """Select and download an immutable server version, then use the normal loader."""
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                api = AutoScriptAPI()
                experiments = [
                    experiment
                    for experiment in api.list_experiments()
                    if experiment.get("versions")
                ]
            finally:
                QApplication.restoreOverrideCursor()

            if not experiments:
                QMessageBox.information(
                    self,
                    "No Server Experiments",
                    "No published experiment versions are available on the server.",
                )
                return

            experiment_labels = [
                f"{experiment['name']} ({len(experiment['versions'])} versions)"
                for experiment in experiments
            ]
            selected_label, accepted = QInputDialog.getItem(
                self,
                "Load from Server",
                "Experiment:",
                experiment_labels,
                0,
                False,
            )
            if not accepted:
                return
            experiment = experiments[experiment_labels.index(selected_label)]

            versions = experiment["versions"]
            version_labels = [
                f"Version {version['version_number']} — {version['created_at']} — "
                f"{version['sha256'][:12]}"
                for version in versions
            ]
            selected_version_label, accepted = QInputDialog.getItem(
                self,
                "Load from Server",
                "Version:",
                version_labels,
                0,
                False,
            )
            if not accepted:
                return
            version = versions[version_labels.index(selected_version_label)]

            safe_name = "".join(
                character if character.isalnum() or character in "-_" else "_"
                for character in experiment["name"]
            ).strip("_\n\r ") or "experiment"
            download_path = (
                ensure_dir(user_data_dir() / "server_downloads")
                / f"{safe_name}_v{version['version_number']}_{version['id'][:8]}.zip"
            )

            QApplication.setOverrideCursor(Qt.WaitCursor)
            original_text = self.btn_load.text()
            try:
                def update_progress(received, total):
                    if total:
                        self.btn_load.setText(
                            f"Downloading… {int((received / total) * 100)}%"
                        )
                        QApplication.processEvents()

                api.download_version(version, download_path, progress=update_progress)
            finally:
                self.btn_load.setText(original_text)
                QApplication.restoreOverrideCursor()

            self.load_experiment_zip(
                test_mode=test_mode,
                file_paths=[str(download_path)],
            )
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Server Load Failed", str(exc))
        except Exception as exc:
            QMessageBox.critical(self, "Server Load Failed", str(exc))

    def _calculate_experiment_info(self, config_file):
        """Return total word count and grid dimensions for a config JSON."""
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        return _calculate_experiment_info_from_config(config)

    def upload_experiment_to_edit(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Upload Experiment Package", "", "ZIP Files (*.zip)")
        if not file_path:
            return

        self.parent.open_builder_import(file_path)

    @staticmethod
    def _api_child_environment():
        environment = os.environ.copy()
        token = get_session_token()
        if token:
            environment["AUTOSCRIPT_API_TOKEN"] = token
        else:
            environment.pop("AUTOSCRIPT_API_TOKEN", None)
        return environment

    def _offer_component_install(self, component, display_name):
        answer = QMessageBox.question(
            self,
            f"{display_name} Not Installed",
            f"The {display_name} component is not installed. Open the Updates menu now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes and hasattr(self.parent, "show_component_updates"):
            self.parent.show_component_updates()

    def launch_runner(self, config_files, session_plan_path, test_mode=False):
        """Resolve and launch the independently installed Runner component."""

        arguments = []
        if test_mode:
            arguments.append("--test-mode")
        arguments.extend(["--session-plan", str(session_plan_path)])
        arguments.extend(str(Path(path).resolve()) for path in config_files)
        try:
            command = component_launch_command(
                "runner",
                arguments,
                source_script="tablet_experiment.py",
                legacy_executable="ExperimentRunner.exe",
                manager=getattr(self.parent, "component_manager", None),
            )
            if command is None:
                self._offer_component_install("runner", "Runner")
                return None
            return subprocess.Popen(command, env=self._api_child_environment())
        except Exception as exc:
            QMessageBox.critical(self, "Runner Launch Failed", str(exc))
            return None

    def launch_analyzer(self, file_paths=None, extra_args=None):
        """Resolve and launch the independently installed Analyzer component."""

        file_paths = [str(Path(path).resolve()) for path in (file_paths or [])]
        extra_args = [str(argument) for argument in (extra_args or [])]
        try:
            command = component_launch_command(
                "analyzer",
                [*extra_args, *file_paths],
                source_script="analyzer_refactored.py",
                legacy_executable="Analyzer.exe",
                manager=getattr(self.parent, "component_manager", None),
            )
            if command is None:
                self._offer_component_install("analyzer", "Analyzer")
                return None
            return subprocess.Popen(command, env=self._api_child_environment())
        except Exception as exc:
            QMessageBox.critical(self, "Analyzer Launch Failed", str(exc))
        return None

