"""Main Interface dialog for independent component installation and updates."""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from component_runtime import create_update_manager
from component_update_manager import UpdateError
from component_versions import COMPONENT_NAMES, COMPONENT_VERSIONS, compare_component_versions


DISPLAY_NAMES = {
    "interface": "Main Interface",
    "builder": "Builder",
    "runner": "Runner",
    "analyzer": "Analyzer",
}


class ComponentUpdatesDialog(QDialog):
    component_installed = pyqtSignal(str, str)

    def __init__(self, parent=None, *, manager=None, bundled_versions=None):
        super().__init__(parent)
        self.manager = manager or create_update_manager()
        self.bundled_versions = dict(bundled_versions or COMPONENT_VERSIONS)
        self.catalog = None
        self._buttons = {}
        self._rows = {}

        self.setWindowTitle("AutoScript Updates")
        self.resize(760, 360)
        layout = QVBoxLayout(self)

        explanation = QLabel(
            "Install or update each AutoScript component independently. "
            "Downloads are signature and checksum verified before activation."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.table = QTableWidget(len(COMPONENT_NAMES), 5)
        self.table.setHorizontalHeaderLabels(
            ["Component", "Installed version", "Latest version", "Status", "Action"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        for row, component in enumerate(COMPONENT_NAMES):
            self._rows[component] = row
            self.table.setItem(row, 0, QTableWidgetItem(DISPLAY_NAMES[component]))
            for column in (1, 2, 3):
                self.table.setItem(row, column, QTableWidgetItem("—"))
            button = QPushButton("Install")
            button.setEnabled(False)
            button.clicked.connect(
                lambda checked=False, name=component: self.install_component(name)
            )
            self.table.setCellWidget(row, 4, button)
            self._buttons[component] = button

        controls = QHBoxLayout()
        self.status = QLabel("Checking for updates…")
        self.status.setWordWrap(True)
        controls.addWidget(self.status, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_catalog)
        controls.addWidget(refresh)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        controls.addWidget(close)
        layout.addLayout(controls)

        self._render_rows()

    def showEvent(self, event):
        super().showEvent(event)
        if self.catalog is None:
            self.refresh_catalog()

    def _installed_version(self, component):
        installed = self.manager.get_installed_component(component)
        if installed is not None:
            return installed.version, False
        if component == "interface":
            return self.bundled_versions[component], True
        return None, False

    def _render_rows(self):
        for component in COMPONENT_NAMES:
            row = self._rows[component]
            installed, bundled = self._installed_version(component)
            latest = (
                self.catalog.components[component].version
                if self.catalog is not None
                else None
            )
            installed_label = installed or "Not installed"
            if bundled:
                installed_label += " (bundled)"
            self.table.item(row, 1).setText(installed_label)
            self.table.item(row, 2).setText(latest or "Unavailable")

            button = self._buttons[component]
            if latest is None:
                state, action, enabled = "Latest version unavailable", "Install", False
            elif installed is None:
                state, action, enabled = "Not installed", "Install", True
            else:
                comparison = compare_component_versions(latest, installed)
                if comparison > 0:
                    state, action, enabled = "Update available", "Update", True
                elif comparison == 0:
                    state, action, enabled = "Up to date", "Up to date", False
                else:
                    state, action, enabled = "Installed version is newer", "Installed", False
            self.table.item(row, 3).setText(state)
            button.setText(action)
            button.setEnabled(enabled)

    def refresh_catalog(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.catalog = self.manager.load_catalog()
            source = self.catalog.source.replace("-", " ")
            self.status.setText(
                f"Latest signed catalog loaded from {source} "
                f"(sequence {self.catalog.sequence})."
            )
        except Exception as exc:
            self.catalog = None
            self.status.setText(f"Updates unavailable: {exc}")
        finally:
            QApplication.restoreOverrideCursor()
            self._render_rows()

    def install_component(self, component):
        if self.catalog is None:
            return
        button = self._buttons[component]
        old_text = button.text()
        button.setEnabled(False)
        button.setText("Installing…")
        QApplication.processEvents()
        try:
            installed = self.manager.install_component(self.catalog, component)
            self.component_installed.emit(component, installed.version)
            if component == "interface":
                message = (
                    "The Main Interface update is installed. Close and reopen "
                    "AutoScript to activate it."
                )
            else:
                message = f"{DISPLAY_NAMES[component]} {installed.version} is ready."
            QMessageBox.information(self, "Component Ready", message)
            self.status.setText(message)
        except (UpdateError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Update Failed", str(exc))
            self.status.setText(f"{DISPLAY_NAMES[component]} update failed: {exc}")
        finally:
            button.setText(old_text)
            self._render_rows()

    def row_snapshot(self, component):
        row = self._rows[component]
        return {
            "installed": self.table.item(row, 1).text(),
            "latest": self.table.item(row, 2).text(),
            "status": self.table.item(row, 3).text(),
            "action": self._buttons[component].text(),
            "enabled": self._buttons[component].isEnabled(),
        }


__all__ = ["ComponentUpdatesDialog", "DISPLAY_NAMES"]
