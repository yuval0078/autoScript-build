"""Main Interface dialog for independent component installation and updates."""

from __future__ import annotations

import threading

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
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


class ComponentUpdateCoordinator(QObject):
    """Run one component installation at a time without blocking the Qt thread."""

    progress_changed = pyqtSignal(str, int)
    install_finished = pyqtSignal(str, object)
    install_failed = pyqtSignal(str, str)

    def __init__(self, manager=None, parent=None):
        super().__init__(parent)
        self.manager = manager or create_update_manager()
        self._state_lock = threading.Lock()
        self._active_component = None
        self._active_catalog = None
        self._progress = 0
        self._thread = None

    @property
    def active_component(self):
        with self._state_lock:
            return self._active_component

    @property
    def active_catalog(self):
        with self._state_lock:
            return self._active_catalog

    @property
    def progress(self):
        with self._state_lock:
            return self._progress

    def is_installing(self, component=None):
        active = self.active_component
        return active is not None and (component is None or active == component)

    def start_install(self, catalog, component):
        with self._state_lock:
            if self._active_component is not None:
                return False
            self._active_component = component
            self._active_catalog = catalog
            self._progress = 0

        def publish_progress(value):
            value = max(0, min(100, int(value)))
            with self._state_lock:
                if self._active_component != component:
                    return
                self._progress = value
            self.progress_changed.emit(component, value)

        def install():
            try:
                installed = self.manager.install_component(
                    catalog, component, progress=publish_progress
                )
            except (UpdateError, OSError, ValueError) as exc:
                with self._state_lock:
                    self._active_component = None
                    self._active_catalog = None
                self.install_failed.emit(component, str(exc))
                return
            except Exception as exc:
                with self._state_lock:
                    self._active_component = None
                    self._active_catalog = None
                self.install_failed.emit(component, str(exc))
                return
            with self._state_lock:
                self._progress = 100
                self._active_component = None
                self._active_catalog = None
            self.install_finished.emit(component, installed)

        self._thread = threading.Thread(
            target=install,
            name=f"autoscript-{component}-update",
            daemon=True,
        )
        self.progress_changed.emit(component, 0)
        self._thread.start()
        return True


class ComponentUpdatesDialog(QDialog):
    component_installed = pyqtSignal(str, str)
    interface_update_started = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        manager=None,
        bundled_versions=None,
        coordinator=None,
    ):
        super().__init__(parent)
        self.manager = manager or (
            coordinator.manager if coordinator is not None else create_update_manager()
        )
        self.coordinator = coordinator or ComponentUpdateCoordinator(
            self.manager, self
        )
        self.bundled_versions = dict(bundled_versions or COMPONENT_VERSIONS)
        self.catalog = self.coordinator.active_catalog
        self._buttons = {}
        self._rows = {}

        self.coordinator.progress_changed.connect(self._on_install_progress)
        self.coordinator.install_finished.connect(self._on_install_finished)
        self.coordinator.install_failed.connect(self._on_install_failed)

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

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setVisible(self.coordinator.is_installing())
        layout.addWidget(self.progress_bar)

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
        if self.coordinator.active_component is not None:
            self._on_install_progress(
                self.coordinator.active_component, self.coordinator.progress
            )

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
            if self.coordinator.is_installing(component):
                state, action, enabled = "Installing…", "Installing…", False
            elif self.coordinator.is_installing():
                state, action, enabled = state_for_component(installed, latest)
                enabled = False
            else:
                state, action, enabled = state_for_component(installed, latest)
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
        if not self.coordinator.start_install(self.catalog, component):
            QMessageBox.information(
                self,
                "Update Already Running",
                "Wait until the current component update is finished.",
            )
            return
        self._on_install_progress(component, 0)
        self._render_rows()
        if component == "interface":
            self.accept()
            self.interface_update_started.emit()

    def _on_install_progress(self, component, percentage):
        if component != self.coordinator.active_component and percentage < 100:
            return
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(percentage)
        self.status.setText(
            f"Installing {DISPLAY_NAMES[component]}… {percentage}%"
        )

    def _on_install_finished(self, component, installed):
        self.component_installed.emit(component, installed.version)
        self.progress_bar.setValue(100)
        message = f"{DISPLAY_NAMES[component]} {installed.version} is ready."
        self.status.setText(message)
        self._render_rows()
        if self.isVisible() and component != "interface":
            QMessageBox.information(self, "Component Ready", message)

    def _on_install_failed(self, component, error):
        self.progress_bar.setVisible(False)
        message = f"{DISPLAY_NAMES[component]} update failed: {error}"
        self.status.setText(message)
        self._render_rows()
        if self.isVisible() and component != "interface":
            QMessageBox.critical(self, "Update Failed", error)

    def row_snapshot(self, component):
        row = self._rows[component]
        return {
            "installed": self.table.item(row, 1).text(),
            "latest": self.table.item(row, 2).text(),
            "status": self.table.item(row, 3).text(),
            "action": self._buttons[component].text(),
            "enabled": self._buttons[component].isEnabled(),
            "progress": self.progress_bar.value(),
        }


def state_for_component(installed, latest):
    if latest is None:
        return "Latest version unavailable", "Install", False
    if installed is None:
        return "Not installed", "Install", True
    comparison = compare_component_versions(latest, installed)
    if comparison > 0:
        return "Update available", "Update", True
    if comparison == 0:
        return "Up to date", "Up to date", False
    return "Installed version is newer", "Installed", False


__all__ = [
    "ComponentUpdateCoordinator",
    "ComponentUpdatesDialog",
    "DISPLAY_NAMES",
]
