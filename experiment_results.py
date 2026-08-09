"""Experiment participant/run management screen."""

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QIntValidator
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app_paths import ensure_dir, user_data_dir
from autoscript_api import APICancelled, APIError, AutoScriptAPI


ARTIFACT_TAGS = (
    ("raw_data", "Raw data"),
    ("analysis_csv", "Analyzed CSV"),
    ("trainable_json", "Trainable Json"),
)


def cloud_file_count(run, kind):
    """Return an artifact count while remaining compatible with older APIs."""
    count_key = {
        "raw_data": "raw_data_count",
        "analysis_csv": "analyzed_csv_count",
        "trainable_json": "trainable_json_count",
    }[kind]
    if count_key in run:
        try:
            return max(0, int(run[count_key]))
        except (TypeError, ValueError):
            return 0
    if kind == "raw_data":
        return len(run.get("results", []))
    return sum(
        1 for artifact in run.get("artifacts", []) if artifact.get("kind") == kind
    )


def run_cloud_file_tags(run):
    """Return the visible cloud-file tags for one participant run."""
    return [
        (kind, label, cloud_file_count(run, kind))
        for kind, label in ARTIFACT_TAGS
        if cloud_file_count(run, kind) > 0
    ]


def analysis_copy_count(run):
    """Best available estimate of saved analysis versions for Analyzer UX."""
    return max(
        cloud_file_count(run, "analysis_csv"),
        cloud_file_count(run, "trainable_json"),
    )


def format_run_datetime(value):
    """Render an API timestamp in the machine's local timezone."""
    if not value:
        return "Time unavailable"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(value)


class _BulkExportWorker(QObject):
    """Run one blocking bulk-export request away from the Qt UI thread."""

    progress = pyqtSignal(object, int, int)
    succeeded = pyqtSignal(object, str)
    failed = pyqtSignal(object, str)
    cancelled = pyqtSignal(object)
    done = pyqtSignal()

    def __init__(
        self,
        api,
        experiment_id,
        run_ids,
        include,
        destination,
        analysis_policy,
        cancel_event,
    ):
        super().__init__()
        self.api = api
        self.experiment_id = experiment_id
        self.run_ids = list(run_ids)
        self.include = list(include)
        self.destination = Path(destination)
        self.analysis_policy = analysis_policy
        self.cancel_event = cancel_event

    @pyqtSlot()
    def run(self):
        try:
            destination = self.api.download_experiment_bulk_export(
                self.experiment_id,
                self.run_ids,
                self.include,
                self.destination,
                analysis_policy=self.analysis_policy,
                progress=(
                    lambda received, total:
                    self.progress.emit(self, received, total)
                ),
                cancel_event=self.cancel_event,
            )
        except APICancelled:
            self.cancelled.emit(self)
        except Exception as exc:
            if self.cancel_event.is_set():
                self.cancelled.emit(self)
            else:
                self.failed.emit(self, str(exc))
        else:
            self.succeeded.emit(self, str(destination))
        finally:
            self.done.emit()


