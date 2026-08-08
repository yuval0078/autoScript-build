"""Experiment participant/run management screen."""

import json
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app_paths import ensure_dir, user_data_dir
from autoscript_api import APIError, AutoScriptAPI


def run_status(run):
    """Return semantic status, color, and explicit human-readable labels."""
    if not run.get("complete", False):
        return (
            "incomplete",
            "#c62828",
            "#fff0f0",
            "Data incomplete",
            _analysis_label(run),
        )
    if run.get("analysis_completed") is not True:
        return (
            "analysis_pending",
            "#b26a00",
            "#fff7e3",
            "Data complete",
            _analysis_label(run),
        )
    return (
        "complete",
        "#237a3b",
        "#eef9f0",
        "Data complete",
        "Analysis completed",
    )


def _analysis_label(run):
    if run.get("analysis_completed") is True:
        return "Analysis completed"
    if run.get("analysis_completed") is False:
        return "Analysis not completed"
    return "Analysis not started"


class ExperimentResultsPage(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.api = AutoScriptAPI(timeout=30)
        self.experiment = None
        self.runs = []
        self.selected_ids = set()
        self.row_checkboxes = []
        self.analysis_processes = []
        self._build_ui()

        self.process_timer = QTimer(self)
        self.process_timer.setInterval(1200)
        self.process_timer.timeout.connect(self._poll_analysis_processes)
        self.process_timer.start()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(36, 26, 36, 26)
        layout.setSpacing(14)

        header = QHBoxLayout()
        back = QPushButton("←  Experiments")
        back.clicked.connect(lambda: self.parent.show_main_menu(refresh=True))
        back.setStyleSheet(self._button_style("#536578"))
        header.addWidget(back)

        title_area = QVBoxLayout()
        self.title = QLabel("Experiment Results")
        self.title.setStyleSheet("font-size: 28px; font-weight: 700; color: #172230;")
        self.subtitle = QLabel("Select participant runs to analyze, download, or delete.")
        self.subtitle.setStyleSheet("color: #657585; font-size: 13px;")
        title_area.addWidget(self.title)
        title_area.addWidget(self.subtitle)
        header.addLayout(title_area, 1)

        refresh = QPushButton("↻  Refresh")
        refresh.clicked.connect(self.refresh_runs)
        refresh.setStyleSheet(self._button_style("#356b9b"))
        header.addWidget(refresh)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.raw_button = self._toolbar_button(
            "↓ Raw data", self.download_raw, "#2463a8"
        )
        self.csv_button = self._toolbar_button(
            "↓ Analyzed CSV", lambda: self.download_artifacts("analysis_csv"), "#16788c"
        )
        self.training_button = self._toolbar_button(
            "↓ Training JSON", lambda: self.download_artifacts("trainable_json"), "#6b4ca5"
        )
        self.analyze_button = self._toolbar_button(
            "▶ Analyze selected", self.analyze_selected, "#198a43"
        )
        self.delete_button = self._toolbar_button(
            "✕ Delete selected", self.delete_selected, "#c62828"
        )
        for button in (
            self.raw_button,
            self.csv_button,
            self.training_button,
            self.analyze_button,
            self.delete_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        column_header = QFrame()
        column_header.setStyleSheet(
            "QFrame { background: #e9eef4; border-radius: 8px; }"
        )
        column_layout = QHBoxLayout(column_header)
        column_layout.setContentsMargins(14, 8, 14, 8)
        self.select_all = QCheckBox("Select all")
        self.select_all.stateChanged.connect(self._toggle_all)
        column_layout.addWidget(self.select_all)
        column_layout.addSpacing(18)
        column_layout.addWidget(QLabel("Participant / run"), 1)
        column_layout.addWidget(QLabel("Status"))
        layout.addWidget(column_header)

        self.status_label = QLabel("No participant runs yet.")
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
        self._update_actions()

    @staticmethod
    def _button_style(color):
        return (
            f"QPushButton {{ background: white; color: {color}; border: 1px solid #ccd7e2; "
            "border-radius: 8px; padding: 9px 13px; font-weight: 650; }} "
            "QPushButton:hover { background: #eef5fb; } "
            "QPushButton:disabled { color: #a8b0b8; background: #f3f5f7; }"
        )

    def _toolbar_button(self, text, callback, color):
        button = QPushButton(text)
        button.setMinimumHeight(38)
        button.setStyleSheet(self._button_style(color))
        button.clicked.connect(callback)
        return button

    def set_experiment(self, experiment):
        self.experiment = dict(experiment)
        self.title.setText(f"Results — {experiment['name']}")
        self.selected_ids.clear()
        self.refresh_runs()

    def _clear_rows(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.row_checkboxes = []

    def refresh_runs(self):
        if not self.experiment:
            return
        self.status_label.setText("Refreshing participant runs…")
        QApplication.processEvents()
        try:
            self.runs = self.api.list_experiment_runs(self.experiment["id"])
        except APIError as exc:
            self.runs = []
            self._clear_rows()
            self.status_label.setText(f"Cloud API unavailable: {exc}")
            return

        available_ids = {run["id"] for run in self.runs}
        self.selected_ids.intersection_update(available_ids)
        self._clear_rows()
        for run in self.runs:
            self.list_layout.insertWidget(
                self.list_layout.count() - 1,
                self._build_run_row(run),
            )
        count = len(self.runs)
        self.status_label.setText(
            "No participant runs for this experiment yet."
            if not count
            else f"{count} participant run{'s' if count != 1 else ''}"
        )
        self._sync_select_all()
        self._update_actions()

    def _build_run_row(self, run):
        _status, color, background, data_label, analysis_label = run_status(run)
        card = QFrame()
        card.setObjectName("resultCard")
        card.setStyleSheet(
            f"QFrame#resultCard {{ background: {background}; border: 1px solid {color}; "
            "border-radius: 9px; }}"
        )
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 11, 14, 11)
        row.setSpacing(12)

        checkbox = QCheckBox()
        checkbox.setChecked(run["id"] in self.selected_ids)
        checkbox.stateChanged.connect(
            lambda state, run_id=run["id"]: self._selection_changed(run_id, state)
        )
        self.row_checkboxes.append((run["id"], checkbox))
        row.addWidget(checkbox)

        identity = QVBoxLayout()
        participant = QLabel(f"Participant {run['participant_number']}")
        participant.setStyleSheet("font-size: 16px; font-weight: 700; color: #172230;")
        details = QLabel(
            f"Age {run['participant_age']} · {run['participant_gender']} · "
            f"Session {run['session_id']}"
        )
        details.setStyleSheet("font-size: 12px; color: #617181;")
        identity.addWidget(participant)
        identity.addWidget(details)
        row.addLayout(identity, 1)

        status_area = QVBoxLayout()
        blocks = f"{run.get('result_count', 0)}/{run.get('block_count', 0)} Blocks"
        words = (
            f"{run.get('completed_word_count', 0)}/"
            f"{run.get('expected_word_count', 0)} words"
        )
        data = QLabel(f"{data_label} — {words}, {blocks}")
        data.setAlignment(Qt.AlignRight)
        data.setStyleSheet(f"font-weight: 700; color: {color};")
        analysis = QLabel(analysis_label)
        analysis.setAlignment(Qt.AlignRight)
        analysis.setStyleSheet(f"color: {color};")
        status_area.addWidget(data)
        status_area.addWidget(analysis)
        row.addLayout(status_area)
        return card

    def _selection_changed(self, run_id, state):
        if state == Qt.Checked:
            self.selected_ids.add(run_id)
        else:
            self.selected_ids.discard(run_id)
        self._sync_select_all()
        self._update_actions()

    def _toggle_all(self, state):
        checked = state == Qt.Checked
        self.selected_ids = {run["id"] for run in self.runs} if checked else set()
        for _run_id, checkbox in self.row_checkboxes:
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self._update_actions()

    def _sync_select_all(self):
        self.select_all.blockSignals(True)
        self.select_all.setChecked(bool(self.runs) and len(self.selected_ids) == len(self.runs))
        self.select_all.blockSignals(False)

    def _update_actions(self):
        enabled = bool(self.selected_ids)
        for button in (
            self.raw_button,
            self.csv_button,
            self.training_button,
            self.analyze_button,
            self.delete_button,
        ):
            button.setEnabled(enabled)

    def _selected_runs(self):
        return [run for run in self.runs if run["id"] in self.selected_ids]

    @staticmethod
    def _safe_name(value):
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in str(value)
        ).strip("_")
        return safe or "participant"

    def download_raw(self):
        runs = self._selected_runs()
        if not runs:
            return
        default_name = f"{self._safe_name(self.experiment['name'])}_raw_results.zip"
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Download Raw Results", default_name, "ZIP Files (*.zip)"
        )
        if not save_path:
            return
        output_path = Path(save_path).with_suffix(".zip")
        try:
            with tempfile.TemporaryDirectory(prefix="autoscript-raw-export-") as temp_dir:
                temp_root = Path(temp_dir)
                with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
                    for run in runs:
                        run_folder = (
                            f"participant_{run['participant_number']}_"
                            f"{self._safe_name(run['session_id'])}"
                        )
                        for result in sorted(
                            run.get("results", []),
                            key=lambda item: item.get("block_index", 0),
                        ):
                            filename = (
                                f"{result.get('block_index', 0):03d}_"
                                f"{self._safe_name(result.get('block_name', 'block'))}.json"
                            )
                            local_path = temp_root / f"{uuid.uuid4().hex}.json"
                            self.api.download_run_result(result, local_path)
                            archive.write(local_path, f"{run_folder}/{filename}")
            QMessageBox.information(self, "Download Complete", f"Saved to:\n{output_path}")
        except (APIError, OSError, ValueError) as exc:
            output_path.unlink(missing_ok=True)
            QMessageBox.critical(self, "Download Failed", str(exc))

    def _latest_artifact(self, run, kind):
        return next(
            (artifact for artifact in run.get("artifacts", []) if artifact["kind"] == kind),
            None,
        )

    def download_artifacts(self, kind):
        runs = self._selected_runs()
        if not runs:
            return
        artifacts = [(run, self._latest_artifact(run, kind)) for run in runs]
        missing = [run for run, artifact in artifacts if artifact is None]
        if missing:
            names = ", ".join(str(run["participant_number"]) for run in missing)
            QMessageBox.warning(
                self,
                "Analysis Export Unavailable",
                f"No {'CSV' if kind == 'analysis_csv' else 'training JSON'} is stored "
                f"for participant(s): {names}.\n\nAnalyze them first and close the Analyzer.",
            )
            return

        suffix = ".csv" if kind == "analysis_csv" else ".json"
        label = "Analyzed CSV" if kind == "analysis_csv" else "Training JSON"
        try:
            if len(artifacts) == 1:
                run, artifact = artifacts[0]
                default_name = (
                    f"participant_{run['participant_number']}_"
                    f"{self._safe_name(run['session_id'])}{suffix}"
                )
                save_path, _ = QFileDialog.getSaveFileName(
                    self, f"Download {label}", default_name, f"*{suffix}"
                )
                if not save_path:
                    return
                output_path = Path(save_path).with_suffix(suffix)
                self.api.download_run_artifact(artifact, output_path)
            else:
                default_name = (
                    f"{self._safe_name(self.experiment['name'])}_"
                    f"{'analysis_csv' if kind == 'analysis_csv' else 'training_json'}.zip"
                )
                save_path, _ = QFileDialog.getSaveFileName(
                    self, f"Download {label}", default_name, "ZIP Files (*.zip)"
                )
                if not save_path:
                    return
                output_path = Path(save_path).with_suffix(".zip")
                with tempfile.TemporaryDirectory(prefix="autoscript-artifact-export-") as temp_dir:
                    temp_root = Path(temp_dir)
                    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
                        for run, artifact in artifacts:
                            filename = (
                                f"participant_{run['participant_number']}_"
                                f"{self._safe_name(run['session_id'])}{suffix}"
                            )
                            local_path = temp_root / f"{uuid.uuid4().hex}{suffix}"
                            self.api.download_run_artifact(artifact, local_path)
                            archive.write(local_path, filename)
            QMessageBox.information(self, "Download Complete", f"Saved to:\n{output_path}")
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Download Failed", str(exc))

    def analyze_selected(self):
        runs = self._selected_runs()
        if not runs:
            return
        try:
            workspace = ensure_dir(
                user_data_dir()
                / "analyzer_downloads"
                / f"analysis_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            )
            inputs = ensure_dir(workspace / "raw")
            output_dir = ensure_dir(workspace / "exports")
            downloaded = []
            context_runs = []
            for run in runs:
                context_run = {"id": run["id"], "session_id": run["session_id"]}
                state_artifact = self._latest_artifact(run, "analysis_state")
                if state_artifact is not None:
                    state_path = workspace / f"{run['id']}_analysis_state.json"
                    self.api.download_run_artifact(state_artifact, state_path)
                    context_run["analysis_state_path"] = str(state_path)
                context_runs.append(context_run)
                for result in sorted(
                    run.get("results", []), key=lambda item: item.get("block_index", 0)
                ):
                    filename = (
                        f"{run['id'][:8]}_{result.get('block_index', 0):03d}_"
                        f"{self._safe_name(result.get('block_name', 'block'))}.json"
                    )
                    destination = inputs / filename
                    self.api.download_run_result(result, destination)
                    downloaded.append(str(destination))
            context_path = workspace / "analysis_context.json"
            context_path.write_text(
                json.dumps(
                    {
                        "api_url": self.api.base_url,
                        "experiment_id": self.experiment["id"],
                        "experiment_name": self.experiment["name"],
                        "runs": context_runs,
                        "output_dir": str(output_dir),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            process = self.parent.main_menu.launch_analyzer(
                downloaded,
                extra_args=["--autoscript-context", str(context_path)],
            )
            if process is not None:
                self.analysis_processes.append(process)
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Analyze Results Failed", str(exc))

    def _poll_analysis_processes(self):
        running = []
        finished = False
        for process in self.analysis_processes:
            if process.poll() is None:
                running.append(process)
            else:
                finished = True
        self.analysis_processes = running
        if finished and self.experiment:
            self.refresh_runs()

    def delete_selected(self):
        runs = self._selected_runs()
        if not runs:
            return
        answer = QMessageBox.warning(
            self,
            "Delete Participant Results",
            f"Permanently delete {len(runs)} selected participant run(s), including "
            "all raw and analyzed files?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            for run in runs:
                self.api.delete_run(run["id"])
            self.selected_ids.clear()
            self.refresh_runs()
        except APIError as exc:
            QMessageBox.critical(self, "Delete Failed", str(exc))
