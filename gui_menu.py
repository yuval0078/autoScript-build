import sys
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
from app_paths import ensure_dir, user_data_dir, asset_path, source_script_path
from archive_utils import safe_extract_zip
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QFileDialog, QMessageBox, QApplication,
                             QDialog, QToolButton, QMenu, QGridLayout,
                             QCheckBox)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont


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


class CombineToggleButton(QPushButton):
    """Boundary button that toggles same-page fitting between adjacent experiments."""

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
            self.setToolTip('Keep these experiments on the same page')
            self.setStyleSheet(
                'font-size: 18px; font-weight: bold; color: #2e7d32; '
                'background-color: white; border: 2px solid #2e7d32; border-radius: 14px;'
            )


class ArrangeExperimentsDialog(QDialog):
    """Dialog for ordering multiple selected experiment packages."""
    
    def __init__(self, experiments, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Arrange Experiments")
        self.experiments = [dict(exp) for exp in experiments]
        self.joined_boundaries = set()
        self.selected_index = 0
        self.has_page_refreshes = False
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the order for this session:"))

        self.session_summary = QLabel()
        self.session_summary.setWordWrap(True)
        self.session_summary.setStyleSheet("color: #46505a; font-size: 12px;")
        layout.addWidget(self.session_summary)

        self.recalibrate_toggle = QCheckBox("Re-calibrate between pages")
        self.recalibrate_toggle.setChecked(True)
        self.recalibrate_toggle.setToolTip(
            "If enabled, every page change opens a fresh calibration before the next page starts."
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
        preview = build_session_layout(
            self.experiments,
            self.joined_boundaries,
            recalibrate_between_pages=self.recalibrate_toggle.isChecked()
        )
        refresh_count = preview.get('refresh_count', 0)
        page_count = preview.get('page_count', 0)
        self.has_page_refreshes = refresh_count > 0
        self.recalibrate_toggle.setVisible(refresh_count > 0)
        self.session_summary.setText(
            f"Session preview: {page_count} page{'s' if page_count != 1 else ''}, "
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
        return build_session_layout(
            self.experiments,
            self.joined_boundaries,
            recalibrate_between_pages=self.recalibrate_toggle.isChecked() and self.has_page_refreshes
        )


class MainMenu(QWidget):
    """Main menu widget for the Touchpad Experiment Manager"""
    
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)
        
        # Title
        title = QLabel("Touchpad Writing Experiment")
        title.setStyleSheet("font-size: 32px; font-weight: bold; color: #1a1a1a; margin-bottom: 40px;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        
        # Buttons Container
        btn_container = QWidget()
        btn_layout = QVBoxLayout(btn_container)
        btn_layout.setSpacing(15)
        
        # Load Experiment
        self.btn_load = QToolButton()
        self.btn_load.setFixedSize(300, 60)
        self.btn_load.setText("Run Experiment")
        self.btn_load.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #4CAF50; color: white; border-radius: 8px;")
        self.btn_load.setPopupMode(QToolButton.MenuButtonPopup)
        self.btn_load.clicked.connect(self.load_experiment_zip)
        load_menu = QMenu(self.btn_load)
        test_run_action = load_menu.addAction("Test-Run Experiment")
        test_run_action.triggered.connect(lambda checked=False: self.load_experiment_zip(test_mode=True))
        self.btn_load.setMenu(load_menu)
        btn_layout.addWidget(self.btn_load)
        
        # New Experiment
        self.btn_new = QPushButton("Create a New Experiment")
        self.btn_new.setFixedSize(300, 60)
        self.btn_new.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #FF9800; color: white; border-radius: 8px;")
        self.btn_new.clicked.connect(parent.show_new_experiment)
        btn_layout.addWidget(self.btn_new)

        self.btn_edit = QPushButton("Upload Experiment To Edit")
        self.btn_edit.setFixedSize(300, 60)
        self.btn_edit.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #795548; color: white; border-radius: 8px;")
        self.btn_edit.clicked.connect(self.upload_experiment_to_edit)
        btn_layout.addWidget(self.btn_edit)
        
        # Analyze Results
        self.btn_analyze = QPushButton("Analyze Results")
        self.btn_analyze.setFixedSize(300, 60)
        self.btn_analyze.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #2196F3; color: white; border-radius: 8px;")
        self.btn_analyze.clicked.connect(self.launch_analyzer)
        btn_layout.addWidget(self.btn_analyze)

        layout.addWidget(btn_container, 0, Qt.AlignCenter)
    
    def load_experiment_zip(self, checked=False, test_mode=False):
        """Load and launch one or more experiments from ZIP packages."""
        file_paths, _ = QFileDialog.getOpenFileNames(self, "Load Experiment Package", "", "ZIP Files (*.zip)")
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
            
            for index, file_path in enumerate(file_paths):
                if len(file_paths) == 1:
                    extract_dir = work_dir
                else:
                    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(file_path).stem)
                    extract_dir = ensure_dir(work_dir / f"{index + 1:02d}_{safe_name}")
                
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    safe_extract_zip(zip_ref, extract_dir)
                    
                json_files = list(extract_dir.glob("*.json"))
                if not json_files:
                    raise FileNotFoundError(f"No configuration JSON found in {Path(file_path).name}")

                config_file = json_files[0]
                total_words, rows, cols = self._calculate_experiment_info(config_file)
                experiments.append({
                    'display_name': Path(file_path).stem,
                    'config_path': str(config_file),
                    'word_count': total_words,
                    'rows': rows,
                    'cols': cols,
                    'cells': rows * cols,
                })

            if len(experiments) > 1:
                QApplication.restoreOverrideCursor()
                arrange_dialog = ArrangeExperimentsDialog(experiments, self)
                if arrange_dialog.exec_() != QDialog.Accepted:
                    return
                experiments = arrange_dialog.ordered_experiments()
                session_plan = arrange_dialog.session_plan()
                QApplication.setOverrideCursor(Qt.WaitCursor)
            else:
                session_plan = build_session_layout(experiments, recalibrate_between_pages=False)

            config_files = [Path(exp['config_path']) for exp in experiments]
            session_plan_path = work_dir / "_autoscript_session_plan.json"
            with open(session_plan_path, 'w', encoding='utf-8') as handle:
                json.dump(session_plan, handle, ensure_ascii=False, indent=2)

            from tablet_experiment import load_experiment_config, write_runtime_session_seed
            for config_file in config_files:
                write_runtime_session_seed(str(config_file), session_seed)
                load_experiment_config(str(config_file))
            
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
                    page_note = "same page" if layout_entry.get('same_page_as_previous') else "new page"
                    start_cell = layout_entry.get('start_cell_offset', 0) + 1
                    summaries.append(
                        f"- {experiment['display_name']}: {total_words} words, {rows}x{cols}, "
                        f"starts at cell {start_cell}, {page_note}"
                    )
                
                QApplication.restoreOverrideCursor()
                run_label = "test mode" if test_mode else "standard mode"
                refresh_count = session_plan.get('refresh_count', 0)
                page_count = session_plan.get('page_count', 0)
                recalibration_label = "on" if session_plan.get('recalibrate_between_pages') else "off"
                msg = (
                    "Experiment Session Loaded:\n\n"
                    + "\n".join(summaries)
                    + f"\n\nSession total: {page_count} pages, {refresh_count} refreshes."
                    + f"\nRe-calibrate between pages: {recalibration_label}."
                    + f"\n\nClick OK to start in {run_label}."
                )
                QMessageBox.information(self, "Experiment Info", msg)
                
            except Exception as e:
                print(f"Error calculating pages: {e}")
                QApplication.restoreOverrideCursor()
            
            if getattr(sys, 'frozen', False):
                exe_dir = Path(sys.executable).parent
                experiment_exe = exe_dir / "ExperimentRunner.exe"
                
                if experiment_exe.exists():
                    command = [str(experiment_exe)]
                    if test_mode:
                        command.append("--test-mode")
                    command.extend(["--session-plan", str(session_plan_path)])
                    command.extend(str(path) for path in config_files)
                    subprocess.Popen(command)
                else:
                    QMessageBox.critical(self, "Error", f"ExperimentRunner.exe not found at {experiment_exe}")
            else:
                runner_script = source_script_path("tablet_experiment.py")
                if not runner_script.exists():
                    QMessageBox.critical(self, "Error", f"tablet_experiment.py not found at {runner_script}")
                    return
                command = [sys.executable, str(runner_script)]
                if test_mode:
                    command.append("--test-mode")
                command.extend(["--session-plan", str(session_plan_path)])
                command.extend(str(path) for path in config_files)
                subprocess.Popen(command)
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to load experiment: {e}")

    def _calculate_experiment_info(self, config_file):
        """Return total word count and grid dimensions for a config JSON."""
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        return _calculate_experiment_info_from_config(config)

    def upload_experiment_to_edit(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Upload Experiment Package", "", "ZIP Files (*.zip)")
        if not file_path:
            return

        if self.parent.new_experiment.import_experiment_zip(file_path):
            self.parent.show_new_experiment()

    def launch_analyzer(self):
        """Launch the results analyzer script"""
        try:
            if getattr(sys, 'frozen', False):
                # Running as packaged executable - launch Analyzer.exe
                exe_dir = Path(sys.executable).parent
                analyzer_exe = exe_dir / "Analyzer.exe"
                
                if analyzer_exe.exists():
                    subprocess.Popen([str(analyzer_exe)])
                else:
                    QMessageBox.critical(self, "Error", f"Analyzer.exe not found at {analyzer_exe}")
            else:
                # Running as script - launch as subprocess
                script_path = source_script_path("analyzer_refactored.py")
                if script_path.exists():
                    subprocess.Popen([sys.executable, str(script_path)])
                else:
                    QMessageBox.critical(self, "Error", f"analyzer_refactored.py not found at {script_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to launch analyzer: {e}")

