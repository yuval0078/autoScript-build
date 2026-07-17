import sys
import os
import json
import subprocess
import zipfile
import shutil
import math
import time
import stat
from pathlib import Path
from app_paths import ensure_dir, user_data_dir, asset_path
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QMessageBox, QApplication,
                             QDialog, QListWidget, QToolButton, QMenu)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont


class ArrangeExperimentsDialog(QDialog):
    """Dialog for ordering multiple selected experiment packages."""
    
    def __init__(self, file_paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Arrange Experiments")
        self.file_paths = list(file_paths)
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the order for this session:"))
        
        self.list_widget = QListWidget()
        for path in self.file_paths:
            self.list_widget.addItem(Path(path).name)
        self.list_widget.setCurrentRow(0)
        layout.addWidget(self.list_widget)
        
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
    
    def move_up(self):
        row = self.list_widget.currentRow()
        if row <= 0:
            return
        self.file_paths[row - 1], self.file_paths[row] = self.file_paths[row], self.file_paths[row - 1]
        item = self.list_widget.takeItem(row)
        self.list_widget.insertItem(row - 1, item)
        self.list_widget.setCurrentRow(row - 1)
    
    def move_down(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= self.list_widget.count() - 1:
            return
        self.file_paths[row + 1], self.file_paths[row] = self.file_paths[row], self.file_paths[row + 1]
        item = self.list_widget.takeItem(row)
        self.list_widget.insertItem(row + 1, item)
        self.list_widget.setCurrentRow(row + 1)
    
    def ordered_paths(self):
        return list(self.file_paths)


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
        
        if len(file_paths) > 1:
            arrange_dialog = ArrangeExperimentsDialog(file_paths, self)
            if arrange_dialog.exec_() != QDialog.Accepted:
                return
            file_paths = arrange_dialog.ordered_paths()
            
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
            config_files = []
            
            for index, file_path in enumerate(file_paths):
                if len(file_paths) == 1:
                    extract_dir = work_dir
                else:
                    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(file_path).stem)
                    extract_dir = ensure_dir(work_dir / f"{index + 1:02d}_{safe_name}")
                
                with zipfile.ZipFile(file_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                    
                json_files = list(extract_dir.glob("*.json"))
                if not json_files:
                    raise FileNotFoundError(f"No configuration JSON found in {Path(file_path).name}")
                
                config_files.append(json_files[0])
            
            try:
                summaries = []
                for config_file in config_files:
                    total_words, rows, cols = self._calculate_experiment_info(config_file)
                    grid_size = rows * cols
                    pages = math.ceil(total_words / grid_size) if grid_size else 0
                    refreshes = max(0, pages - 1)
                    summaries.append(f"- {config_file.stem}: {total_words} words, {rows}x{cols}, {pages} pages, {refreshes} refreshes")
                
                QApplication.restoreOverrideCursor()
                run_label = "test mode" if test_mode else "standard mode"
                msg = "Experiment Session Loaded:\n\n" + "\n".join(summaries) + f"\n\nClick OK to start in {run_label}."
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
                    command.extend(str(path) for path in config_files)
                    subprocess.Popen(command)
                else:
                    QMessageBox.critical(self, "Error", f"ExperimentRunner.exe not found at {experiment_exe}")
            else:
                command = [sys.executable, "tablet_experiment.py"]
                if test_mode:
                    command.append("--test-mode")
                command.extend(str(path) for path in config_files)
                subprocess.Popen(command)
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to load experiment: {e}")

    def _calculate_experiment_info(self, config_file):
        """Return total word count and grid dimensions for a config JSON."""
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)

        if 'groups' in config and isinstance(config.get('groups'), list):
            grid = config.get('grid', {})
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
        else:
            props = config.get('properties', {})
            grid = props.get('grid', {})
            rows = grid.get('rows', 5)
            cols = grid.get('cols', 5)

            words_data = config.get('words', {})
            repetitions = props.get('repetitions', {})
            order = props.get('order', 'random')

            if order == 'random':
                total_words = 0
                for group_name, word_list in words_data.items():
                    total_words += len(word_list) * repetitions.get(group_name, 1)
            else:
                max_repeats = max(repetitions.values()) if repetitions else 1
                total_words = 0
                for rep in range(max_repeats):
                    for group_name, word_list in words_data.items():
                        if rep < repetitions.get(group_name, 1):
                            total_words += len(word_list)
        
        return total_words, rows, cols

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
                script_path = Path("analyzer_refactored.py")
                if script_path.exists():
                    subprocess.Popen([sys.executable, str(script_path)])
                else:
                    QMessageBox.critical(self, "Error", "analyzer_refactored.py not found!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to launch analyzer: {e}")

