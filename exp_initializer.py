import sys
import os
import json
import zipfile
import tempfile
import shutil
import numpy as np
import math
import traceback
import uuid
from pathlib import Path
from app_paths import ensure_dir, user_data_dir
from project_version import APP_VERSION
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QListWidget, QListWidgetItem, 
                             QTextEdit, QMessageBox, QGroupBox, QSplitter, 
                             QSpinBox, QComboBox, QRadioButton, QCheckBox, 
                             QLineEdit, QFormLayout, QDialog, QScrollArea,
                             QApplication, QSlider, QInputDialog, QTreeWidget, QTreeWidgetItem, QAbstractItemView)
from PyQt5.QtCore import Qt, QUrl, QPointF, QRectF, QEvent
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from pydub import AudioSegment

# Import AudioProcessor
try:
    from audio_processor import AudioProcessor
except ImportError:
    # Handle case where audio_processor might be missing or has issues
    AudioProcessor = None


def canonicalize_audio_path(file_path):
    return os.path.normcase(os.path.abspath(str(file_path)))


def create_segment_id(file_path):
    return f"{Path(file_path).stem}_{uuid.uuid4().hex}"


def normalize_segments(file_path, segments):
    normalized_segments = []
    sorted_segments = sorted(
        (dict(segment) for segment in segments),
        key=lambda segment: (
            segment.get('start', 0),
            segment.get('end', 0),
            segment.get('segment_id', ''),
        ),
    )

    for position, segment in enumerate(sorted_segments):
        start = max(0, int(round(segment.get('start', 0))))
        end = max(start + 1, int(round(segment.get('end', start))))
        normalized_segment = dict(segment)
        normalized_segment['start'] = start
        normalized_segment['end'] = end
        normalized_segment['duration'] = end - start
        normalized_segment['index'] = position + 1
        normalized_segment['segment_id'] = normalized_segment.get('segment_id') or create_segment_id(file_path)
        normalized_segments.append(normalized_segment)

    return normalized_segments


def reconcile_segment_ids(file_path, previous_segments, new_segments):
    prior_segments = normalize_segments(file_path, previous_segments)
    updated_segments = [dict(segment) for segment in new_segments]
    candidates = []

    for new_index, new_segment in enumerate(updated_segments):
        if new_segment.get('segment_id'):
            continue

        new_start = int(round(new_segment.get('start', 0)))
        new_end = max(new_start + 1, int(round(new_segment.get('end', new_start))))

        for prior_index, prior_segment in enumerate(prior_segments):
            overlap = max(0, min(prior_segment['end'], new_end) - max(prior_segment['start'], new_start))
            boundary_gap = abs(prior_segment['start'] - new_start) + abs(prior_segment['end'] - new_end)
            if overlap > 0 or boundary_gap <= 200:
                candidates.append((-overlap, boundary_gap, prior_index, new_index))

    candidates.sort()
    matched_prior = set()
    matched_new = set()

    for _, _, prior_index, new_index in candidates:
        if prior_index in matched_prior or new_index in matched_new:
            continue
        updated_segments[new_index]['segment_id'] = prior_segments[prior_index]['segment_id']
        matched_prior.add(prior_index)
        matched_new.add(new_index)

    return normalize_segments(file_path, updated_segments)


