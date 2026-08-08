"""Standalone AutoScript Experiment Builder application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
)

from autoscript_api import APIError, AutoScriptAPI
from builder_workspace import ExperimentBuilderWorkspace
from exp_initializer import ExperimentPropertiesPage, NewExperimentWizard
from component_versions import get_component_version
from qt_bootstrap import ensure_qt_platform_plugin_path


class BuilderInterface(QMainWindow):
    """Top-level host for every screen that belongs to the Builder."""

    def __init__(
        self,
        *,
        experiment_id: Optional[str] = None,
        import_block: Optional[str] = None,
        api: Optional[AutoScriptAPI] = None,
        authenticate: bool = True,
    ):
        super().__init__()
        self.api = api or AutoScriptAPI(timeout=10)
        self.api_user = self._authenticate_if_required() if authenticate else None
        self.saved_experiment_id = None
        self.editing_block_key = None

        self.status_msg = QLabel("Ready")
        self.statusBar().addWidget(self.status_msg)
        version_label = QLabel(f"Builder {get_component_version('builder')}")
        version_label.setStyleSheet(
            "color: #6f7d8c; font-size: 11px; padding-left: 12px;"
        )
        self.statusBar().addPermanentWidget(version_label)

        self.setWindowTitle("AutoScript Experiment Builder")
        self.resize(1200, 800)

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.builder_workspace = ExperimentBuilderWorkspace(self)
        # Reuse the authenticated client rather than creating a second session.
        self.builder_workspace.api = self.api
        self.new_experiment = NewExperimentWizard(self)
        self.experiment_properties = ExperimentPropertiesPage(self)
        self.active_builder = self.builder_workspace

        self.stack.addWidget(self.builder_workspace)
        self.stack.addWidget(self.new_experiment)
        self.stack.addWidget(self.experiment_properties)

        if import_block:
            self.open_imported_block(import_block)
        elif experiment_id:
            self.open_experiment(experiment_id)
        else:
            self.open_new_experiment()

    def _authenticate_if_required(self):
        try:
            return self.api.me()
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
                return self.api.login(username.strip(), password)["user"]
            except APIError as exc:
                retry = QMessageBox.warning(
                    self,
                    "Login Failed",
                    str(exc),
                    QMessageBox.Retry | QMessageBox.Cancel,
                    QMessageBox.Retry,
                )
                if retry != QMessageBox.Retry:
                    return None

    def open_new_experiment(self):
        self.active_builder = self.builder_workspace
        self.editing_block_key = None
        self.builder_workspace.new_experiment()
        self.stack.setCurrentWidget(self.builder_workspace)

    def open_experiment(self, experiment_id: str):
        try:
            experiment = self.api.get_experiment(experiment_id)
        except APIError as exc:
            title = "Experiment Not Found" if exc.status_code == 404 else "Load Failed"
            QMessageBox.critical(self, title, str(exc))
            self.open_new_experiment()
            return

        self.active_builder = self.builder_workspace
        self.editing_block_key = None
        self.builder_workspace.load_experiment(experiment)
        self.stack.setCurrentWidget(self.builder_workspace)

    def open_imported_block(self, package_path: str):
        self.open_new_experiment()
        self.new_experiment.reset_draft()
        if self.new_experiment.import_experiment_zip(str(Path(package_path))):
            self.stack.setCurrentWidget(self.new_experiment)

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
        self.saved_experiment_id = str(experiment_id)
        self.status_msg.setText("Experiment saved")
        # Closing the Builder is the process-level success signal. The Main
        # Interface observes the child exit and refreshes its cloud list.
        QTimer.singleShot(0, self.close)

    def show_experiment_properties(self, audio_groups, loaded_properties=None):
        self.experiment_properties.set_data(audio_groups, loaded_properties)
        self.stack.setCurrentWidget(self.experiment_properties)

    def show_new_experiment(self):
        self.stack.setCurrentWidget(self.new_experiment)

    def show_main_menu(self, refresh=False):
        """Compatibility callback used by the workspace's Back button."""

        self.close()


def parse_args(arguments=None):
    parser = argparse.ArgumentParser(description="AutoScript Experiment Builder")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--new", action="store_true", help="Create a new experiment")
    mode.add_argument("--experiment-id", help="Edit an existing cloud experiment")
    mode.add_argument("--import-block", help="Import a local or legacy Block ZIP")
    return parser.parse_args(arguments)


def main(arguments=None):
    args = parse_args(arguments)
    ensure_qt_platform_plugin_path()
    app = QApplication([sys.argv[0], *(arguments if arguments is not None else sys.argv[1:])])
    app.setStyle("Fusion")
    window = BuilderInterface(
        experiment_id=args.experiment_id,
        import_block=args.import_block,
    )
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
