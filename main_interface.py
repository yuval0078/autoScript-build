import os
import subprocess
import sys
import threading

from qt_bootstrap import ensure_qt_platform_plugin_path
from project_version import APP_NAME
from component_versions import (
    COMPONENT_NAMES,
    COMPONENT_VERSIONS,
    compare_component_versions,
    get_component_version,
)

from PyQt5.QtWidgets import (
    QAction, QApplication, QInputDialog, QLineEdit, QMainWindow,
    QMessageBox, QProgressDialog, QStackedWidget, QLabel,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from autoscript_api import APIError, AutoScriptAPI, get_session_token
from component_runtime import component_launch_command, create_update_manager
from component_updates_dialog import ComponentUpdateCoordinator, ComponentUpdatesDialog
from user_management_dialog import UserManagementDialog

from gui_menu import MainMenu
from experiment_results import ExperimentResultsPage


def builder_launch_command(arguments, *, manager=None, frozen=None, executable=None):
    """Return the standalone Builder command for packaged or source mode."""

    return component_launch_command(
        "builder",
        arguments,
        source_script="builder_main.py",
        legacy_executable="Builder.exe",
        manager=manager,
        frozen=frozen,
        executable=executable,
    )


def builder_child_environment(environment=None):
    """Build the child environment without ever placing credentials in argv."""

    child_environment = dict(os.environ if environment is None else environment)
    token = get_session_token()
    if token:
        child_environment["AUTOSCRIPT_API_TOKEN"] = token
    else:
        child_environment.pop("AUTOSCRIPT_API_TOKEN", None)
    return child_environment


class MainInterface(QMainWindow):
    """Main application window that manages different pages"""

    cloud_sync_status = pyqtSignal(str)
    
    def __init__(self):
        super().__init__()
        self.api_user = self._authenticate_if_required()
        self.component_manager = create_update_manager()
        self.component_update_coordinator = ComponentUpdateCoordinator(
            self.component_manager, self
        )
        self.component_update_coordinator.progress_changed.connect(
            self._handle_component_update_progress
        )
        self.component_update_coordinator.install_finished.connect(
            self._handle_component_update_finished
        )
        self.component_update_coordinator.install_failed.connect(
            self._handle_component_update_failed
        )
        self.updates_dialog = None
        self._interface_update_progress = None
        self._automatic_update_result = None
        # Status Bar
        self.status_msg = QLabel("Ready")
        self.statusBar().addWidget(self.status_msg)
        self.cloud_sync_status.connect(self.status_msg.setText)
        self._pending_upload_lock = threading.Lock()
        self.version_label = QLabel(
            f"Interface {get_component_version('interface')}"
        )
        self.version_label.setStyleSheet("color: #6f7d8c; font-size: 11px; padding-left: 12px;")
        self.statusBar().addPermanentWidget(self.version_label)

        if (self.api_user or {}).get("role") == "admin":
            administration_menu = self.menuBar().addMenu("Administration")
            users_action = QAction("User Management…", self)
            users_action.triggered.connect(self.show_user_management)
            administration_menu.addAction(users_action)

        updates_menu = self.menuBar().addMenu("Updates")
        updates_action = QAction("Manage Components…", self)
        updates_action.triggered.connect(self.show_component_updates)
        updates_menu.addAction(updates_action)

        self.setWindowTitle(APP_NAME)
        self.resize(1200, 800)
        
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        
        self.main_menu = MainMenu(self)
        self.experiment_results = ExperimentResultsPage(self)
        self.builder_processes = []
        
        self.stack.addWidget(self.main_menu)
        self.stack.addWidget(self.experiment_results)

        self.builder_process_timer = QTimer(self)
        self.builder_process_timer.setInterval(800)
        self.builder_process_timer.timeout.connect(self._poll_builder_processes)
        self.builder_process_timer.start()

        # Always start on the home screen.
        self.show_main_menu()
        QTimer.singleShot(0, self._retry_pending_uploads)
        self.pending_upload_timer = QTimer(self)
        self.pending_upload_timer.setInterval(30_000)
        self.pending_upload_timer.timeout.connect(self._retry_pending_uploads)
        self.pending_upload_timer.start()
        QTimer.singleShot(1500, self._start_automatic_update_check)

    def _authenticate_if_required(self):
        api = AutoScriptAPI(timeout=10)
        try:
            return api.me()
        except APIError as exc:
            if exc.status_code != 401:
                return None
        while True:
            username, accepted = QInputDialog.getText(
                self, "AutoScript Login", "Username:"
            )
            if not accepted:
                return None
            password, accepted = QInputDialog.getText(
                self, "AutoScript Login", "Password:", QLineEdit.Password
            )
            if not accepted:
                return None
            try:
                return api.login(username.strip(), password)["user"]
            except APIError as exc:
                retry = QMessageBox.warning(
                    self, "Login Failed", str(exc),
                    QMessageBox.Retry | QMessageBox.Cancel, QMessageBox.Retry,
                )
                if retry != QMessageBox.Retry:
                    return None

    def _retry_pending_uploads(self):
        if not self._pending_upload_lock.acquire(blocking=False):
            return

        def synchronize():
            status_text = None
            try:
                from analysis_sync_queue import drain_analysis_queue
                from result_upload_queue import drain_upload_queue

                api = AutoScriptAPI(timeout=10)
                uploaded, errors = drain_upload_queue(api)
                analyzed, analysis_errors, _outcomes = drain_analysis_queue(api)
                if uploaded or analyzed:
                    status_text = (
                        f"Synchronized {uploaded + analyzed} queued operation(s)"
                    )
                elif errors or analysis_errors:
                    status_text = "Pending cloud uploads remain queued"
            except Exception:
                status_text = "Pending cloud uploads remain queued"
            finally:
                self._pending_upload_lock.release()
            if status_text:
                self.cloud_sync_status.emit(status_text)

        threading.Thread(target=synchronize, daemon=True).start()

    def _start_automatic_update_check(self):
        if self._automatic_update_result is not None:
            return

        def check():
            try:
                self._automatic_update_result = (
                    "ok",
                    self.component_manager.load_catalog(),
                )
            except Exception as exc:
                self._automatic_update_result = ("error", exc)

        threading.Thread(target=check, name="autoscript-update-check", daemon=True).start()
        QTimer.singleShot(250, self._poll_automatic_update_check)

    def _poll_automatic_update_check(self):
        result = self._automatic_update_result
        if result is None:
            QTimer.singleShot(250, self._poll_automatic_update_check)
            return
        status, value = result
        if status != "ok":
            self.status_msg.setText("Automatic update check unavailable")
            return

        catalog = value
        available = []
        for component in COMPONENT_NAMES:
            installed = self.component_manager.get_installed_component(component)
            current = installed.version if installed is not None else (
                COMPONENT_VERSIONS[component] if component == "interface" else None
            )
            latest = catalog.components[component].version
            if current is None or compare_component_versions(latest, current) > 0:
                available.append(component)
        if available:
            self.status_msg.setText(
                f"{len(available)} component update(s) available in the Updates menu"
            )

    def show_main_menu(self, refresh=False):
        if refresh:
            self.main_menu.refresh_experiments()
        self.stack.setCurrentWidget(self.main_menu)

    def open_new_experiment(self):
        if (self.api_user or {}).get("role") == "operator":
            QMessageBox.warning(self, "Permission denied", "Researcher access is required.")
            return None
        return self._launch_builder(["--new"])

    def open_experiment_builder(self, experiment):
        if (self.api_user or {}).get("role") == "operator":
            QMessageBox.warning(self, "Permission denied", "Researcher access is required.")
            return None
        return self._launch_builder(["--experiment-id", str(experiment["id"])])

    def open_builder_import(self, package_path):
        if (self.api_user or {}).get("role") == "operator":
            QMessageBox.warning(self, "Permission denied", "Researcher access is required.")
            return None
        return self._launch_builder(["--import-block", str(package_path)])

    def open_experiment_results(self, experiment):
        self.experiment_results.set_experiment(experiment)
        self.stack.setCurrentWidget(self.experiment_results)


    def show_component_updates(self):
        dialog = ComponentUpdatesDialog(
            self,
            manager=self.component_manager,
            coordinator=self.component_update_coordinator,
        )
        dialog.interface_update_started.connect(self._begin_interface_update)
        self.updates_dialog = dialog
        try:
            dialog.exec_()
        finally:
            self.updates_dialog = None

    def _begin_interface_update(self):
        progress = QProgressDialog(self)
        progress.setWindowTitle("Updating AutoScript")
        progress.setLabelText(
            "Updating, the app will restart when finished."
        )
        progress.setCancelButton(None)
        progress.setRange(0, 100)
        progress.setValue(self.component_update_coordinator.progress)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumDuration(0)
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setWindowFlag(Qt.WindowCloseButtonHint, False)
        self._interface_update_progress = progress
        progress.show()

    def _handle_component_update_progress(self, component, percentage):
        display_name = component.replace("_", " ").title()
        self.status_msg.setText(
            f"Updating {display_name}… {percentage}%"
        )
        if component == "interface" and self._interface_update_progress is not None:
            self._interface_update_progress.setValue(percentage)

    def _handle_component_update_finished(self, component, installed):
        self.status_msg.setText(
            f"{component.replace('_', ' ').title()} {installed.version} is ready"
        )
        if component == "interface":
            if self._interface_update_progress is not None:
                self._interface_update_progress.setValue(100)
                self._interface_update_progress.setLabelText(
                    "Update complete. Restarting AutoScript…"
                )
            QTimer.singleShot(250, self._restart_after_interface_update)

    def _handle_component_update_failed(self, component, error):
        self.status_msg.setText(
            f"{component.replace('_', ' ').title()} update failed"
        )
        if component != "interface":
            return
        if self._interface_update_progress is not None:
            self._interface_update_progress.close()
            self._interface_update_progress.deleteLater()
            self._interface_update_progress = None
        QMessageBox.critical(self, "Update Failed", error)

    def _restart_after_interface_update(self):
        command = component_launch_command(
            "interface",
            sys.argv[1:],
            source_script="main_interface.py",
            legacy_executable="AutoScriptInterface.exe",
            manager=self.component_manager,
        )
        if command is None:
            self._handle_component_update_failed(
                "interface", "The updated Main Interface entry point is unavailable."
            )
            return
        try:
            subprocess.Popen(command, env=builder_child_environment())
        except OSError as exc:
            self._handle_component_update_failed("interface", str(exc))
            return
        QApplication.instance().quit()

    def show_user_management(self):
        if (self.api_user or {}).get("role") != "admin":
            QMessageBox.warning(
                self,
                "Administrator access required",
                "Only an administrator can manage application users.",
            )
            return None
        dialog = UserManagementDialog(self, current_user=self.api_user)
        dialog.exec_()
        return dialog



    def _launch_builder(self, arguments):
        if (
            self.component_update_coordinator.is_installing("builder")
            and self.component_manager.get_installed_component("builder") is None
        ):
            QMessageBox.information(
                self,
                "Builder Update In Progress",
                "Wait until the Builder installation is finished.",
            )
            return None
        command = builder_launch_command(arguments, manager=self.component_manager)
        if command is None:
            answer = QMessageBox.question(
                self,
                "Builder Not Installed",
                "The Builder component is not installed. Open the Updates menu now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self.show_component_updates()
            return None
        try:
            process = subprocess.Popen(
                command,
                env=builder_child_environment(),
            )
        except OSError as exc:
            QMessageBox.critical(self, "Builder Launch Failed", str(exc))
            return None
        self.builder_processes.append(process)
        self.status_msg.setText("Builder opened")
        return process

    def _poll_builder_processes(self):
        running = []
        finished = False
        for process in self.builder_processes:
            if process.poll() is None:
                running.append(process)
            else:
                finished = True
        self.builder_processes = running
        if finished:
            self.status_msg.setText("Experiment list refreshed")
            self.main_menu.refresh_experiments()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        # Exercise the packaged runtime without login, network or research data.
        import json
        from pathlib import Path
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        ensure_qt_platform_plugin_path()
        app = QApplication([])
        page = ExperimentResultsPage(type("SmokeParent", (), {"api_user": {"role": "operator"}})())
        page.process_timer.stop()
        page.close()
        Path(sys.argv[2]).write_text(
            json.dumps({"status": "ok", "qt": True, "results_page": True}),
            encoding="utf-8",
        )
        sys.exit(0)
    ensure_qt_platform_plugin_path()
    app = QApplication(sys.argv)
    window = MainInterface()
    window.show()
    sys.exit(app.exec_())