class WaveformWidget(QWidget):
    """Widget for displaying and editing audio waveform with multiple word segments"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        # ... (rest of WaveformWidget unchanged)
        self.audio_data = None
        self.sample_rate = 44100
        self.duration_ms = 0
        self.segments = []
        self.selected_segment_index = -1
        self.zoom_level = 1.0
        self.scroll_offset = 0
        self.dragging = None
        self.drag_segment_index = -1
        self.wave_color = QColor(24, 119, 242)
        self.bg_color = QColor(255, 255, 255)
        self.segment_base_color = QColor(76, 175, 80, 50)     
        self.segment_selected_color = QColor(76, 175, 80, 100) 
        self.marker_color = QColor(50, 50, 50)
        self.setMouseTracking(True)

    def load_file(self, file_path, segments=None, processor=None):
        temp_wav_path = None

        try:
            source_path = file_path
            if processor is not None:
                temp_dir = ensure_dir(user_data_dir() / 'temp')
                temp_wav_path = str(temp_dir / f"waveform_preview_{uuid.uuid4().hex}.wav")
                processor.convert_to_wav(file_path, temp_wav_path)
                source_path = temp_wav_path

            audio = AudioSegment.from_wav(source_path) if processor is not None else AudioSegment.from_file(source_path)
            audio = audio.set_channels(1)
            self.sample_rate = audio.frame_rate
            self.duration_ms = len(audio)
            samples = np.array(audio.get_array_of_samples())
            target_points = 20000
            if len(samples) > target_points:
                step = len(samples) // target_points
                self.audio_data = samples[::step]
            else:
                self.audio_data = samples
            if segments is not None:
                self.segments = segments
            else:
                self.segments = []
            self.update()
        except Exception as e:
            self.audio_data = None
            self.duration_ms = 0
            if processor is not None:
                processor.log(f"[WAVEFORM] Error loading waveform for {file_path}: {e}")
            print(f"Error loading waveform: {e}")
            self.update()
        finally:
            if temp_wav_path and os.path.exists(temp_wav_path):
                try:
                    os.remove(temp_wav_path)
                except OSError:
                    pass

    def zoom_to_fit(self):
        self.zoom_level = 1.0
        self.scroll_offset = 0
        self.update()

    def ms_to_x(self, ms):
        width = self.width()
        total_visible_ms = self.duration_ms / self.zoom_level
        start_visible_ms = self.scroll_offset * self.duration_ms
        rel_ms = ms - start_visible_ms
        return (rel_ms / total_visible_ms) * width

    def x_to_ms(self, x):
        width = self.width()
        total_visible_ms = self.duration_ms / self.zoom_level
        start_visible_ms = self.scroll_offset * self.duration_ms
        rel_ms = (x / width) * total_visible_ms
        return start_visible_ms + rel_ms

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), self.bg_color)
        if self.audio_data is None:
            painter.drawText(self.rect(), Qt.AlignCenter, "No Audio Data")
            return
        width = self.width()
        height = self.height()
        mid_y = height / 2
        painter.setPen(QPen(self.wave_color, 1))
        total_samples = len(self.audio_data)
        visible_samples = int(total_samples / self.zoom_level)
        start_sample = int(self.scroll_offset * total_samples)
        end_sample = min(total_samples, start_sample + visible_samples)
        if visible_samples <= 0: return
        subset = self.audio_data[start_sample:end_sample]
        if len(subset) == 0: return
        max_amp = np.max(np.abs(self.audio_data)) if len(self.audio_data) > 0 else 1
        if max_amp == 0: max_amp = 1
        points = []
        step = max(1, len(subset) // width)
        for i in range(0, len(subset), step):
            x = (i / len(subset)) * width
            amp = subset[i] / max_amp
            y = mid_y - (amp * (height / 2) * 0.9)
            points.append(QPointF(x, y))
        if points:
            painter.drawPolyline(QPolygonF(points))
        for i, seg in enumerate(self.segments):
            start_x = self.ms_to_x(seg['start'])
            end_x = self.ms_to_x(seg['end'])
            if end_x < 0 or start_x > width: continue
            is_selected = (i == self.selected_segment_index)
            color = self.segment_selected_color if is_selected else self.segment_base_color
            rect_width = max(1, end_x - start_x)
            region_rect = QRectF(start_x, 0, rect_width, height)
            painter.fillRect(region_rect, color)
            painter.setPen(QPen(self.marker_color, 2 if is_selected else 1))
            painter.drawLine(int(start_x), 0, int(start_x), height)
            painter.drawLine(int(end_x), 0, int(end_x), height)
            painter.setPen(QPen(Qt.black, 1))
            label = f"#{i+1}"
            painter.drawText(int(start_x) + 5, 20, label)

    def mousePressEvent(self, event):
        x = event.x()
        ms = self.x_to_ms(x)
        closest_dist = float('inf')
        target_idx = -1
        drag_type = None
        threshold_px = 10
        for i, seg in enumerate(self.segments):
            start_x = self.ms_to_x(seg['start'])
            end_x = self.ms_to_x(seg['end'])
            dist_start = abs(x - start_x)
            dist_end = abs(x - end_x)
            if dist_start < threshold_px and dist_start < closest_dist:
                closest_dist = dist_start
                target_idx = i
                drag_type = 'start'
            if dist_end < threshold_px and dist_end < closest_dist:
                closest_dist = dist_end
                target_idx = i
                drag_type = 'end'
        if target_idx != -1:
            self.dragging = drag_type
            self.drag_segment_index = target_idx
            self.selected_segment_index = target_idx
            self.update()
            return
        for i, seg in enumerate(self.segments):
            if seg['start'] <= ms <= seg['end']:
                self.selected_segment_index = i
                self.update()
                return
        self.selected_segment_index = -1
        self.update()

    def mouseMoveEvent(self, event):
        x = event.x()
        ms = self.x_to_ms(x)
        cursor = Qt.ArrowCursor
        for seg in self.segments:
            start_x = self.ms_to_x(seg['start'])
            end_x = self.ms_to_x(seg['end'])
            if abs(x - start_x) < 10 or abs(x - end_x) < 10:
                cursor = Qt.SizeHorCursor
                break
        self.setCursor(cursor)
        if self.dragging and self.drag_segment_index != -1:
            idx = self.drag_segment_index
            seg = self.segments[idx]
            if self.dragging == 'start':
                new_start = max(0, min(ms, seg['end'] - 50)) 
                seg['start'] = new_start
                seg['duration'] = seg['end'] - seg['start']
            elif self.dragging == 'end':
                new_end = min(self.duration_ms, max(ms, seg['start'] + 50))
                seg['end'] = new_end
                seg['duration'] = seg['end'] - seg['start']
            self.update()

    def mouseReleaseEvent(self, event):
        self.dragging = None
        self.drag_segment_index = -1

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        zoom_factor = 1.1 if delta > 0 else 0.9
        new_zoom = self.zoom_level * zoom_factor
        new_zoom = max(1.0, min(new_zoom, 50.0))
        center_x = event.x()
        center_ms = self.x_to_ms(center_x)
        self.zoom_level = new_zoom
        total_visible_ms = self.duration_ms / self.zoom_level
        new_start_ms = center_ms - (center_x / self.width()) * total_visible_ms
        self.scroll_offset = max(0, min(1.0, new_start_ms / self.duration_ms))
        self.update()


class FileEditorWindow(QDialog):
    """Advanced File Review Window."""
    def __init__(self, file_path, segments, processor, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"File Editor: {Path(file_path).name}")
        self.resize(1000, 700)
        self.file_path = file_path
        self.segments = normalize_segments(file_path, segments)
        self._saved_segments = None
        self.processor = processor
        self.media_player = QMediaPlayer()
        self.init_ui()
        self.waveform.load_file(file_path, self.segments, self.processor)
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Noise Sensitivity:"))
        self.slider_sens = QSlider(Qt.Horizontal)
        self.slider_sens.setRange(-60, 0)
        self.slider_sens.setValue(-30)
        self.slider_sens.setTickPosition(QSlider.TicksBelow)
        self.slider_sens.setTickInterval(5)
        self.slider_sens.valueChanged.connect(self.update_sens_label)
        top_bar.addWidget(self.slider_sens)
        self.lbl_sens = QLabel("-30 dB")
        top_bar.addWidget(self.lbl_sens)
        btn_reslice = QPushButton("Re-Slice File")
        btn_reslice.clicked.connect(self.reslice_file)
        top_bar.addWidget(btn_reslice)
        layout.addLayout(top_bar)
        self.waveform = WaveformWidget()
        layout.addWidget(self.waveform, 1)
        controls = QHBoxLayout()
        btn_play_sel = QPushButton("▶ Play Selected")
        btn_play_sel.clicked.connect(self.play_selected)
        controls.addWidget(btn_play_sel)
        btn_play_all = QPushButton("▶ Play All")
        btn_play_all.clicked.connect(self.play_all)
        controls.addWidget(btn_play_all)
        controls.addStretch()
        btn_add = QPushButton("➕ Add Word Segment")
        btn_add.clicked.connect(self.add_segment)
        controls.addWidget(btn_add)
        btn_del = QPushButton("🗑 Delete Selected")
        btn_del.clicked.connect(self.delete_segment)
        controls.addWidget(btn_del)
        layout.addLayout(controls)
        self.seg_list = QListWidget()
        self.seg_list.setFixedHeight(150)
        self.seg_list.itemClicked.connect(self.on_list_select)
        layout.addWidget(self.seg_list)
        footer = QHBoxLayout()
        btn_save = QPushButton("Save & Close")
        btn_save.setProperty("class", "primary")
        btn_save.clicked.connect(self.save_and_close)
        footer.addStretch()
        footer.addWidget(btn_save)
        layout.addLayout(footer)
        self.refresh_list()

    def update_sens_label(self, val):
        self.lbl_sens.setText(f"{val} dB")

    def reslice_file(self):
        if not self.processor:
            return
        thresh = self.slider_sens.value()
        reply = QMessageBox.question(self, "Confirm Re-Slice", 
                                     "This will overwrite all current segments with new detection settings.\nContinue?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            selected_segment_id = None
            if 0 <= self.waveform.selected_segment_index < len(self.segments):
                selected_segment_id = self.segments[self.waveform.selected_segment_index].get('segment_id')
            new_segs, _ = self.processor.detect_segments(
                self.file_path, 
                silence_thresh=thresh, 
                min_silence_len=200
            )
            self.segments = reconcile_segment_ids(self.file_path, self.segments, new_segs)
            self._sync_segments(selected_segment_id)

    def refresh_list(self):
        self.seg_list.clear()
        for i, seg in enumerate(self.segments):
            dur = seg.get('duration', seg['end'] - seg['start'])
            item = QListWidgetItem(f"Word {i+1}: {int(seg['start'])}ms - {int(seg['end'])}ms ({int(dur)}ms)")
            self.seg_list.addItem(item)

    def on_list_select(self, item):
        idx = self.seg_list.row(item)
        self.waveform.selected_segment_index = idx
        self.waveform.update()

    def play_selected(self):
        idx = self.waveform.selected_segment_index
        if idx < 0 or idx >= len(self.segments): return
        seg = self.segments[idx]
        self._play_range(seg['start'], seg['end'], context="editor_sel")

    def play_all(self):
        self._play_range(0, self.waveform.duration_ms, context="editor_all")

    def _play_range(self, start, end, context):
        if not self.processor:
            return
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        temp_file = self.processor.get_temp_segment_file(self.file_path, start, end, context=context)
        if temp_file:
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(temp_file))))
            self.media_player.play()

    def add_segment(self):
        start = int(self.waveform.x_to_ms(10))
        end = start + 500
        new_seg = {
            'start': start,
            'end': end,
            'duration': 500,
            'index': len(self.segments) + 1,
            'segment_id': create_segment_id(self.file_path),
        }
        self.segments.append(new_seg)
        self._sync_segments(new_seg['segment_id'])

    def delete_segment(self):
        idx = self.waveform.selected_segment_index
        if 0 <= idx < len(self.segments):
            del self.segments[idx]
            next_segment_id = None
            if self.segments:
                next_index = min(idx, len(self.segments) - 1)
                next_segment_id = self.segments[next_index].get('segment_id')
            self._sync_segments(next_segment_id)

    def get_segments(self):
        if self._saved_segments is not None:
            return [dict(segment) for segment in self._saved_segments]
        return [dict(segment) for segment in self.segments]

    def _sync_segments(self, selected_segment_id=None):
        self.segments = normalize_segments(self.file_path, self.segments)
        self.waveform.segments = self.segments

        selected_index = -1
        if selected_segment_id:
            for index, segment in enumerate(self.segments):
                if segment.get('segment_id') == selected_segment_id:
                    selected_index = index
                    break

        self.waveform.selected_segment_index = selected_index
        self.refresh_list()
        if 0 <= selected_index < self.seg_list.count():
            self.seg_list.setCurrentRow(selected_index)
        self.waveform.update()

    def select_segment_by_id(self, segment_id):
        for index, segment in enumerate(self.segments):
            if segment.get('segment_id') == segment_id:
                self.waveform.selected_segment_index = index
                self.seg_list.setCurrentRow(index)
                self.waveform.update()
                return

    def save_and_close(self):
        # Capture a deep snapshot of the live segment data BEFORE any
        # Qt teardown. self.segments is the same list the waveform edits
        # in-place during drag, so we read the edits directly.
        self._saved_segments = normalize_segments(
            self.file_path, [dict(seg) for seg in self.segments])
        super().accept()


class GroupTreeWidget(QTreeWidget):
    """Custom TreeWidget to handle drag-and-drop of words between groups."""
    def __init__(self, parent_wizard):
        super().__init__()
        self.wizard = parent_wizard
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setIndentation(20)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.wizard.sync_from_tree()

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)
        
        # Enforce Constraints
        target = self.itemAt(event.pos())
        drop_pos = self.dropIndicatorPosition()
        
        sel = self.selectedItems()
        if not sel: return
        
        dragged_item = sel[0]
        dragged_role = dragged_item.data(0, Qt.UserRole)
        
        if dragged_role == "group":
            # Groups cannot be dropped ON anything (nesting)
            if drop_pos == QAbstractItemView.OnItem:
                event.ignore()
                return 
            # Groups can only be at root level (parent is None)
            if target and target.parent():
                 event.ignore()
                 return

        elif dragged_role == "word":
            # Words cannot be dropped on Root (OnViewport)
            if drop_pos == QAbstractItemView.OnViewport:
                event.ignore()
                return

            if target:
                target_role = target.data(0, Qt.UserRole)
                # Word on Word -> Prevent nesting (Qt default)
                if drop_pos == QAbstractItemView.OnItem and target_role == "word":
                    event.ignore()
                    return
                # Word Above/Below Group -> Bad (becomes Sibling of Group = Root)
                if drop_pos != QAbstractItemView.OnItem and target_role == "group":
                    event.ignore()
                    return


class NewExperimentWizard(QWidget):
    """Refactored Experiment Wizard: Group -> Files -> Words with Sidebar Tree"""
    
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.processor = AudioProcessor(verbose=True) if AudioProcessor else None
        self._active_word_item = None
        self._active_word_text_input = None
        self.loaded_properties = None
        self.imported_package_dir = None
        
        # New Data Structure:
        # { 
        #   "Group 1": { 
        #      "files": [ {path, parsed_segments: []} ], 
        #      "words": [ {id, text, file_path, start, end} ] 
        #   } 
        # }
        self.groups = {"Group 1": {"files": [], "words": []}}
        
        self.media_player = QMediaPlayer()
        
        self.init_ui()

    def _has_setup_data(self):
        if self.loaded_properties:
            return True

        if list(self.groups.keys()) != ["Group 1"]:
            return True

        group = self.groups.get("Group 1", {})
        return bool(group.get("files") or group.get("words"))

    def import_experiment_zip(self, file_path=None):
        if file_path is None:
            file_path, _ = QFileDialog.getOpenFileName(self, "Upload Experiment Package", "", "ZIP Files (*.zip)")

        if not file_path:
            return False

        if self._has_setup_data():
            reply = QMessageBox.question(
                self,
                "Replace Current Draft",
                "Loading an experiment package will replace the current experiment draft in the editor. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return False

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.parent.status_msg.setText("Importing experiment package...")  # type: ignore

        try:
            imported_groups, loaded_properties, extract_dir = self._load_experiment_package(file_path)
        except Exception as exc:
            QMessageBox.critical(self, "Import Failed", f"Could not import experiment package:\n{exc}")
            return False
        finally:
            QApplication.restoreOverrideCursor()
            self.parent.status_msg.setText("Ready")  # type: ignore

        self.groups = imported_groups
        self.loaded_properties = loaded_properties
        self.imported_package_dir = extract_dir
        self.populate_tree()
        self.clear_right_panel()

        first_group = next(iter(self.groups), None)
        if first_group:
            self._select_group_item(first_group)

        self.activateWindow()
        self.setFocus()
        return True

    def _extract_experiment_package(self, file_path):
        import_root = ensure_dir(user_data_dir() / "editable_experiments")
        safe_stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in Path(file_path).stem) or "experiment"
        extract_dir = ensure_dir(import_root / f"{safe_stem}_{uuid.uuid4().hex[:8]}")

        with zipfile.ZipFile(file_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)

        json_files = sorted(extract_dir.glob("*.json"))
        if not json_files:
            json_files = sorted(extract_dir.rglob("*.json"))
        if not json_files:
            raise FileNotFoundError("No experiment configuration JSON found inside the ZIP package.")

        return extract_dir, json_files[0]

    def _resolve_imported_media_path(self, package_dir, file_lookup, source_ref):
        if not source_ref:
            raise ValueError("Experiment package contains a word without a source audio reference.")

        entry = file_lookup.get(source_ref)
        if entry:
            return entry['path'], entry

        candidate = (package_dir / str(source_ref)).resolve()
        if candidate.exists():
            return str(candidate), {
                'path': str(candidate),
                'original_name': candidate.name,
                'owner_group': None,
            }

        media_candidate = (package_dir / "media" / str(source_ref)).resolve()
        if media_candidate.exists():
            return str(media_candidate), {
                'path': str(media_candidate),
                'original_name': media_candidate.name,
                'owner_group': None,
            }

        raise FileNotFoundError(f"Missing source audio '{source_ref}' in the experiment package.")

    def _load_experiment_package(self, file_path):
        extract_dir, config_path = self._extract_experiment_package(file_path)

        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        config_groups = config.get('groups')
        if not isinstance(config_groups, list):
            raise ValueError("This experiment package format cannot be edited by the current setup tool.")

        groups = {}
        for index, group_cfg in enumerate(config_groups, start=1):
            group_name = str(group_cfg.get('name') or f"Group {index}")
            if group_name in groups:
                suffix = 2
                unique_name = f"{group_name} ({suffix})"
                while unique_name in groups:
                    suffix += 1
                    unique_name = f"{group_name} ({suffix})"
                group_name = unique_name
            groups[group_name] = {"files": [], "words": []}

        file_lookup = {}
        for file_cfg in config.get('files', []):
            file_name = file_cfg.get('file_name') or Path(file_cfg.get('path', '')).name
            if not file_name:
                continue

            rel_path = file_cfg.get('path') or f"media/{file_name}"
            abs_path = (extract_dir / rel_path).resolve()
            if not abs_path.exists():
                fallback = (extract_dir / "media" / file_name).resolve()
                if fallback.exists():
                    abs_path = fallback

            entry = {
                'path': str(abs_path),
                'original_name': file_cfg.get('original_name') or file_name,
                'owner_group': file_cfg.get('owner_group'),
                'auto_slice_word': file_cfg.get('auto_slice_word', True),
            }
            file_lookup[file_name] = entry
            file_lookup[rel_path] = entry

        file_segments = {}
        file_first_group = {}
        staged_words = []

        for group_cfg in config_groups:
            group_name = str(group_cfg.get('name') or '')
            if group_name not in groups:
                continue

            for word_cfg in group_cfg.get('words', []) or []:
                source_ref = word_cfg.get('source_file') or word_cfg.get('file') or word_cfg.get('source')
                resolved_path, source_meta = self._resolve_imported_media_path(extract_dir, file_lookup, source_ref)

                start_ms = int(round(word_cfg.get('start_ms', word_cfg.get('start', 0))))
                end_raw = word_cfg.get('end_ms', word_cfg.get('end', start_ms + 1))
                end_ms = max(start_ms + 1, int(round(end_raw)))
                segment_id = word_cfg.get('id') or word_cfg.get('segment_id') or create_segment_id(resolved_path)

                file_segments.setdefault(resolved_path, []).append({
                    'start': start_ms,
                    'end': end_ms,
                    'segment_id': segment_id,
                })
                file_first_group.setdefault(resolved_path, group_name)

                staged_words.append({
                    'group_name': group_name,
                    'text': word_cfg.get('text', ''),
                    'segment_id': segment_id,
                    'source_path': resolved_path,
                    'source_meta': source_meta,
                })

        normalized_segments_by_path = {
            path: normalize_segments(path, segments)
            for path, segments in file_segments.items()
        }
        segment_lookup = {
            path: {segment['segment_id']: segment for segment in segments}
            for path, segments in normalized_segments_by_path.items()
        }

        for source_path, segments in normalized_segments_by_path.items():
            source_meta = next(
                (word['source_meta'] for word in staged_words if word['source_path'] == source_path),
                None,
            ) or {}
            owner_group = source_meta.get('owner_group')
            if owner_group not in groups:
                owner_group = file_first_group.get(source_path)
            if owner_group not in groups:
                owner_group = next(iter(groups), "Group 1")
                groups.setdefault(owner_group, {"files": [], "words": []})

            groups[owner_group]['files'].append({
                'path': source_path,
                'original_name': source_meta.get('original_name') or Path(source_path).name,
                'segments': segments,
                'duration': None,
                'auto_slice_word': source_meta.get('auto_slice_word', True),
            })

        for staged_word in staged_words:
            group_name = staged_word['group_name']
            segments_for_file = segment_lookup.get(staged_word['source_path'], {})
            segment = segments_for_file.get(staged_word['segment_id'])
            if not segment:
                continue

            groups[group_name]['words'].append({
                'id': staged_word['segment_id'],
                'segment_id': staged_word['segment_id'],
                'text': staged_word['text'],
                'file_path': staged_word['source_path'],
                'start': segment['start'],
                'end': segment['end'],
                'seg_index': segment['index'],
            })

        loaded_properties = {
            'name': config.get('name', Path(file_path).stem),
            'grid': config.get('grid', {}),
            'order': config.get('order', 'random'),
            'sequence': list(config.get('sequence', []) or []),
            'repetitions': dict(config.get('repetitions', {}) or {}),
            'active_block_sequence': list(config.get('active_block_sequence', []) or []),
            'proceed_condition': dict(config.get('proceed_condition', {}) or {}),
            'beeps': dict(config.get('beeps', {}) or {}),
        }

        return groups, loaded_properties, str(extract_dir)
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QHBoxLayout()
        btn_back = QPushButton("← Back")
        btn_back.clicked.connect(self.parent.show_main_menu)  # type: ignore
        header.addWidget(btn_back)
        header.addStretch()
        title = QLabel("Experiment Setup")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)
        
        splitter = QSplitter(Qt.Horizontal)
        
        # LEFT: Sidebar (Tree + Controls)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # Structure Controls
        t_controls = QHBoxLayout()
        btn_add_grp = QPushButton("Add Group")
        btn_add_grp.clicked.connect(self.add_group)
        t_controls.addWidget(btn_add_grp)
        
        btn_del_grp = QPushButton("Delete Group")
        btn_del_grp.clicked.connect(self.delete_current_group)
        t_controls.addWidget(btn_del_grp)
        
        left_layout.addLayout(t_controls)
        
        # Tree
        self.tree = GroupTreeWidget(self)
        self.tree.itemSelectionChanged.connect(self.on_selection_changed)
        self.tree.itemClicked.connect(self.on_tree_item_clicked)
        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        left_layout.addWidget(self.tree)
        
        splitter.addWidget(left_panel)
        
        # RIGHT: Details & Actions
        self.right_panel = QWidget()
        self.right_layout = QVBoxLayout(self.right_panel)
        self.right_layout.setAlignment(Qt.AlignTop)
        
        # Placeholder content (will be cleared/swapped)
        self.right_layout.addWidget(QLabel("Select a Group or Word to edit details."))
        
        splitter.addWidget(self.right_panel)
        splitter.setSizes([250, 750])
        layout.addWidget(splitter)
        
        # Footer
        footer = QHBoxLayout()
        self.btn_next = QPushButton("Next: Properties →")
        self.btn_next.setProperty("class", "primary")
        self.btn_next.setFixedSize(200, 50)
        self.btn_next.clicked.connect(self.go_next)
        footer.addStretch()
        footer.addWidget(self.btn_next)
        footer.addStretch()
        layout.addLayout(footer)
        
        # Initial Population
        self.populate_tree()

    def populate_tree(self):
        """Rebuilds tree from self.groups"""
        self.tree.clear()
        
        for grp_name, grp_data in self.groups.items():
            grp_item = QTreeWidgetItem(self.tree)
            grp_item.setText(0, grp_name)
            # Allow drop on groups
            grp_item.setFlags(grp_item.flags() | Qt.ItemIsDropEnabled | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            # Tag as group
            grp_item.setData(0, Qt.UserRole, "group")
            
            for word in grp_data["words"]:
                word_item = QTreeWidgetItem(grp_item)
                label = self._format_word_label(word)
                word_item.setText(0, label)
                word_item.setFlags(word_item.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                # Tag as word
                word_item.setData(0, Qt.UserRole, "word")
                # Store dict reference
                word_item.setData(0, Qt.UserRole + 1, word)
                
            grp_item.setExpanded(True)

    def _format_word_label(self, word_data):
        text = word_data.get('text', '') or '[No Text]'
        return f"{text} ({Path(word_data['file_path']).name} #{word_data['seg_index']})"

    def _create_word_entry(self, file_path, segment, text=''):
        segment_id = segment['segment_id']
        return {
            'id': segment_id,
            'segment_id': segment_id,
            'text': text,
            'file_path': file_path,
            'start': segment['start'],
            'end': segment['end'],
            'seg_index': segment['index'],
        }

    def _show_details_for_item(self, item):
        self._commit_active_word_text()

        if not item:
            self.clear_right_panel()
            return

        role = item.data(0, Qt.UserRole)
        if role == "group":
            self.show_group_details(item.text(0))
            return

        if role == "word":
            word_data = item.data(0, Qt.UserRole + 1)
            if not word_data:
                self.clear_right_panel()
                return
            self.show_word_details(word_data, item)

            inputs = self.right_panel.findChildren(QLineEdit)
            if inputs:
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(0, inputs[0].setFocus)

    def _find_file_entry(self, file_path):
        file_key = canonicalize_audio_path(file_path)
        for group_name, group_data in self.groups.items():
            for file_entry in group_data['files']:
                if canonicalize_audio_path(file_entry['path']) == file_key:
                    return group_name, file_entry
        return None, None

    def _select_group_item(self, group_name):
        root = self.tree.invisibleRootItem()
        for index in range(root.childCount()):
            group_item = root.child(index)
            if group_item and group_item.text(0) == group_name:
                self.tree.setCurrentItem(group_item)
                return True
        return False

    def _select_word_item(self, segment_id):
        root = self.tree.invisibleRootItem()
        for group_index in range(root.childCount()):
            group_item = root.child(group_index)
            if not group_item:
                continue
            for word_index in range(group_item.childCount()):
                word_item = group_item.child(word_index)
                if not word_item:
                    continue
                word_data = word_item.data(0, Qt.UserRole + 1)
                if word_data and word_data.get('segment_id') == segment_id:
                    self.tree.setCurrentItem(word_item)
                    return True
        return False

    def _format_review_file_label(self, file_entry):
        word_count = len(file_entry.get('segments', []))
        word_label = "word" if word_count == 1 else "words"
        mode_label = "auto-slice on" if file_entry.get('auto_slice_word', True) else "auto-slice off"
        return f"{file_entry['original_name']} ({word_count} {word_label}, {mode_label})"

    def _build_single_recording_segment(self, file_entry):
        duration = file_entry.get('duration')

        if (not duration or duration <= 0) and self.processor:
            try:
                _segments, duration = self.processor.detect_segments(file_entry['path'])
                file_entry['duration'] = duration
            except Exception:
                duration = None

        if not duration or duration <= 0:
            existing_segments = file_entry.get('segments', [])
            if existing_segments:
                duration = max(int(round(segment.get('end', 0))) for segment in existing_segments)

        end_ms = max(1, int(round(duration or 1)))
        return normalize_segments(file_entry['path'], [{'start': 0, 'end': end_ms}])

    def _replace_words_for_file(self, owner_group, file_entry, segments):
        normalized_segments = normalize_segments(file_entry['path'], segments)
        file_entry['segments'] = normalized_segments

        file_key = canonicalize_audio_path(file_entry['path'])
        owner_words = self.groups[owner_group]['words']
        insert_at = len(owner_words)
        for index, word in enumerate(owner_words):
            if canonicalize_audio_path(word['file_path']) == file_key:
                insert_at = index
                break

        for group_data in self.groups.values():
            group_data['words'] = [
                word for word in group_data['words']
                if canonicalize_audio_path(word['file_path']) != file_key
            ]

        new_words = [self._create_word_entry(file_entry['path'], segment) for segment in normalized_segments]
        self.groups[owner_group]['words'][insert_at:insert_at] = new_words
        return normalized_segments

    def _set_file_auto_slice(self, file_path, enabled):
        owner_group, file_entry = self._find_file_entry(file_path)
        if not owner_group or not file_entry:
            raise ValueError("Could not find recording entry.")

        file_entry['auto_slice_word'] = enabled

        if enabled:
            if not self.processor:
                raise RuntimeError("Audio processor is not available.")
            segments, duration = self.processor.detect_segments(file_entry['path'])
            file_entry['duration'] = duration
            updated_segments = normalize_segments(file_entry['path'], segments)
        else:
            updated_segments = self._build_single_recording_segment(file_entry)

        return file_entry, self._replace_words_for_file(owner_group, file_entry, updated_segments)

    def _apply_file_segment_changes(self, file_path, updated_segments):
        owner_group, file_entry = self._find_file_entry(file_path)
        if not owner_group or not file_entry:
            return None, []

        file_entry['segments'] = normalize_segments(file_entry['path'], file_entry.get('segments', []))
        previous_segments = [dict(segment) for segment in file_entry['segments']]
        merged_segments = reconcile_segment_ids(file_entry['path'], previous_segments, updated_segments)
        file_entry['segments'] = merged_segments

        previous_segment_ids = {segment['segment_id'] for segment in previous_segments}
        previous_ids_by_index = {segment['index']: segment['segment_id'] for segment in previous_segments}
        file_key = canonicalize_audio_path(file_entry['path'])
        existing_words = {}

        for group_data in self.groups.values():
            for word in group_data['words']:
                if canonicalize_audio_path(word['file_path']) != file_key:
                    continue
                if not word.get('segment_id'):
                    word['segment_id'] = previous_ids_by_index.get(word.get('seg_index')) or word.get('id') or create_segment_id(file_entry['path'])
                word['id'] = word.get('id') or word['segment_id']
                existing_words[word['segment_id']] = word

        active_segment_ids = {segment['segment_id'] for segment in merged_segments}

        for segment in merged_segments:
            existing_word = existing_words.get(segment['segment_id'])
            if not existing_word:
                continue
            existing_word['start'] = segment['start']
            existing_word['end'] = segment['end']
            existing_word['seg_index'] = segment['index']
            existing_word['file_path'] = file_entry['path']
            existing_word['segment_id'] = segment['segment_id']
            existing_word['id'] = existing_word.get('id') or segment['segment_id']

        for group_data in self.groups.values():
            group_data['words'] = [
                word for word in group_data['words']
                if canonicalize_audio_path(word['file_path']) != file_key
                or word.get('segment_id') in active_segment_ids
            ]

        owner_words = self.groups[owner_group]['words']
        insert_at = 0
        for index, word in enumerate(owner_words):
            if canonicalize_audio_path(word['file_path']) == file_key:
                insert_at = index + 1

        new_words = [
            self._create_word_entry(file_entry['path'], segment)
            for segment in merged_segments
            if segment['segment_id'] not in existing_words and segment['segment_id'] not in previous_segment_ids
        ]
        if new_words:
            owner_words[insert_at:insert_at] = new_words

        return owner_group, merged_segments

    def _open_recording_editor(self, file_path, selected_segment_id=None, fallback_group=None, dialog_parent=None, list_item=None):
        self._commit_active_word_text()

        if not self.processor:
            return

        owner_group, file_entry = self._find_file_entry(file_path)
        if not owner_group or not file_entry:
            QMessageBox.warning(self, "Missing Recording", "Could not find the source recording for this word.")
            return

        file_entry['segments'] = normalize_segments(file_entry['path'], file_entry.get('segments', []))
        editor = FileEditorWindow(file_entry['path'], file_entry['segments'], self.processor, dialog_parent or self)
        if selected_segment_id:
            editor.select_segment_by_id(selected_segment_id)

        if editor.exec_() != QDialog.Accepted:
            return

        owner_group, merged_segments = self._apply_file_segment_changes(file_entry['path'], editor.get_segments())
        if list_item is not None:
            list_item.setText(self._format_review_file_label(file_entry))

        self.populate_tree()
        if selected_segment_id and any(segment.get('segment_id') == selected_segment_id for segment in merged_segments):
            if self._select_word_item(selected_segment_id):
                return

        if fallback_group and self._select_group_item(fallback_group):
            return

        if owner_group:
            self._select_group_item(owner_group)

    def sync_from_tree(self):
        """
        Reconstructs self.groups based on tree structure.
        Crucial for Drag-and-Drop.
        """
        new_groups = {}
        
        # Iterate top level items (Groups)
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            grp_item = root.child(i)
            grp_name = grp_item.text(0)
            
            # Find existing data or create new if name lost (should match)
            # We must preserve 'files' list from old dict if possible
            existing_data = self.groups.get(grp_name, {"files": [], "words": []})
            
            files = existing_data["files"]
            words = []
            
            # Iterate children (Words)
            for j in range(grp_item.childCount()):
                word_item = grp_item.child(j)
                # Retrieve the full word object we stored earlier
                word_data = word_item.data(0, Qt.UserRole + 1)
                if word_data:
                    words.append(word_data)
                    
            new_groups[grp_name] = {"files": files, "words": words}

        referenced_file_keys = {
            canonicalize_audio_path(word['file_path'])
            for group_data in new_groups.values()
            for word in group_data['words']
            if word.get('file_path')
        }

        for group_data in new_groups.values():
            group_data['files'] = [
                file_entry for file_entry in group_data['files']
                if file_entry.get('path') and canonicalize_audio_path(file_entry['path']) in referenced_file_keys
            ]
            
        self.groups = new_groups
        # Refresh right panel if needed?
        self.on_selection_changed()

    def add_group(self, checked=False):
        count = len(self.groups) + 1
        name = f"Group {count}"
        while name in self.groups:
            count += 1
            name = f"Group {count}"
            
        self.groups[name] = {"files": [], "words": []}
        self.populate_tree()
        self._select_group_item(name)

    def delete_current_group(self, checked=False):
        item = self.tree.currentItem()
        if not item: return
        
        # Check if group
        role = item.data(0, Qt.UserRole)
        if role == "group":
            name = item.text(0)
            del self.groups[name]
            self.populate_tree()
            self.clear_right_panel()

    def rename_group_item(self, item):
        """Rename the specified group item"""
        if not item: return
        
        role = item.data(0, Qt.UserRole)
        if role != "group":
            return
            
        old_name = item.text(0)
        new_name, ok = QInputDialog.getText(self, "Rename Group", "New Name:", text=old_name)
        
        if ok and new_name and new_name != old_name:
            if new_name in self.groups:
                QMessageBox.warning(self, "Error", "Group name already exists.")
                return
            
            # Update data structure: Re-key dictionary
            self.groups[new_name] = self.groups.pop(old_name)
            
            # Update UI directly
            item.setText(0, new_name)

    def on_selection_changed(self):
        items = self.tree.selectedItems()
        self._show_details_for_item(items[0] if items else None)

    def on_tree_item_clicked(self, item, col):
        self._show_details_for_item(item)

    def on_item_double_clicked(self, item, col):
        role = item.data(0, Qt.UserRole)
        if role == "group":
            self.rename_group_item(item)
        elif role == "word":
            word_data = item.data(0, Qt.UserRole + 1)
            if not word_data:
                return
            parent_item = item.parent()
            fallback_group = parent_item.text(0) if parent_item else None
            self._open_recording_editor(word_data['file_path'], word_data.get('segment_id'), fallback_group)

    def clear_right_panel(self):
        self._active_word_item = None
        self._active_word_text_input = None

        # Clear layout
        while self.right_layout.count():
            child = self.right_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

    def _update_word_in_groups(self, updated_word_data):
        segment_id = updated_word_data.get('segment_id')
        file_key = canonicalize_audio_path(updated_word_data.get('file_path', ''))

        for group_data in self.groups.values():
            for word in group_data['words']:
                same_segment = segment_id and word.get('segment_id') == segment_id
                same_file_and_index = (
                    canonicalize_audio_path(word.get('file_path', '')) == file_key
                    and word.get('seg_index') == updated_word_data.get('seg_index')
                )
                if same_segment or same_file_and_index:
                    word['text'] = updated_word_data.get('text', '')
                    return

    def _commit_active_word_text(self):
        if not self._active_word_item or not self._active_word_text_input:
            return

        word_data = self._active_word_item.data(0, Qt.UserRole + 1)
        if not word_data:
            return

        text = self._active_word_text_input.text()
        if word_data.get('text', '') != text:
            word_data['text'] = text
            self._active_word_item.setData(0, Qt.UserRole + 1, word_data)
            self._active_word_item.setText(0, self._format_word_label(word_data))
            self._update_word_in_groups(word_data)

    def show_group_details(self, group_name):
        self.clear_right_panel()
        
        grp_data = self.groups.get(group_name)
        if not grp_data: return
        
        files = grp_data["files"]
        word_count = len(grp_data["words"])
        
        # Title
        lbl = QLabel(f"Details: {group_name}")
        lbl.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.right_layout.addWidget(lbl)
        
        self.right_layout.addWidget(QLabel(f"Total Words: {word_count}"))
        self.right_layout.addWidget(QLabel(f"Source Files: {len(files)}"))
        
        # File Actions
        act_box = QGroupBox("File Management")
        al = QVBoxLayout()
        
        btn_up = QPushButton("Upload Audio Files to this Group")
        btn_up.clicked.connect(lambda checked: self.upload_files_to_group(group_name))
        al.addWidget(btn_up)
        
        act_box.setLayout(al)
        self.right_layout.addWidget(act_box)
        
        self.right_layout.addStretch()

    def eventFilter(self, source, event):
        if event.type() == QEvent.KeyPress:
            # Navigation
            if event.key() == Qt.Key_Up:
                self.navigate_tree_selection(-1)
                return True # Consume
            elif event.key() == Qt.Key_Down:
                self.navigate_tree_selection(1)
                return True # Consume
            
            # Note: Enter key for Play is handled via returnPressed signal on the QLineEdit
            # to respect standard widget behavior, but we keep the fallback here if needed,
            # though returnPressed is preferred for LineEdit.
                
        return super().eventFilter(source, event)

    def navigate_tree_selection(self, delta):
        """Move tree selection up (-1) or down (1), skipping groups if desired or traversing linearly."""
        current = self.tree.currentItem()
        if not current: return
        
        # We can just use itemAbove / itemBelow which traverses visible items
        next_item = self.tree.itemBelow(current) if delta > 0 else self.tree.itemAbove(current)
        
        if next_item:
            self.tree.setCurrentItem(next_item)
            # If focusing a text box in right panel, we want to KEEP focus there, 
            # but on_selection_changed recreates the panel and destroys the focused widget.
            # However, the user wants "always allow arrows". 
            # If we recreate the panel, the focus is lost effectively. 
            # But the new panel will have a text box. We could try to focus it?
            # Actually, `populate_tree` or `show_word_details` logic handles the UI build.
            # If the user is typing, hits Down, we save the text (via textChanged), move selection,
            # new panel appears. We probably want to auto-focus the text input if it was focused.
            
            # Let's try to set focus to the new text input if we were in one.
            # The creation happens in show_word_details.

    def play_current_active_word(self):
        item = self.tree.currentItem()
        if item and item.data(0, Qt.UserRole) == "word":
            word_data = item.data(0, Qt.UserRole + 1)
            self.play_word_audio(word_data)

    def show_word_details(self, word_data, item):
        self.clear_right_panel()
        
        lbl = QLabel("Word Details")
        lbl.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.right_layout.addWidget(lbl)
        self.right_layout.addWidget(QLabel(f"Source Recording: {Path(word_data['file_path']).name}"))
        
        # Actions
        btn_play = QPushButton("▶ Play Audio (Enter)")
        btn_play.clicked.connect(lambda checked: self.play_word_audio(word_data))
        self.right_layout.addWidget(btn_play)

        parent_item = item.parent()
        fallback_group = parent_item.text(0) if parent_item else None
        btn_edit_slices = QPushButton("Review / Edit Recording Slices")
        btn_edit_slices.clicked.connect(
            lambda checked: self._open_recording_editor(
                word_data['file_path'],
                word_data.get('segment_id'),
                fallback_group,
            )
        )
        self.right_layout.addWidget(btn_edit_slices)
        
        # Text Assignment (Vertical Layout)
        self.right_layout.addWidget(QLabel("Assigned Text:"))
        
        txt_input = QLineEdit()
        txt_input.setText(word_data.get("text", ""))
        self.right_layout.addWidget(txt_input)
        self._active_word_item = item
        self._active_word_text_input = txt_input
        
        # Install Event Filter to capture Arrow Keys even when focused
        txt_input.installEventFilter(self)
        
        # Connect Enter key to Play Audio
        txt_input.returnPressed.connect(lambda: self.play_word_audio(word_data))
        
        # Auto-focus if we just navigated here (optional, but good UX)
        # Use QTimer to ensure focus happens after layout stabilizes
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, txt_input.setFocus)
        
        def update_text(text):
            word_data["text"] = text
            # CRITICAL FIX: Update the actual data stored in the item so it syncs correctly
            item.setData(0, Qt.UserRole + 1, word_data)
            self._update_word_in_groups(word_data)
            
            # Update tree label
            item.setText(0, self._format_word_label(word_data))
            
        txt_input.textChanged.connect(update_text)
        txt_input.editingFinished.connect(self._commit_active_word_text)
        
        # Delete Button
        btn_del = QPushButton("Delete Word")
        btn_del.setStyleSheet("background-color: #ffebee; color: #c62828;")
        def delete_word():
            parent = item.parent()
            if parent:
                parent.removeChild(item)
                # If group empty? Keep group.
                self.sync_from_tree()
                self.clear_right_panel()
        btn_del.clicked.connect(delete_word)
        self.right_layout.addWidget(btn_del)
        
        self.right_layout.addStretch()

    def upload_files_to_group(self, group_name):
        if not self.processor: return
        files, _ = QFileDialog.getOpenFileNames(self, "Select Audio", "", "Audio (*.wav *.mp3 *.m4a)")
        if not files: return
        
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.parent.status_msg.setText("Processing...") # type: ignore
        
        stats = []
        errors = []
        new_files = []
        try:
            for fpath in files:
                try:
                    segments, dur = self.processor.detect_segments(fpath)
                except Exception as e:
                    errors.append(f"{Path(fpath).name}: {str(e)}")
                    continue

                normalized_segments = normalize_segments(fpath, segments)
                
                # File Entry
                f_entry = {
                    "path": fpath, 
                    "original_name": Path(fpath).name,
                    "segments": normalized_segments,
                    "duration": dur,
                    "auto_slice_word": True,
                }
                self.groups[group_name]["files"].append(f_entry)
                new_files.append(f_entry)
                
                # Create Words
                for seg in normalized_segments:
                    self.groups[group_name]["words"].append(self._create_word_entry(fpath, seg))
                
                stats.append(f"{Path(fpath).name}: {len(normalized_segments)} words")
        finally:
            QApplication.restoreOverrideCursor()
            self.parent.status_msg.setText("Ready") # type: ignore

        if not stats and errors:
            QMessageBox.warning(self, "Upload Failed", "No files were processed successfully.\n\n" + "\n".join(errors))
            return

        self.populate_tree()
        self._select_group_item(group_name)
        
        # Prompt for immediate review
        msg = QMessageBox(self)
        msg.setWindowTitle("Upload Report")
        report_lines = list(stats)
        if errors:
            report_lines.append("")
            report_lines.append("Failed files:")
            report_lines.extend(errors)
        msg.setText("\n".join(report_lines))
        btn_review = msg.addButton("Review / Edit Files", QMessageBox.ActionRole)
        btn_ok = msg.addButton("OK", QMessageBox.AcceptRole)
        msg.exec_()
        
        if msg.clickedButton() == btn_review:
            self.review_files_in_group(group_name, review_files=new_files)

    def review_files_in_group(self, group_name, review_files=None):
        # Open the file list dialog
        # (Same logic as 'open_file_review_dialog' in previous version)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Review Files: {group_name}")
        dialog.resize(560, 440)
        lay = QVBoxLayout(dialog)

        select_all_checkbox = QCheckBox("Select / Deselect All")
        lay.addWidget(select_all_checkbox)
        
        file_list = QListWidget()
        files = list(review_files) if review_files is not None else self.groups[group_name]["files"]
        for f in files:
            f.setdefault('auto_slice_word', True)

        updating_items = {'value': False}

        for f in files:
            item = QListWidgetItem(self._format_review_file_label(f))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if f.get('auto_slice_word', True) else Qt.Unchecked)
            file_list.addItem(item)
            
        lay.addWidget(QLabel("Check recordings to auto-slice them. Uncheck to keep the whole recording as one word. Double-click a file to edit its segments manually."))
        lay.addWidget(file_list)

        btn_close = QPushButton("Close")
        lay.addWidget(btn_close)

        def refresh_select_all_checkbox():
            if not files:
                all_checked = False
            else:
                all_checked = all(f.get('auto_slice_word', True) for f in files)

            select_all_checkbox.blockSignals(True)
            select_all_checkbox.setChecked(all_checked)
            select_all_checkbox.blockSignals(False)

        def apply_auto_slice_to_item(item, enabled):
            idx = file_list.row(item)
            if idx < 0 or idx >= len(files):
                return

            file_data = files[idx]
            if file_data.get('auto_slice_word', True) == enabled:
                item.setText(self._format_review_file_label(file_data))
                return

            QApplication.setOverrideCursor(Qt.WaitCursor)
            self.parent.status_msg.setText("Updating recording slices...")  # type: ignore
            try:
                file_data, _segments = self._set_file_auto_slice(file_data['path'], enabled)
                item.setText(self._format_review_file_label(file_data))
                self.populate_tree()
                self._select_group_item(group_name)
            except Exception as e:
                file_data['auto_slice_word'] = not enabled
                updating_items['value'] = True
                item.setCheckState(Qt.Checked if file_data.get('auto_slice_word', True) else Qt.Unchecked)
                updating_items['value'] = False
                QMessageBox.warning(dialog, "Slice Update Failed", f"Could not update {file_data['original_name']}:\n{e}")
            finally:
                QApplication.restoreOverrideCursor()
                self.parent.status_msg.setText("Ready")  # type: ignore
                refresh_select_all_checkbox()

        def on_item_changed(item):
            if updating_items['value']:
                return
            apply_auto_slice_to_item(item, item.checkState() == Qt.Checked)

        def on_select_all_toggled(checked):
            target_state = Qt.Checked if checked else Qt.Unchecked
            changed_items = []

            updating_items['value'] = True
            for i in range(file_list.count()):
                item = file_list.item(i)
                if item is None:
                    continue
                if item.checkState() != target_state:
                    item.setCheckState(target_state)
                    changed_items.append(item)
            updating_items['value'] = False

            for item in changed_items:
                apply_auto_slice_to_item(item, target_state == Qt.Checked)

            refresh_select_all_checkbox()
        
        def open_editor(item):
            idx = file_list.row(item)
            file_data = files[idx]
            self._open_recording_editor(file_data['path'], fallback_group=group_name, dialog_parent=dialog, list_item=item)

        file_list.itemChanged.connect(on_item_changed)
        file_list.itemDoubleClicked.connect(open_editor)
        select_all_checkbox.toggled.connect(on_select_all_toggled)
        btn_close.clicked.connect(dialog.accept)
        refresh_select_all_checkbox()
        dialog.exec_()

    def play_word_audio(self, word_data):
        if not self.processor:
            return
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        temp = self.processor.get_temp_segment_file(
            word_data['file_path'], word_data['start'], word_data['end'], context="preview"
        )
        if temp:
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(temp))))
            self.media_player.play()
        
    def go_next(self):
        self.sync_from_tree()
        self.parent.show_experiment_properties(self.groups, self.loaded_properties) # type: ignore


from PyQt5.QtWidgets import QHeaderView 

class ExperimentPropertiesPage(QWidget):
    """
    Export logic updated for Refactored structure.
    Preserves original files.
    """
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.groups = {}
        self.current_group_order = [] 
        # Stores the current active sequence of (group_name, index) blocks
        self.active_block_sequence = []
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        header = QHBoxLayout()
        btn_back = QPushButton("← Back")
        btn_back.clicked.connect(self.parent.show_new_experiment) # type: ignore
        header.addWidget(btn_back)
        layout.addLayout(header)
        
        # Scroll Area for Experiment Properties
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form_layout = QVBoxLayout(content)
        
        # 1. Experiment Name
        name_box = QGroupBox("Experiment Name")
        name_layout = QVBoxLayout()
        self.txt_exp_name = QLineEdit("My Experiment")
        name_layout.addWidget(self.txt_exp_name)
        name_box.setLayout(name_layout)
        form_layout.addWidget(name_box)
        
        # 2. Grid Size
        grid_box = QGroupBox("1. Grid Size")
        grid_layout = QHBoxLayout()
        grid_layout.addWidget(QLabel("Rows:"))
        self.spin_rows = QSpinBox()
        self.spin_rows.setRange(1, 20)
        self.spin_rows.setValue(5)
        grid_layout.addWidget(self.spin_rows)
        
        grid_layout.addWidget(QLabel("Columns:"))
        self.spin_cols = QSpinBox()
        self.spin_cols.setRange(1, 20)
        self.spin_cols.setValue(5)
        grid_layout.addWidget(self.spin_cols)
        grid_layout.addStretch()
        grid_box.setLayout(grid_layout)
        form_layout.addWidget(grid_box)

        # 3. Repetitions (Moved ABOVE Order)
        repeat_box = QGroupBox("2. Word Repetitions")
        repeat_layout = QVBoxLayout()
        
        repeat_info = QLabel("Specify how many times each word group should be repeated:")
        repeat_info.setStyleSheet("color: gray; font-size: 12px; margin-bottom: 10px;")
        repeat_layout.addWidget(repeat_info)
        
        # Create spin boxes for each group (will be populated in set_data)
        self.repeat_widgets = {}
        self.repeat_container = QWidget()
        self.repeat_form = QFormLayout(self.repeat_container)
        repeat_layout.addWidget(self.repeat_container)
        
        repeat_note = QLabel(
            "• Random order: Each word repeated X times with spacing\n"
            "• Stiff order: The listed sequence plays once (repeats ignored)"
        )
        repeat_note.setStyleSheet("color: gray; font-size: 11px; font-style: italic; margin-top: 5px;")
        repeat_layout.addWidget(repeat_note)
        
        repeat_box.setLayout(repeat_layout)
        form_layout.addWidget(repeat_box)
        
        # 4. Word Reading Order
        order_box = QGroupBox("3. Word Reading Order")
        order_layout = QVBoxLayout()
        
        self.radio_random = QRadioButton("Randomized (Runtime)")
        self.radio_stiff = QRadioButton("Fixed / Stiff Order")
        self.radio_random.setChecked(True)
        self.radio_random.toggled.connect(self.toggle_order_view)
        
        order_layout.addWidget(self.radio_random)
        order_layout.addWidget(self.radio_stiff)
        
        # Stiff Order Editor
        self.stiff_container = QWidget()
        stiff_layout = QVBoxLayout(self.stiff_container)
        
        # Editor Buttons
        btn_layout = QHBoxLayout()
        btn_shuffle_within = QPushButton("Shuffle Within Groups")
        btn_shuffle_between = QPushButton("Shuffle Between Groups")
        btn_reset = QPushButton("Reset")
        
        btn_shuffle_within.clicked.connect(self.shuffle_within_groups)
        btn_shuffle_between.clicked.connect(self.shuffle_between_groups)
        btn_reset.clicked.connect(self.reset_order)
        
        btn_layout.addWidget(btn_shuffle_within)
        btn_layout.addWidget(btn_shuffle_between)
        btn_layout.addWidget(btn_reset)
        stiff_layout.addLayout(btn_layout)
        
        self.btn_change_groups = QPushButton("Change Group Order")
        self.btn_change_groups.clicked.connect(self.change_group_order_dialog)
        stiff_layout.addWidget(self.btn_change_groups)
        
        self.word_list = QListWidget()
        self.word_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.word_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.word_list.setFixedHeight(300)
        stiff_layout.addWidget(self.word_list)
        
        order_layout.addWidget(self.stiff_container)
        order_box.setLayout(order_layout)
        form_layout.addWidget(order_box)
        
        # 5. Proceed Condition
        proceed_box = QGroupBox("4. Proceed to Next Word")
        proceed_layout = QVBoxLayout()
        
        # Option 1: Key Press
        self.radio_key = QRadioButton("Key Press (Space/Enter/Tap)")
        self.radio_key.setChecked(True)
        proceed_layout.addWidget(self.radio_key)
        
        # Option 2: Time Delay
        time_layout = QHBoxLayout()
        self.radio_time = QRadioButton("Time Delay:")
        time_layout.addWidget(self.radio_time)
        self.spin_delay = QSpinBox()
        self.spin_delay.setRange(0, 10000)
        self.spin_delay.setValue(2000)
        self.spin_delay.setSuffix(" ms")
        time_layout.addWidget(self.spin_delay)
        time_layout.addWidget(QLabel("after audio ends"))
        time_layout.addStretch()
        proceed_layout.addLayout(time_layout)
        
        # UX: Disable time delay if Key Press is selected
        self.radio_time.toggled.connect(self.spin_delay.setEnabled)
        self.spin_delay.setEnabled(False) # Default is Key Press
        
        proceed_box.setLayout(proceed_layout)
        form_layout.addWidget(proceed_box)
        
        # 6. Beep Sounds
        beep_box = QGroupBox("5. Beep Sounds")
        beep_layout = QVBoxLayout()
        
        # Before
        before_layout = QHBoxLayout()
        self.chk_beep_before = QCheckBox("Beep BEFORE word")
        before_layout.addWidget(self.chk_beep_before)
        self.spin_beep_before = QSpinBox()
        self.spin_beep_before.setRange(0, 5000)
        self.spin_beep_before.setValue(100)
        self.spin_beep_before.setSuffix(" ms")
        before_layout.addWidget(self.spin_beep_before)
        before_layout.addWidget(QLabel("before start"))
        before_layout.addStretch()
        beep_layout.addLayout(before_layout)
        
        # UX: Toggle spinbox
        self.chk_beep_before.toggled.connect(self.spin_beep_before.setEnabled)
        self.spin_beep_before.setEnabled(False)
        
        # After
        after_layout = QHBoxLayout()
        self.chk_beep_after = QCheckBox("Beep AFTER word")
        after_layout.addWidget(self.chk_beep_after)
        self.spin_beep_after = QSpinBox()
        self.spin_beep_after.setRange(0, 5000)
        self.spin_beep_after.setValue(100)
        self.spin_beep_after.setSuffix(" ms")
        after_layout.addWidget(self.spin_beep_after)
        after_layout.addWidget(QLabel("after end"))
        after_layout.addStretch()
        beep_layout.addLayout(after_layout)
        
        # UX: Toggle spinbox
        self.chk_beep_after.toggled.connect(self.spin_beep_after.setEnabled)
        self.spin_beep_after.setEnabled(False)
        
        beep_box.setLayout(beep_layout)
        form_layout.addWidget(beep_box)
        
        scroll.setWidget(content)
        layout.addWidget(scroll)
        
        # Footer
        footer = QHBoxLayout()
        self.btn_export = QPushButton("Export Experiment Package")
        self.btn_export.setProperty("class", "primary")
        self.btn_export.setFixedSize(250, 50)
        self.btn_export.clicked.connect(self.export_package)
        footer.addStretch()
        footer.addWidget(self.btn_export)
        footer.addStretch()
        layout.addLayout(footer)

    def set_data(self, groups, loaded_properties=None):
        self.groups = groups
        self.current_group_order = list(groups.keys())
        
        # Calculate suggested grid size from total words
        total_words = 0
        for g_data in groups.values():
            total_words += len(g_data.get('words', []))
            
        import math
        side = math.ceil(math.sqrt(total_words)) if total_words > 0 else 5
        self.spin_rows.setValue(side)
        self.spin_cols.setValue(side)
        
        # Populate Repetition Widgets
        while self.repeat_form.count():
            child = self.repeat_form.takeAt(0)
            if child.widget(): child.widget().deleteLater()
        self.repeat_widgets.clear()
        
        for group_name in sorted(groups.keys()):
            spin = QSpinBox()
            spin.setRange(1, 100)
            spin.setValue(1) 
            spin.setFixedWidth(80)
            # Connect change signal to update list immediately
            # When count changes, we rebuild the default block sequence based on seed order
            spin.valueChanged.connect(self.rebuild_default_blocks)
            self.repeat_widgets[group_name] = spin
            self.repeat_form.addRow(f"{group_name}:", spin)
            
        # Initialize Stiff List
        self.rebuild_default_blocks()

        if loaded_properties:
            self._apply_loaded_properties(loaded_properties)

        self.toggle_order_view()

    def _normalize_loaded_block_sequence(self, raw_sequence):
        normalized = []
        for item in raw_sequence or []:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            group_name = item[0]
            if group_name not in self.groups:
                continue
            try:
                block_index = int(item[1]) if len(item) > 1 else 1
            except Exception:
                block_index = 1
            normalized.append((group_name, max(1, block_index)))
        return normalized

    def _apply_loaded_properties(self, loaded_properties):
        self.txt_exp_name.setText(str(loaded_properties.get('name', self.txt_exp_name.text())))

        grid = loaded_properties.get('grid', {}) or {}
        self.spin_rows.setValue(int(grid.get('rows', self.spin_rows.value()) or self.spin_rows.value()))
        self.spin_cols.setValue(int(grid.get('cols', self.spin_cols.value()) or self.spin_cols.value()))

        repetitions = loaded_properties.get('repetitions', {}) or {}
        for group_name, spin in self.repeat_widgets.items():
            try:
                repeat_count = int(repetitions.get(group_name, spin.value()))
            except Exception:
                repeat_count = spin.value()
            spin.blockSignals(True)
            spin.setValue(max(1, repeat_count))
            spin.blockSignals(False)

        block_sequence = self._normalize_loaded_block_sequence(loaded_properties.get('active_block_sequence'))
        if block_sequence:
            self.active_block_sequence = block_sequence

            seen_groups = []
            for group_name, _ in block_sequence:
                if group_name not in seen_groups:
                    seen_groups.append(group_name)
            self.current_group_order = seen_groups + [g for g in self.groups.keys() if g not in seen_groups]
        else:
            self.rebuild_default_blocks()

        proceed = loaded_properties.get('proceed_condition', {}) or {}
        proceed_type = proceed.get('type', 'key')
        self.radio_key.setChecked(proceed_type != 'time')
        self.radio_time.setChecked(proceed_type == 'time')
        try:
            self.spin_delay.setValue(int(proceed.get('delay_ms', self.spin_delay.value()) or self.spin_delay.value()))
        except Exception:
            pass

        beeps = loaded_properties.get('beeps', {}) or {}
        before_cfg = beeps.get('before', {}) or {}
        after_cfg = beeps.get('after', {}) or {}

        self.chk_beep_before.setChecked(bool(before_cfg.get('enabled')))
        self.chk_beep_after.setChecked(bool(after_cfg.get('enabled')))
        try:
            self.spin_beep_before.setValue(int(before_cfg.get('delay_ms', self.spin_beep_before.value()) or self.spin_beep_before.value()))
        except Exception:
            pass
        try:
            self.spin_beep_after.setValue(int(after_cfg.get('delay_ms', self.spin_beep_after.value()) or self.spin_beep_after.value()))
        except Exception:
            pass

        order = loaded_properties.get('order', 'random')
        self.radio_stiff.setChecked(order == 'stiff')
        self.radio_random.setChecked(order != 'stiff')

        if order == 'stiff' and loaded_properties.get('sequence'):
            self._restore_stiff_sequence(loaded_properties.get('sequence', []))
        else:
            self.reset_order()

    def _restore_stiff_sequence(self, sequence_ids):
        self.word_list.clear()

        words_by_id = {}
        for group_name, group_data in self.groups.items():
            for word in group_data.get('words', []):
                word_id = word.get('id')
                if word_id:
                    words_by_id[word_id] = (group_name, word)

        missing_ids = []
        for word_id in sequence_ids:
            group_and_word = words_by_id.get(word_id)
            if not group_and_word:
                missing_ids.append(word_id)
                continue
            group_name, word = group_and_word
            self._add_word_to_list(word, group_name)

        if missing_ids:
            self.reset_order()

    def rebuild_default_blocks(self):
        """Generates active_block_sequence based on current_group_order and repetition counts."""
        self.active_block_sequence = []
        for g_name in self.current_group_order:
            if g_name in self.groups:
                count = 1
                if g_name in self.repeat_widgets:
                    count = self.repeat_widgets[g_name].value()
                
                for i in range(count):
                    # Tuple: (group_name, repetition_index 1-based)
                    self.active_block_sequence.append((g_name, i+1))
        
        self.reset_order()

    def toggle_order_view(self):
        show_stiff = self.radio_stiff.isChecked()
        self.stiff_container.setVisible(show_stiff)

    def _add_word_to_list(self, word_data, group_name):
        txt = word_data.get('text', '') or '[No Text]'
        label = f"{group_name}: {txt}"
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, word_data)
        item.setData(Qt.UserRole + 1, group_name)
        self.word_list.addItem(item)

    def reset_order(self):
        """Reset list to active block order"""
        self.word_list.clear()
        
        for g_name, idx in self.active_block_sequence:
            count = 1
            if g_name in self.repeat_widgets:
                count = self.repeat_widgets[g_name].value()
            
            # Format display string "GroupName" or "GroupName (i+1)"
            display_name = f"{g_name} ({idx})" if count > 1 else g_name
            
            if g_name in self.groups:
                for w in self.groups[g_name]['words']:
                     self._add_word_to_list(w, display_name)
                         
        self.btn_change_groups.setVisible(True)

    def shuffle_within_groups(self):
        """Shuffle words within each group instance, preserving group block structure"""
        import random
        self.word_list.clear()
        
        for g_name, idx in self.active_block_sequence:
            count = 1
            if g_name in self.repeat_widgets:
                count = self.repeat_widgets[g_name].value()
                
            display_name = f"{g_name} ({idx})" if count > 1 else g_name
            
            if g_name in self.groups:
                # Create a fresh copy of words to shuffle independently
                words = list(self.groups[g_name]['words'])
                random.shuffle(words)
                
                for w in words:
                    self._add_word_to_list(w, display_name)
            
        self.btn_change_groups.setVisible(True)

    def shuffle_between_groups(self):
        """Shuffle all words globally, including all repeated instances"""
        import random
        all_words_with_group = []
        
        for g_name, idx in self.active_block_sequence:
            count = 1
            if g_name in self.repeat_widgets:
                count = self.repeat_widgets[g_name].value()
                
            display_name = f"{g_name} ({idx})" if count > 1 else g_name
            
            if g_name in self.groups:
                for w in self.groups[g_name]['words']:
                     all_words_with_group.append((display_name, w))
                
        random.shuffle(all_words_with_group)
        
        self.word_list.clear()
        for display_name, w in all_words_with_group:
            self._add_word_to_list(w, display_name)
            
        self.btn_change_groups.setVisible(False)

    def change_group_order_dialog(self):
        """Dialog to reorder groups and their repeated blocks"""
        dlg = QDialog(self)
        dlg.setWindowTitle("Reorder Groups")
        l = QVBoxLayout(dlg)
        
        list_g = QListWidget()
        list_g.setDragDropMode(QAbstractItemView.InternalMove)
        
        # Populate with current BLOCK sequence
        for g_name, idx in self.active_block_sequence:
            count = 1
            if g_name in self.repeat_widgets:
                count = self.repeat_widgets[g_name].value()
            
            label = f"{g_name} ({idx})" if count > 1 else g_name
            item = QListWidgetItem(label)
            # Store data to reconstruct sequence
            item.setData(Qt.UserRole, g_name)
            item.setData(Qt.UserRole + 1, idx)
            list_g.addItem(item)
            
        l.addWidget(QLabel("Drag to reorder groups:"))
        l.addWidget(list_g)
        
        btn_ok = QPushButton("Apply Order")
        btn_ok.clicked.connect(dlg.accept)
        l.addWidget(btn_ok)
        
        if dlg.exec_() == QDialog.Accepted:
            new_block_sequence = []
            for i in range(list_g.count()):
                item = list_g.item(i)
                if item is None:
                    continue
                g_name = item.data(Qt.UserRole)
                idx = item.data(Qt.UserRole + 1)
                new_block_sequence.append((g_name, idx))
                
            self.active_block_sequence = new_block_sequence
            # Apply reset to enforce new block order
            self.reset_order()

    def export_package(self):
        exp_name = self.txt_exp_name.text().strip()
        if not exp_name:
            QMessageBox.warning(self, "Warning", "Please enter an experiment name")
            return

        save_path, _ = QFileDialog.getSaveFileName(self, "Save Experiment", f"{exp_name}.zip", "ZIP (*.zip)")
        if not save_path: return

        try:
            temp_dir = tempfile.mkdtemp()
            # 1. Create centralized Media folder
            media_root = Path(temp_dir) / "media"
            media_root.mkdir()

            global_files_config = []
            path_to_filename_map = {} # abs_path -> unique_filename
            used_filenames = set()

            # Pass 1: Collect ALL unique file paths from groups and words 
            all_source_paths = set()
            for grp in self.groups.values():
                for f in grp["files"]:
                    all_source_paths.add(str(Path(f["path"]).resolve()))
                for w in grp["words"]:
                    all_source_paths.add(str(Path(w["file_path"]).resolve()))
            
            # Copy Files and Build Global File List
            for val_path in all_source_paths:
                src_path = Path(val_path)
                if not src_path.exists():
                    print(f"Skipping missing file: {src_path}")
                    continue
                    
                # Handle filename collisions
                base_name = src_path.name
                stem = src_path.stem
                suffix = src_path.suffix
                counter = 1
                new_name = base_name
                
                while new_name in used_filenames:
                    new_name = f"{stem}_{counter}{suffix}"
                    counter += 1
                
                used_filenames.add(new_name)
                path_to_filename_map[val_path] = new_name
                
                # Copy to media folder
                shutil.copy2(src_path, media_root / new_name)

                owner_group = None
                matched_file_entry = None
                for group_name, group_data in self.groups.items():
                    for file_entry in group_data.get("files", []):
                        if str(Path(file_entry["path"]).resolve()) == val_path:
                            owner_group = group_name
                            matched_file_entry = file_entry
                            break
                    if owner_group:
                        break
                
                global_files_config.append({
                    "file_name": new_name,
                    "path": f"media/{new_name}",
                    "original_name": base_name,
                    "owner_group": owner_group,
                    "auto_slice_word": matched_file_entry.get("auto_slice_word", True) if matched_file_entry else True,
                })

            # Pass 2: Build Groups Config
            config_groups = []
            
            for grp_name, grp_data in self.groups.items():
                word_list_config = []
                for w in grp_data["words"]:
                    w_path = str(Path(w["file_path"]).resolve())
                    media_ref = path_to_filename_map.get(w_path, "MISSING_FILE")
                    
                    word_list_config.append({
                        "id": w["id"],
                        "text": w["text"],
                        "source_file": media_ref,
                        "start_ms": w["start"],
                        "end_ms": w["end"]
                    })

                config_groups.append({
                    "name": grp_name,
                    "words": word_list_config
                })
            
            # 3. Determine specific sequence if Stiff Order
            sequence_ids = []
            if self.radio_stiff.isChecked():
                for i in range(self.word_list.count()):
                    item = self.word_list.item(i)
                    if item is None:
                        continue
                    word_data = item.data(Qt.UserRole)
                    if word_data and 'id' in word_data:
                        # Append ID with optional decoration if needed, 
                        # but receiver just needs the ID for lookup.
                        # Wait, if we have duplicate words (due to repetition), 
                        # they have the SAME ID. This works if the player list is just a sequence of IDs.
                        sequence_ids.append(word_data['id'])
            
            # If Random Order, we generate the structure for runtime randomization
            # We need to tell the runtime player about the repetitions.
            # The JSON config includes "repetitions": {...}
            # so the runtime player can generate the sequence itself.
            # But "active_block_sequence" is only for Stiff Order generation currently.
            # If Random order is chosen, do we want to respect the user's custom block order?
            # E.g. Block sequence: B(1), A(1), B(2).
            # And Random toggle is on: "Each word repeated X times with spacing". 
            # Usually random ignores blocks.
            # However, if the user explicitly reordered blocks, maybe they want "Block Randomization"?
            # For now, we stick to the original spec: "sequence" is only for Stiff.
            # "repetitions" is for Random.

            # 4. Final Config Structure
            config = {
                "app_version": APP_VERSION,
                "name": exp_name,
                "grid": {"rows": self.spin_rows.value(), "cols": self.spin_cols.value()},
                "order": "stiff" if self.radio_stiff.isChecked() else "random",
                "sequence": sequence_ids, 
                "repetitions": {group: spin.value() for group, spin in self.repeat_widgets.items()},
                "active_block_sequence": self.active_block_sequence, # NEW: Save this so we can restore UI on load
                "proceed_condition": {
                    "type": "key" if self.radio_key.isChecked() else "time",
                    "delay_ms": self.spin_delay.value()
                },
                "beeps": {
                    "before": {
                        "enabled": self.chk_beep_before.isChecked(),
                        "delay_ms": self.spin_beep_before.value()
                    },
                    "after": {
                        "enabled": self.chk_beep_after.isChecked(),
                        "delay_ms": self.spin_beep_after.value()
                    }
                },
                "files": global_files_config,
                "groups": config_groups
            }
            
            with open(Path(temp_dir) / f"{exp_name}.json", 'w') as f:
                json.dump(config, f, indent=2)

            shutil.make_archive(str(Path(save_path).with_suffix('')), 'zip', temp_dir)
            
            shutil.rmtree(temp_dir)
            QMessageBox.information(self, "Success", "Export Complete!")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))
            traceback.print_exc()
