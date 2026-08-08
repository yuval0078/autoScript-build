import sys
from qt_bootstrap import ensure_qt_platform_plugin_path
from project_version import APP_VERSION_LABEL, APP_NAME

from PyQt5.QtWidgets import (
    QApplication, QInputDialog, QLineEdit, QMainWindow, QMessageBox,
    QStackedWidget, QLabel,
)
from PyQt5.QtCore import QTimer
from autoscript_api import APIError, AutoScriptAPI

# Import the modularized components
from gui_menu import MainMenu
from exp_initializer import NewExperimentWizard, ExperimentPropertiesPage
from builder_workspace import ExperimentBuilderWorkspace
from experiment_results import ExperimentResultsPage


class MainInterface(QMainWindow):
    """Main application window that manages different pages"""
    
    def __init__(self):
        super().__init__()
        self.api_user = self._authenticate_if_required()
        # Status Bar
        self.status_msg = QLabel("Ready")
        self.statusBar().addWidget(self.status_msg)
        self.version_label = QLabel(APP_VERSION_LABEL)
        self.version_label.setStyleSheet("color: #6f7d8c; font-size: 11px; padding-left: 12px;")
        self.statusBar().addPermanentWidget(self.version_label)

        self.setWindowTitle(APP_NAME)
        self.resize(1200, 800)
        
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        
        self.main_menu = MainMenu(self)
        self.builder_workspace = ExperimentBuilderWorkspace(self)
        self.new_experiment = NewExperimentWizard(self)
        self.experiment_properties = ExperimentPropertiesPage(self)
        self.experiment_results = ExperimentResultsPage(self)
        self.active_builder = self.builder_workspace
        self.editing_block_key = None
        
        self.stack.addWidget(self.main_menu)
        self.stack.addWidget(self.builder_workspace)
        self.stack.addWidget(self.new_experiment)
        self.stack.addWidget(self.experiment_properties)
        self.stack.addWidget(self.experiment_results)

        # Always start on the home screen.
        self.show_main_menu()
        QTimer.singleShot(0, self._retry_pending_uploads)

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
        try:
            from result_upload_queue import drain_upload_queue
            uploaded, errors = drain_upload_queue(AutoScriptAPI(timeout=30))
            if uploaded:
                self.status_msg.setText(f"Uploaded {uploaded} queued result(s)")
            elif errors:
                self.status_msg.setText("Pending result uploads remain queued")
        except Exception:
            pass
        
    def show_new_experiment(self):
        self.stack.setCurrentWidget(self.new_experiment)

    def show_main_menu(self, refresh=False):
        if refresh:
            self.main_menu.refresh_experiments()
        self.stack.setCurrentWidget(self.main_menu)

    def open_new_experiment(self):
        self.builder_workspace.new_experiment()
        self.stack.setCurrentWidget(self.builder_workspace)

    def open_experiment_builder(self, experiment):
        self.builder_workspace.load_experiment(experiment)
        self.stack.setCurrentWidget(self.builder_workspace)

    def open_experiment_results(self, experiment):
        self.experiment_results.set_experiment(experiment)
        self.stack.setCurrentWidget(self.experiment_results)

    def start_new_block(self, workspace):
        self.active_builder = workspace
        self.editing_block_key = None
        self.new_experiment.reset_draft()
        self.experiment_properties.reset_defaults(
            f"Block {len(workspace.blocks) + 1}"
        )
        self.stack.setCurrentWidget(self.new_experiment)

    def start_edit_block(self, workspace, block_key, package_path):
        self.active_builder = workspace
        self.editing_block_key = block_key
        self.new_experiment.reset_draft()
        if self.new_experiment.import_experiment_zip(
            str(package_path),
            replace_without_prompt=True,
        ):
            self.stack.setCurrentWidget(self.new_experiment)

    def cancel_block_edit(self):
        self.stack.setCurrentWidget(self.active_builder)

    def finish_block_edit(self, package_path, block_name):
        self.active_builder.add_or_replace_block(
            package_path,
            block_name,
            editing_key=self.editing_block_key,
        )
        self.editing_block_key = None
        self.stack.setCurrentWidget(self.active_builder)

    def experiment_saved(self, experiment_id):
        self.status_msg.setText("Experiment saved")
        self.show_main_menu(refresh=True)
        
    def show_experiment_properties(self, audio_groups, loaded_properties=None):
        self.experiment_properties.set_data(audio_groups, loaded_properties)
        self.stack.setCurrentWidget(self.experiment_properties)


if __name__ == "__main__":
    ensure_qt_platform_plugin_path()
    app = QApplication(sys.argv)
    window = MainInterface()
    window.show()
    sys.exit(app.exec_())