class ExperimentResultsPage(QWidget):
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.api = AutoScriptAPI(timeout=30)
        self.experiment = None
        self.runs = []
        self.selected_ids = set()
        self.row_checkboxes = []
        self.run_details = {}
        self.analysis_processes = []
        self._bulk_export_jobs = []
        self._run_cursor = None
        self._run_total_count = 0
        self._run_page_limit = 50
        self._run_loading = False
        self._build_ui()

        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(300)
        self.filter_timer.timeout.connect(self.refresh_runs)

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

        filters = QFrame()
        filters.setObjectName("runFilters")
        filters.setStyleSheet(
            "QFrame#runFilters { background: #f7fafc; border: 1px solid #d7e1ea; "
            "border-radius: 9px; } QLabel { color: #516273; }"
        )
        filter_layout = QGridLayout(filters)
        filter_layout.setContentsMargins(12, 10, 12, 10)
        filter_layout.setHorizontalSpacing(10)
        filter_layout.setVerticalSpacing(7)

        self.participant_filter = QLineEdit()
        self.participant_filter.setPlaceholderText("Any participant")
        self.participant_filter.setClearButtonEnabled(True)
        self.participant_filter.setValidator(QIntValidator(1, 2147483647, self))
        self.session_filter = QLineEdit()
        self.session_filter.setPlaceholderText("Search session ID...")
        self.session_filter.setClearButtonEnabled(True)
        self.session_filter.setMaxLength(128)
        self.status_filter = QComboBox()
        for label, value in (
            ("Any status", None),
            ("Created", "created"),
            ("Running", "running"),
            ("Completed", "completed"),
            ("Incomplete", "incomplete"),
            ("Failed", "failed"),
            ("Cancelled", "cancelled"),
        ):
            self.status_filter.addItem(label, value)
        self.complete_filter = self._boolean_filter_combo(
            "Any completeness", "Complete data", "Incomplete data"
        )
        self.raw_filter = self._boolean_filter_combo(
            "Any raw data", "Has raw data", "No raw data"
        )
        self.csv_filter = self._boolean_filter_combo(
            "Any analyzed CSV", "Has analyzed CSV", "No analyzed CSV"
        )
        self.trainable_filter = self._boolean_filter_combo(
            "Any trainable JSON", "Has trainable JSON", "No trainable JSON"
        )
        clear_filters = QPushButton("Clear filters")
        clear_filters.setStyleSheet(self._button_style("#536578"))
        clear_filters.clicked.connect(self._clear_filters)

        filter_layout.addWidget(QLabel("Participant"), 0, 0)
        filter_layout.addWidget(self.participant_filter, 0, 1)
        filter_layout.addWidget(QLabel("Session"), 0, 2)
        filter_layout.addWidget(self.session_filter, 0, 3)
        filter_layout.addWidget(QLabel("Status"), 0, 4)
        filter_layout.addWidget(self.status_filter, 0, 5)
        filter_layout.addWidget(self.complete_filter, 1, 0, 1, 2)
        filter_layout.addWidget(self.raw_filter, 1, 2)
        filter_layout.addWidget(self.csv_filter, 1, 3)
        filter_layout.addWidget(self.trainable_filter, 1, 4)
        filter_layout.addWidget(clear_filters, 1, 5)
        filter_layout.setColumnStretch(1, 1)
        filter_layout.setColumnStretch(3, 1)
        layout.addWidget(filters)

        self.participant_filter.textChanged.connect(self._queue_filter_refresh)
        self.session_filter.textChanged.connect(self._queue_filter_refresh)
        for control in (
            self.status_filter,
            self.complete_filter,
            self.raw_filter,
            self.csv_filter,
            self.trainable_filter,
        ):
            control.currentIndexChanged.connect(self._queue_filter_refresh)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.raw_button = self._toolbar_button(
            "↓ Raw data", self.download_raw, "#2463a8"
        )
        self.csv_button = self._toolbar_button(
            "↓ Analyzed CSV", lambda: self.download_artifacts("analysis_csv"), "#16788c"
        )
        self.training_button = self._toolbar_button(
            "↓ Trainable Json", lambda: self.download_artifacts("trainable_json"), "#6b4ca5"
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
        self.select_all = QCheckBox("Select all loaded")
        self.select_all.stateChanged.connect(self._toggle_all)
        column_layout.addWidget(self.select_all)
        column_layout.addSpacing(18)
        column_layout.addWidget(QLabel("Participant / run"), 1)
        column_layout.addWidget(QLabel("Cloud files / actions"))
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

        self.load_more_button = QPushButton("Load more participant runs")
        self.load_more_button.setStyleSheet(self._button_style("#356b9b"))
        self.load_more_button.clicked.connect(self.load_more_runs)
        self.load_more_button.hide()
        layout.addWidget(self.load_more_button)
        self._update_actions()

    @staticmethod
    def _boolean_filter_combo(any_label, yes_label, no_label):
        combo = QComboBox()
        combo.addItem(any_label, None)
        combo.addItem(yes_label, True)
        combo.addItem(no_label, False)
        return combo

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

    def _queue_filter_refresh(self, *_args):
        if hasattr(self, "filter_timer"):
            self._run_cursor = None
            self.load_more_button.hide()
            self.filter_timer.start()

    def _clear_filters(self):
        self.participant_filter.clear()
        self.session_filter.clear()
        for combo in (
            self.status_filter,
            self.complete_filter,
            self.raw_filter,
            self.csv_filter,
            self.trainable_filter,
        ):
            combo.setCurrentIndex(0)
        self._queue_filter_refresh()

    def _run_filter_parameters(self):
        participant_text = self.participant_filter.text().strip()
        return {
            "participant_number": (
                int(participant_text) if participant_text else None
            ),
            "session_search": self.session_filter.text().strip() or None,
            "status": self.status_filter.currentData(),
            "complete": self.complete_filter.currentData(),
            "has_raw_data": self.raw_filter.currentData(),
            "has_analyzed_csv": self.csv_filter.currentData(),
            "has_trainable_json": self.trainable_filter.currentData(),
        }

    def _clear_rows(self):
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.row_checkboxes = []

    def refresh_runs(self):
        self._load_run_page(reset=True)

    def load_more_runs(self):
        if self._run_cursor:
            self._load_run_page(reset=False)

    def _load_run_page(self, *, reset):
        if not self.experiment:
            return
        if self._run_loading:
            return
        self._run_loading = True
        try:
            self._load_run_page_once(reset=reset)
        finally:
            self._run_loading = False
            self.load_more_button.setEnabled(True)

    def _load_run_page_once(self, *, reset):
        if reset:
            self._run_cursor = None
            self.load_more_button.hide()
        self.status_label.setText(
            "Refreshing participant runs…"
            if reset else "Loading more participant runs…"
        )
        self.load_more_button.setEnabled(False)
        QApplication.processEvents()
        try:
            page = self.api.list_experiment_runs(
                self.experiment["id"],
                **self._run_filter_parameters(),
                include_files=False,
                limit=self._run_page_limit,
                cursor=None if reset else self._run_cursor,
            )
        except APIError as exc:
            if reset:
                self.runs = []
                self.selected_ids.clear()
                self._clear_rows()
                self._sync_select_all()
                self._update_actions()
            self.status_label.setText(f"Cloud API unavailable: {exc}")
            return

        if reset:
            self.runs = []
            self.run_details.clear()
            self.selected_ids.clear()
        known_ids = {run["id"] for run in self.runs}
        for run in page:
            if run["id"] in known_ids:
                continue
            summary = dict(run)
            summary["_files_included"] = False
            self.runs.append(summary)
        self._run_cursor = getattr(page, "next_cursor", None)
        if reset:
            self._run_total_count = getattr(page, "total_count", len(self.runs))
        elif not self._run_cursor and len(self.runs) != self._run_total_count:
            self._run_total_count = len(self.runs)
        self._clear_rows()
        for run in self.runs:
            self.list_layout.insertWidget(
                self.list_layout.count() - 1,
                self._build_run_row(run),
            )
        loaded_count = len(self.runs)
        self.status_label.setText(
            "No participant runs for this experiment yet."
            if not loaded_count
            else (
                f"Loaded {loaded_count} of {self._run_total_count} participant runs"
                if self._run_total_count > loaded_count
                else f"{loaded_count} participant run{'s' if loaded_count != 1 else ''}"
            )
        )
        self.load_more_button.setVisible(bool(self._run_cursor))
        self._sync_select_all()
        self._update_actions()

    def _build_run_row(self, run):
        card = QFrame()
        card.setObjectName("resultCard")
        card.setStyleSheet(
            "QFrame#resultCard { background: white; border: 1px solid #d6e0e9; "
            "border-radius: 9px; }"
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
            f"{format_run_datetime(run.get('started_at') or run.get('created_at'))} · "
            f"Session {run['session_id']}"
        )
        details.setStyleSheet("font-size: 12px; color: #617181;")
        identity.addWidget(participant)
        identity.addWidget(details)
        row.addLayout(identity, 1)

        status_area = QVBoxLayout()
        status_area.setSpacing(5)
        blocks = f"{run.get('result_count', 0)}/{run.get('block_count', 0)} Blocks"
        words = (
            f"{run.get('completed_word_count', 0)}/"
            f"{run.get('expected_word_count', 0)} words"
        )
        completeness = "Data complete" if run.get("complete", False) else "Data incomplete"
        data = QLabel(f"{completeness} — {words}, {blocks}")
        data.setAlignment(Qt.AlignRight)
        data.setStyleSheet("font-weight: 650; color: #536578;")
        status_area.addWidget(data)

        tags = QHBoxLayout()
        tags.setSpacing(6)
        tags.addStretch()
        for kind, label, _count in run_cloud_file_tags(run):
            tags.addWidget(self._build_file_tag(run, kind, label))
        status_area.addLayout(tags)
        row.addLayout(status_area)

        analyze = QPushButton("Analyze")
        analyze.setToolTip("Analyze only this participant run")
        analyze.setStyleSheet(self._button_style("#198a43"))
        analyze.setEnabled(cloud_file_count(run, "raw_data") > 0)
        analyze.clicked.connect(lambda _checked=False, item=run: self.analyze_run(item))
        row.addWidget(analyze)
        return card

    def _build_file_tag(self, run, kind, label):
        tag = QFrame()
        tag.setObjectName("fileTag")
        tag.setStyleSheet(
            "QFrame#fileTag { background: #edf4fa; border: 1px solid #c8d9e8; "
            "border-radius: 10px; }"
        )
        layout = QHBoxLayout(tag)
        layout.setContentsMargins(8, 2, 3, 2)
        layout.setSpacing(4)
        text = QLabel(label)
        text.setStyleSheet("color: #31536f; font-size: 11px; font-weight: 650;")
        layout.addWidget(text)
        download = QPushButton("↓")
        download.setToolTip(f"Download {label}")
        download.setFixedSize(23, 20)
        download.setStyleSheet(
            "QPushButton { border: 0; border-radius: 7px; color: #245f91; "
            "font-weight: 800; } QPushButton:hover { background: #d6e8f6; }"
        )
        if kind == "raw_data":
            download.clicked.connect(
                lambda _checked=False, item=run: self._download_raw_runs([item])
            )
        else:
            download.clicked.connect(
                lambda _checked=False, item=run, artifact_kind=kind:
                self.download_run_artifacts(item, artifact_kind)
            )
        layout.addWidget(download)
        return tag

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
            QMessageBox.information(
                self, "Nothing Selected", "Select at least one participant run."
            )
            return
        self._download_raw_runs(runs)

    def _download_raw_runs(self, runs):
        self._download_bulk_export(runs, ["raw_data"])

    def _download_bulk_export(self, runs, include, *, analysis_policy="latest"):
        if not runs:
            QMessageBox.information(
                self, "Nothing Selected", "Select at least one participant run."
            )
            return
        if len(runs) > 500:
            QMessageBox.warning(
                self,
                "Selection Too Large",
                "A single server export can contain up to 500 participant runs. "
                "Reduce the selection and try again.",
            )
            return
        names = {
            "raw_data": "raw_data",
            "analysis_csv": "analyzed_csv",
            "trainable_json": "trainable_json",
        }
        export_name = "_and_".join(names[kind] for kind in include)
        if any(kind in {"analysis_csv", "trainable_json"} for kind in include):
            export_name += f"_{analysis_policy}"
        default_name = (
            f"{self._safe_name(self.experiment['name'])}_{export_name}.zip"
        )
        if len(runs) == 1:
            run = runs[0]
            default_name = (
                f"participant_{run['participant_number']}_"
                f"{self._safe_name(run['session_id'])}_{export_name}.zip"
            )
        save_path, _ = QFileDialog.getSaveFileName(
            self, "Download Results", default_name, "ZIP Files (*.zip)"
        )
        if not save_path:
            return
        output_path = Path(save_path).with_suffix(".zip")
        progress_dialog = QProgressDialog(
            "Preparing server export...", "Cancel", 0, 100, self
        )
        progress_dialog.setWindowTitle("Downloading Results")
        progress_dialog.setWindowModality(Qt.WindowModal)
        progress_dialog.setMinimumDuration(0)
        progress_dialog.setValue(0)
        cancel_event = threading.Event()
        thread = QThread()
        worker = _BulkExportWorker(
            self.api,
            self.experiment["id"],
            [run["id"] for run in runs],
            include,
            output_path,
            analysis_policy,
            cancel_event,
        )
        worker.moveToThread(thread)
        job = {
            "thread": thread,
            "worker": worker,
            "dialog": progress_dialog,
            "cancel_event": cancel_event,
            "outcome_received": False,
            "thread_finished": False,
        }
        self._bulk_export_jobs.append(job)
        progress_dialog.canceled.connect(
            lambda current=job: self._cancel_bulk_export(current)
        )
        thread.started.connect(worker.run)
        # Bound QWidget slots guarantee queued delivery on the GUI thread. Bare
        # lambdas do not provide an explicit QObject receiver across Qt versions.
        worker.progress.connect(self._update_bulk_export_progress)
        worker.succeeded.connect(self._bulk_export_succeeded)
        worker.failed.connect(self._bulk_export_failed)
        worker.cancelled.connect(self._bulk_export_cancelled)
        worker.done.connect(thread.quit)
        worker.done.connect(worker.deleteLater)
        thread.finished.connect(self._release_bulk_export_job)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _bulk_export_job_for_source(self, source):
        return next(
            (
                job
                for job in self._bulk_export_jobs
                if source is job["worker"] or source is job["thread"]
            ),
            None,
        )

    @pyqtSlot(object, int, int)
    def _update_bulk_export_progress(self, worker, received, total):
        job = self._bulk_export_job_for_source(worker)
        if job is None:
            return
        if job["cancel_event"].is_set():
            return
        dialog = job["dialog"]
        if total:
            dialog.setValue(min(99, int(received * 100 / total)))
        else:
            dialog.setLabelText(f"Downloaded {received // 1024:,} KB...")

    def _cancel_bulk_export(self, job):
        if job["cancel_event"].is_set():
            return
        job["cancel_event"].set()
        self.status_label.setText("Cancelling download...")
        dialog = job["dialog"]
        dialog.setLabelText("Cancelling server export...")
        dialog.setCancelButton(None)

    @pyqtSlot(object, str)
    def _bulk_export_succeeded(self, worker, destination):
        job = self._bulk_export_job_for_source(worker)
        if job is None:
            return
        job["outcome_received"] = True
        job["dialog"].setValue(100)
        job["dialog"].close()
        QMessageBox.information(
            self, "Download Complete", f"Saved to:\n{destination}"
        )
        self._discard_bulk_export_job_if_finished(job)

    @pyqtSlot(object, str)
    def _bulk_export_failed(self, worker, message):
        job = self._bulk_export_job_for_source(worker)
        if job is None:
            return
        job["outcome_received"] = True
        job["dialog"].close()
        QMessageBox.critical(self, "Download Failed", message)
        self._discard_bulk_export_job_if_finished(job)

    @pyqtSlot(object)
    def _bulk_export_cancelled(self, worker):
        job = self._bulk_export_job_for_source(worker)
        if job is None:
            return
        job["outcome_received"] = True
        job["dialog"].close()
        self.status_label.setText("Download cancelled.")
        self._discard_bulk_export_job_if_finished(job)

    @pyqtSlot()
    def _release_bulk_export_job(self):
        job = self._bulk_export_job_for_source(self.sender())
        if job is None:
            return
        job["thread_finished"] = True
        self._discard_bulk_export_job_if_finished(job)

    def _discard_bulk_export_job_if_finished(self, job):
        # Worker outcome signals and QThread.finished are queued from different
        # QObject senders, so Qt does not guarantee their cross-sender order.
        if not (job["outcome_received"] and job["thread_finished"]):
            return
        job["dialog"].close()
        if job in self._bulk_export_jobs:
            self._bulk_export_jobs.remove(job)

    def _analysis_copies(self, run, kind):
        """Load versioned exports, with a flat-artifact fallback for older servers."""
        run = self._get_run_detail(run)
        try:
            revisions = self.api.list_run_analysis_copies(run["id"])
        except APIError as exc:
            if exc.status_code != 404:
                raise
            revisions = []

        copies = []
        seen_artifacts = set()
        field = "analyzed_csv" if kind == "analysis_csv" else "trainable_json"
        for revision in revisions:
            artifact = revision.get(field)
            if not artifact:
                continue
            seen_artifacts.add(str(artifact.get("id")))
            copies.append({**revision, "artifact": artifact, "legacy": False})

        for artifact in run.get("artifacts", []):
            if artifact.get("kind") != kind or str(artifact.get("id")) in seen_artifacts:
                continue
            copies.append({
                "id": None,
                "revision": None,
                "created_at": artifact.get("created_at"),
                "completed": run.get("analysis_completed"),
                "is_current_editable": False,
                "artifact": artifact,
                "legacy": True,
            })
        copies.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return copies

    def _get_run_detail(self, run):
        """Fetch nested files lazily for actions that actually require them."""
        if run.get("_files_included") is not False and (
            "results" in run or "artifacts" in run
        ):
            return run
        run_id = run["id"]
        if not hasattr(self, "run_details"):
            self.run_details = {}
        if run_id not in self.run_details:
            self.run_details[run_id] = self.api.get_experiment_run(run_id)
        return self.run_details[run_id]

    @staticmethod
    def _artifact_details(kind):
        if kind == "analysis_csv":
            return "Analyzed CSV", ".csv", "analysis_csv"
        return "Trainable Json", ".json", "trainable_json"

    def _copy_filename(self, run, copy, kind, *, include_version=False):
        _label, suffix, stem = self._artifact_details(kind)
        filename = (
            f"participant_{run['participant_number']}_"
            f"{self._safe_name(run['session_id'])}_{stem}"
        )
        if include_version:
            created = self._safe_name(
                format_run_datetime(copy.get("created_at")).replace(" ", "_").replace(":", "-")
            )
            revision = copy.get("revision")
            filename += f"_{'r' + str(revision) if revision is not None else 'legacy'}_{created}"
        return filename + suffix

    def _download_artifact_entries(self, entries, kind, *, include_versions=False):
        label, suffix, _stem = self._artifact_details(kind)
        del include_versions
        if len(entries) != 1:
            raise ValueError(
                "Multiple analyzed files must be downloaded through the server export."
            )
        run, copy = entries[0]
        save_path, _ = QFileDialog.getSaveFileName(
            self,
            f"Download {label}",
            self._copy_filename(run, copy, kind),
            f"{label} (*{suffix})",
        )
        if not save_path:
            return
        output_path = Path(save_path).with_suffix(suffix)
        self.api.download_run_artifact(copy["artifact"], output_path)
        QMessageBox.information(self, "Download Complete", f"Saved to:\n{output_path}")

    def _download_artifact_entries_safely(
        self, entries, kind, *, include_versions=False
    ):
        try:
            self._download_artifact_entries(
                entries, kind, include_versions=include_versions
            )
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Download Failed", str(exc))

    def download_run_artifacts(self, run, kind):
        try:
            copies = self._analysis_copies(run, kind)
            if not copies:
                raise ValueError("The requested cloud file is no longer available.")
            if len(copies) > 1:
                self._show_analysis_copies(run, kind, copies)
            else:
                self._download_artifact_entries([(run, copies[0])], kind)
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Download Failed", str(exc))

    def download_artifacts(self, kind):
        runs = self._selected_runs()
        if not runs:
            QMessageBox.information(
                self, "Nothing Selected", "Select at least one participant run."
            )
            return
        try:
            missing = [run for run in runs if cloud_file_count(run, kind) < 1]
            if missing:
                names = ", ".join(str(run["participant_number"]) for run in missing)
                QMessageBox.warning(
                    self,
                    "Analysis Export Unavailable",
                    f"No {self._artifact_details(kind)[0]} is stored for "
                    f"participant(s): {names}.",
                )
                return

            policy = "latest"
            duplicates = [
                (run, cloud_file_count(run, kind))
                for run in runs
                if cloud_file_count(run, kind) > 1
            ]
            if duplicates:
                policy = self._choose_bulk_duplicate_download(duplicates, kind)
                if not policy:
                    return
            self._download_bulk_export(
                runs, [kind], analysis_policy=policy
            )
        except (APIError, OSError, ValueError) as exc:
            QMessageBox.critical(self, "Download Failed", str(exc))

    def _choose_bulk_duplicate_download(self, duplicates, kind):
        label, _suffix, _stem = self._artifact_details(kind)
        dialog = QDialog(self)
        dialog.setWindowTitle("How to manage duplicates?")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        heading = QLabel(f"Multiple {label} copies are stored in the cloud.")
        heading.setStyleSheet("font-size: 15px; font-weight: 700;")
        layout.addWidget(heading)
        layout.addWidget(QLabel(
            "Choose whether the server should include the latest copy or every copy."
        ))
        for run, count in duplicates:
            item = QLabel(
                f"Participant {run['participant_number']} — {count} copies"
            )
            item.setStyleSheet(
                "background: #f2f6f9; padding: 8px; border-radius: 6px;"
            )
            layout.addWidget(item)
        buttons = QHBoxLayout()
        buttons.addStretch()
        latest = QPushButton("Download latest")
        all_copies = QPushButton("Download all")
        cancel = QPushButton("Cancel")
        for button in (latest, all_copies, cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        dialog.choice = None
        latest.clicked.connect(
            lambda: (setattr(dialog, "choice", "latest"), dialog.accept())
        )
        all_copies.clicked.connect(
            lambda: (setattr(dialog, "choice", "all"), dialog.accept())
        )
        cancel.clicked.connect(dialog.reject)
        dialog.exec_()
        return dialog.choice

    def _show_analysis_copies(self, run, kind, copies):
        label, _suffix, _stem = self._artifact_details(kind)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{label} copies")
        dialog.setMinimumWidth(680)
        layout = QVBoxLayout(dialog)
        heading = QLabel(
            f"Participant {run['participant_number']} has {len(copies)} {label} copies."
        )
        heading.setStyleSheet("font-size: 15px; font-weight: 700;")
        layout.addWidget(heading)
        layout.addWidget(QLabel("Download a specific copy, or choose which saved analysis to edit next."))

        for copy in copies:
            copy_row = QFrame()
            copy_row.setStyleSheet(
                "QFrame { background: #f3f6f9; border: 1px solid #d9e2ea; border-radius: 7px; }"
            )
            copy_layout = QHBoxLayout(copy_row)
            revision = copy.get("revision")
            description = f"Created {format_run_datetime(copy.get('created_at'))}"
            if revision is not None:
                description = f"Revision {revision} · {description}"
            if copy.get("is_current_editable"):
                description += " · Current editing version"
            copy_layout.addWidget(QLabel(description), 1)
            download = QPushButton("Download")
            download.clicked.connect(
                lambda _checked=False, item=copy:
                self._download_artifact_entries_safely([(run, item)], kind)
            )
            copy_layout.addWidget(download)
            use_current = QPushButton("Use for editing")
            use_current.setEnabled(not copy.get("legacy") and not copy.get("is_current_editable"))
            use_current.clicked.connect(
                lambda _checked=False, item=copy:
                self._set_copy_editable(dialog, run, item)
            )
            copy_layout.addWidget(use_current)
            delete = QPushButton("Delete")
            delete.setEnabled(not copy.get("legacy"))
            delete.clicked.connect(
                lambda _checked=False, item=copy:
                self._delete_analysis_copy(dialog, run, item)
            )
            copy_layout.addWidget(delete)
            layout.addWidget(copy_row)

        footer = QHBoxLayout()
        footer.addStretch()
        latest = QPushButton("Download latest")
        latest.clicked.connect(
            lambda: self._download_artifact_entries_safely([(run, copies[0])], kind)
        )
        all_copies = QPushButton("Download all")
        all_copies.clicked.connect(
            lambda: self._download_bulk_export(
                [run], [kind], analysis_policy="all"
            )
        )
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        for button in (latest, all_copies, close):
            footer.addWidget(button)
        layout.addLayout(footer)
        dialog.exec_()

    def _set_copy_editable(self, dialog, run, copy):
        try:
            self.api.set_run_analysis_copy_editable(run["id"], copy["id"])
            dialog.accept()
            self.refresh_runs()
            QMessageBox.information(
                self,
                "Editing Version Updated",
                "This analysis will be restored the next time the participant is opened in Analyzer.",
            )
        except APIError as exc:
            QMessageBox.critical(self, "Update Failed", str(exc))

    def _delete_analysis_copy(self, dialog, run, copy):
        answer = QMessageBox.warning(
            self,
            "Delete Analyzed Copy",
            "Permanently delete this analyzed CSV, trainable JSON, and its saved edit state?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.api.delete_run_analysis_copy(run["id"], copy["id"])
            dialog.accept()
            self.refresh_runs()
        except APIError as exc:
            QMessageBox.critical(self, "Delete Failed", str(exc))

    def analyze_selected(self):
        runs = self._selected_runs()
        if not runs:
            return
        self._analyze_runs(runs)

    def analyze_run(self, run):
        """Launch Analyzer with exactly one participant run."""
        self._analyze_runs([run])

    def _analyze_runs(self, runs):
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
            for run_summary in runs:
                run = self._get_run_detail(run_summary)
                context_run = {
                    "id": run["id"],
                    "session_id": run["session_id"],
                    "analysis_copy_count": analysis_copy_count(run),
                    "analysis_etag": None,
                    "analysis_revision": 0,
                    "results": [],
                }
                try:
                    saved_state = self.api.get_run_analysis_state(run["id"])
                except APIError as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    state_path = workspace / f"{run['id']}_analysis_state.json"
                    state_path.write_text(
                        json.dumps(saved_state["state"], ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    context_run["analysis_state_path"] = str(state_path)
                    context_run["analysis_etag"] = saved_state.get("etag")
                    context_run["analysis_revision"] = saved_state.get("revision", 0)
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
                    context_run["results"].append(
                        {
                            "id": result["id"],
                            "sha256": result["sha256"],
                            "block_id": result.get("block_id"),
                            "block_index": result.get("block_index"),
                            "block_name": result.get("block_name"),
                            "path": str(destination),
                        }
                    )
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
