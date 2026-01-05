import sys
import os
import json
import subprocess
import zipfile
import shutil
import math
import time
import stat
import urllib.request
from pathlib import Path
from app_paths import ensure_dir, user_data_dir, asset_path
import updater
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QMessageBox, QApplication)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont


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

        # Check for Updates
        self.btn_update = QPushButton("Check for Updates")
        self.btn_update.setFixedSize(300, 50)
        self.btn_update.setStyleSheet("font-size: 16px; font-weight: bold; background-color: #607D8B; color: white; border-radius: 8px;")
        self.btn_update.clicked.connect(self.check_updates)
        btn_layout.addWidget(self.btn_update)
        
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
            subprocess.Popen([sys.executable, "tablet_experiment.py", str(config_file)])
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to load experiment: {e}")

    def launch_analyzer(self):
        """Launch the results analyzer script"""
        try:
            script_path = Path("analyzer_refactored.py")
            
            if script_path.exists():
                subprocess.Popen([sys.executable, str(script_path)])
            else:
                QMessageBox.critical(self, "Error", "analyzer_refactored.py not found!")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to launch analyzer: {e}")

    def check_updates(self):
        """Update check + optional in-app update (portable ZIP install)."""
        try:
            cfg_path = asset_path("update_config.json")
            local_ver_path = asset_path("version.json")

            if not cfg_path.exists() or not local_ver_path.exists():
                QMessageBox.warning(self, "Update", "Update configuration or local version is missing. Rebuild or reinstall.")
                return

            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            with open(local_ver_path, "r", encoding="utf-8") as f:
                local_info = json.load(f)

            channel = local_info.get("channel") or cfg.get("default_channel", "stable")
            channel_cfg = cfg.get("channels", {}).get(channel)
            if not channel_cfg:
                QMessageBox.warning(self, "Update", f"Channel '{channel}' not found in update_config.json.")
                return

            remote_url = channel_cfg.get("version_url")
            if not remote_url:
                QMessageBox.warning(self, "Update", "Remote version URL is not configured.")
                return

            try:
                with urllib.request.urlopen(remote_url, timeout=10) as resp:
                    remote_info = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                QMessageBox.warning(self, "Update", f"Could not reach update server for channel '{channel}'.\n{e}")
                return

            local_version = str(local_info.get("version", "0"))
            remote_version = str(remote_info.get("version", "0"))
            remote_channel = remote_info.get("channel", channel)

            if not updater.is_remote_newer(remote_version, local_version):
                QMessageBox.information(self, "Update", f"You are up to date ({local_version}, channel {channel}).")
                return

            asset_url_tmpl = channel_cfg.get("asset_url")
            checksum_url = channel_cfg.get("checksum_url")
            if not asset_url_tmpl or not checksum_url:
                QMessageBox.information(
                    self,
                    "Update",
                    f"Update available on '{remote_channel}':\nCurrent: {local_version}\nRemote: {remote_version}\n\n"
                    f"But asset_url/checksum_url are missing in update_config.json, so auto-update can't run.",
                )
                return

            if not getattr(sys, "frozen", False):
                QMessageBox.information(
                    self,
                    "Update",
                    f"Update available on '{remote_channel}':\nCurrent: {local_version}\nRemote: {remote_version}\n\n"
                    "You are running from source (python). Update via git pull / reinstall requirements.\n"
                    "Auto-update is supported for the packaged EXE build.",
                )
                return

            download_url = asset_url_tmpl.replace("{version}", remote_version)
            prompt = QMessageBox(self)
            prompt.setWindowTitle("Update")
            prompt.setIcon(QMessageBox.Information)
            prompt.setText(
                f"Update available on '{remote_channel}':\n"
                f"Current: {local_version}\nRemote: {remote_version}\n\n"
                "Download and install now? (The new version will open in a new folder and this one will close.)"
            )
            btn_update = prompt.addButton("Update Now", QMessageBox.AcceptRole)
            prompt.addButton("Later", QMessageBox.RejectRole)
            prompt.exec_()
            if prompt.clickedButton() != btn_update:
                return

            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                zip_path, extract_dir = updater.get_update_paths(remote_version)
                updater.download_to(download_url, zip_path)

                expected = updater.fetch_checksum_sha256(checksum_url)
                actual = updater.sha256_file(zip_path)
                if actual != expected:
                    raise ValueError(
                        "Downloaded update failed SHA256 verification. "
                        "(This usually means the Release ZIP and version.sha256 are out of sync.)"
                    )

                updater.extract_zip(zip_path, extract_dir)
                exe_path = updater.find_app_executable(extract_dir)
                updater.launch_detached(exe_path)

            finally:
                QApplication.restoreOverrideCursor()

            QMessageBox.information(
                self,
                "Update",
                "Update installed and launched. This window will now close.\n\n"
                "If everything looks good, you can delete the old folder manually.",
            )
            QApplication.quit()

        except Exception as e:
            QMessageBox.critical(self, "Update", f"Unexpected error during update check: {e}")
