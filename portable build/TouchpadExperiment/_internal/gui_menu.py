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
                             QLabel, QFileDialog, QMessageBox, QApplication)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont

# Import analyzer and experiment runner for direct launching
import analyzer_refactored
import tablet_experiment


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
        self.btn_load = QPushButton("Run Experiment")
        self.btn_load.setFixedSize(300, 60)
        self.btn_load.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #4CAF50; color: white; border-radius: 8px;")
        self.btn_load.clicked.connect(self.load_experiment_zip)
        btn_layout.addWidget(self.btn_load)
        
        # New Experiment
        self.btn_new = QPushButton("Create a New Experiment")
        self.btn_new.setFixedSize(300, 60)
        self.btn_new.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #FF9800; color: white; border-radius: 8px;")
        self.btn_new.clicked.connect(parent.show_new_experiment)
        btn_layout.addWidget(self.btn_new)
        
        # Analyze Results
        self.btn_analyze = QPushButton("Analyze Results")
        self.btn_analyze.setFixedSize(300, 60)
        self.btn_analyze.setStyleSheet("font-size: 18px; font-weight: bold; background-color: #2196F3; color: white; border-radius: 8px;")
        self.btn_analyze.clicked.connect(self.launch_analyzer)
        btn_layout.addWidget(self.btn_analyze)

        layout.addWidget(btn_container, 0, Qt.AlignCenter)
    
    def load_experiment_zip(self):
        """Load and launch an experiment from a ZIP package"""
        file_path, _ = QFileDialog.getOpenFileName(self, "Load Experiment Package", "", "ZIP Files (*.zip)")
        if not file_path:
            return
            
        # Define working directory
        work_dir = ensure_dir(user_data_dir() / "current_experiment")
        
        # Robust cleanup function
        def on_rm_error(func, path, exc_info):
            # Attempt to make the file writable and try again
            os.chmod(path, stat.S_IWRITE)
            try:
                func(path)
            except Exception:
                pass

        if work_dir.exists():
            try:
                # Try standard removal
                shutil.rmtree(work_dir, onerror=on_rm_error)
            except Exception as e:
                # If that fails, try to rename it to move it out of the way
                try:
                    timestamp = int(time.time())
                    trash_dir = Path(f"trash_{timestamp}")
                    os.rename(work_dir, trash_dir)
                    shutil.rmtree(trash_dir, ignore_errors=True)
                except Exception as e2:
                    QMessageBox.warning(self, "Warning", f"Could not clean previous experiment files.\nPlease ensure no experiment is currently running.\n\nError: {e}")
                    return
        
        # Re-create directory if it was removed
        if not work_dir.exists():
            work_dir.mkdir()
        
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(work_dir)
                
            # Find JSON config
            json_files = list(work_dir.glob("*.json"))
            if not json_files:
                raise FileNotFoundError("No configuration JSON found in zip")
            
            config_file = json_files[0]
            
            # Calculate pages needed
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                
                props = config.get('properties', {})
                grid = props.get('grid', {})
                rows = grid.get('rows', 5)
                cols = grid.get('cols', 5)
                grid_size = rows * cols
                
                words_data = config.get('words', {})
                repetitions = props.get('repetitions', {})
                order = props.get('order', 'random')
                
                # Calculate total words including repetitions
                if order == 'random':
                    # Random: each word repeated X times based on group
                    total_words = 0
                    for group_name, word_list in words_data.items():
                        repeat_count = repetitions.get(group_name, 1)
                        total_words += len(word_list) * repeat_count
                else:
                    # Ordinal: all groups played, then repeat entire sequence
                    max_repeats = max(repetitions.values()) if repetitions else 1
                    total_words = 0
                    unique_word_count = sum(len(word_list) for word_list in words_data.values())
                    
                    for rep in range(max_repeats):
                        for group_name, word_list in words_data.items():
                            group_repeats = repetitions.get(group_name, 1)
                            if rep < group_repeats:
                                total_words += len(word_list)
                
                pages = math.ceil(total_words / grid_size)
                refreshes = max(0, pages - 1)
                
                QApplication.restoreOverrideCursor()
                
                msg = f"Experiment Loaded:\n\n" \
                      f"• Total Words: {total_words}\n" \
                      f"• Grid Size: {rows}x{cols} ({grid_size} cells)\n" \
                      f"• Pages Needed: {pages}\n" \
                      f"• Paper Refreshes: {refreshes}\n\n" \
                      f"Click OK to start."
                
                QMessageBox.information(self, "Experiment Info", msg)
                
            except Exception as e:
                print(f"Error calculating pages: {e}")
                QApplication.restoreOverrideCursor()
            
            # Launch experiment
            if getattr(sys, 'frozen', False):
                # Running as packaged executable - launch ExperimentRunner.exe
                exe_dir = Path(sys.executable).parent
                experiment_exe = exe_dir / "ExperimentRunner.exe"
                
                if experiment_exe.exists():
                    subprocess.Popen([str(experiment_exe), str(config_file)])
                else:
                    QMessageBox.critical(self, "Error", f"ExperimentRunner.exe not found at {experiment_exe}")
            else:
                # Running as script - use subprocess
                subprocess.Popen([sys.executable, "tablet_experiment.py", str(config_file)])
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to load experiment: {e}")

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

