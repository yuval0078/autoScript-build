import sys
import os
import json
import zipfile
import tempfile
import shutil
import numpy as np
import math
import traceback
from pathlib import Path
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

    def load_file(self, file_path, segments=None):
        try:
            audio = AudioSegment.from_file(file_path)
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
            print(f"Error loading waveform: {e}")

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
        self.segments = [dict(s) for s in segments] 
        self.processor = processor
        self.media_player = QMediaPlayer()
        self.init_ui()
        self.waveform.load_file(file_path, self.segments)
        
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
        btn_save.clicked.connect(self.accept)
        footer.addStretch()
        footer.addWidget(btn_save)
        layout.addLayout(footer)
        self.refresh_list()

    def update_sens_label(self, val):
        self.lbl_sens.setText(f"{val} dB")

    def reslice_file(self):
        thresh = self.slider_sens.value()
        reply = QMessageBox.question(self, "Confirm Re-Slice", 
                                     "This will overwrite all current segments with new detection settings.\nContinue?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            new_segs, _ = self.processor.detect_segments(
                self.file_path, 
                silence_thresh=thresh, 
                min_silence_len=200
            )
            self.segments = new_segs
            self.waveform.segments = self.segments
            self.waveform.update()
            self.refresh_list()

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
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        temp_file = self.processor.get_temp_segment_file(self.file_path, start, end, context=context)
        if temp_file:
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(temp_file))))
            self.media_player.play()

    def add_segment(self):
        start = self.waveform.x_to_ms(10)
        end = start + 500
        new_seg = {'start': start, 'end': end, 'duration': 500, 'index': len(self.segments)+1}
        self.segments.append(new_seg)
        self.segments.sort(key=lambda x: x['start'])
        self.waveform.update()
        self.refresh_list()

    def delete_segment(self):
        idx = self.waveform.selected_segment_index
        if idx >= 0:
            del self.segments[idx]
            self.waveform.selected_segment_index = -1
            self.waveform.update()
            self.refresh_list()

    def get_segments(self):
        return self.segments


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
            grp_item.setFlags(grp_item.flags() | Qt.ItemIsDropEnabled)
            # Tag as group
            grp_item.setData(0, Qt.UserRole, "group")
            
            for word in grp_data["words"]:
                word_item = QTreeWidgetItem(grp_item)
                label = f"{word['text'] if word['text'] else '[No Text]'} ({Path(word['file_path']).name} #{word['seg_index']})"
                word_item.setText(0, label)
                word_item.setFlags(word_item.flags() | Qt.ItemIsDragEnabled | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                # Tag as word
                word_item.setData(0, Qt.UserRole, "word")
                # Store dict reference
                word_item.setData(0, Qt.UserRole + 1, word)
                
            grp_item.setExpanded(True)

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
        item = self.tree.currentItem()
        if not item: 
            self.clear_right_panel()
            return
        
        role = item.data(0, Qt.UserRole)
        
        if role == "group":
            self.show_group_details(item.text(0))
        elif role == "word":
            word_data = item.data(0, Qt.UserRole + 1)
            self.show_word_details(word_data, item)
            
            # Additional focus assurance for mouse clicks
            # Find the line edit in the new panel and focus it
            inputs = self.right_panel.findChildren(QLineEdit)
            if inputs:
                from PyQt5.QtCore import QTimer
                QTimer.singleShot(0, inputs[0].setFocus)

    def on_item_double_clicked(self, item, col):
        role = item.data(0, Qt.UserRole)
        if role == "group":
            self.rename_group_item(item)
        elif role == "word":
            # Play or edit? User says double click usually plays or edits.
            # Let's open the fine-tuning editor
            word_data = item.data(0, Qt.UserRole + 1)
            self.edit_word_dialog(word_data, item)

    def clear_right_panel(self):
        # Clear layout
        while self.right_layout.count():
            child = self.right_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

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
        
        # Actions
        btn_play = QPushButton("▶ Play Audio (Enter)")
        btn_play.clicked.connect(lambda checked: self.play_word_audio(word_data))
        self.right_layout.addWidget(btn_play)
        
        # Text Assignment (Vertical Layout)
        self.right_layout.addWidget(QLabel("Assigned Text:"))
        
        txt_input = QLineEdit()
        txt_input.setText(word_data.get("text", ""))
        self.right_layout.addWidget(txt_input)
        
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
            
            # Update tree label
            label = f"{text if text else '[No Text]'} ({Path(word_data['file_path']).name} #{word_data['seg_index']})"
            item.setText(0, label)
            
        txt_input.textChanged.connect(update_text)
        
        btn_fine = QPushButton("Fine-tune (Visual Editor)")
        btn_fine.clicked.connect(lambda checked: self.edit_word_dialog(word_data, item))
        self.right_layout.addWidget(btn_fine)
        
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
        for fpath in files:
            segments, dur = self.processor.detect_segments(fpath)
            
            # File Entry
            f_entry = {
                "path": fpath, 
                "original_name": Path(fpath).name,
                "segments": segments,
                "duration": dur
            }
            self.groups[group_name]["files"].append(f_entry)
            
            # Create Words
            for seg in segments:
                w_entry = {
                    "id": f"{Path(fpath).stem}_{seg['index']}",
                    "text": "",
                    "file_path": fpath,
                    "start": seg['start'],
                    "end": seg['end'],
                    "seg_index": seg['index']
                }
                self.groups[group_name]["words"].append(w_entry)
            
            stats.append(f"{Path(fpath).name}: {len(segments)} words")
            
        QApplication.restoreOverrideCursor()
        self.parent.status_msg.setText("Ready") # type: ignore
        self.populate_tree()
        
        # Prompt for immediate review
        msg = QMessageBox(self)
        msg.setWindowTitle("Upload Report")
        msg.setText("\n".join(stats))
        btn_review = msg.addButton("Review / Edit Files", QMessageBox.ActionRole)
        btn_ok = msg.addButton("OK", QMessageBox.AcceptRole)
        msg.exec_()
        
        if msg.clickedButton() == btn_review:
            self.review_files_in_group(group_name)

    def review_files_in_group(self, group_name):
        # Open the file list dialog
        # (Same logic as 'open_file_review_dialog' in previous version)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Review Files: {group_name}")
        dialog.resize(500, 400)
        lay = QVBoxLayout(dialog)
        
        file_list = QListWidget()
        files = self.groups[group_name]["files"]
        for f in files:
            file_list.addItem(f"{f['original_name']} ({len(f['segments'])} words)")
            
        lay.addWidget(QLabel("Double-click a file to edit segments:"))
        lay.addWidget(file_list)
        
        def open_editor(item):
            idx = file_list.row(item)
            file_data = files[idx]
            
            editor = FileEditorWindow(file_data['path'], file_data['segments'], self.processor, dialog)
            if editor.exec_() == QDialog.Accepted:
                new_segs = editor.get_segments()
                file_data['segments'] = new_segs
                
                # Re-sync words: Remove old words for this file, add new ones
                grp_words = self.groups[group_name]["words"]
                # Filter out old
                self.groups[group_name]["words"] = [w for w in grp_words if w['file_path'] != file_data['path']]
                
                # Add new
                for seg in new_segs:
                    w_entry = {
                        "id": f"{Path(file_data['path']).stem}_{seg['index']}",
                        "text": "",
                        "file_path": file_data['path'],
                        "start": seg['start'],
                        "end": seg['end'],
                        "seg_index": seg['index']
                    }
                    self.groups[group_name]["words"].append(w_entry)
                
                item.setText(f"{file_data['original_name']} ({len(new_segs)} words)")
                # Refresh main tree
                self.populate_tree()

        file_list.itemDoubleClicked.connect(open_editor)
        dialog.exec_()

    def play_word_audio(self, word_data):
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        temp = self.processor.get_temp_segment_file(
            word_data['file_path'], word_data['start'], word_data['end'], context="preview"
        )
        if temp:
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(temp))))
            self.media_player.play()

    def edit_word_dialog(self, word_data, item_to_update):
        # Single word editor
        dlg = QDialog(self)
        dlg.setWindowTitle("Fine Tune Word")
        lay = QVBoxLayout(dlg)
        
        wf = WaveformWidget()
        seg = [{'start': word_data['start'], 'end': word_data['end'], 'index': 1}]
        wf.load_file(word_data['file_path'], seg)
        
        # Zoom logic
        dur = word_data['end'] - word_data['start']
        padding = dur * 0.5
        wf.zoom_level = wf.duration_ms / (dur + 2*padding) if dur > 0 else 1
        start_view = max(0, word_data['start'] - padding)
        wf.scroll_offset = start_view / wf.duration_ms
        
        lay.addWidget(wf)
        
        # Play button in fine tune
        btn_play = QPushButton("Play Segment")
        def play():
            curr_seg = wf.segments[0]
            self.media_player.stop()
            self.media_player.setMedia(QMediaContent())
            t_f = self.processor.get_temp_segment_file(word_data['file_path'], curr_seg['start'], curr_seg['end'], "fine_tune")
            if t_f:
                 self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(t_f))))
                 self.media_player.play()
        btn_play.clicked.connect(play)
        lay.addWidget(btn_play)
        
        btn_save = QPushButton("Save")
        def save():
            new_seg = wf.segments[0]
            word_data['start'] = new_seg['start']
            word_data['end'] = new_seg['end']
            
            # Update UI label if needed
            label = f"{word_data['text'] if word_data['text'] else '[No Text]'} ({Path(word_data['file_path']).name} #{word_data['seg_index']})"
            item_to_update.setText(0, label)
            
            dlg.accept()
        btn_save.clicked.connect(save)
        lay.addWidget(btn_save)
        
        dlg.exec_()
        
    def go_next(self):
        self.sync_from_tree()
        self.parent.show_experiment_properties(self.groups) # type: ignore


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
        self.current_group_order = sorted(groups.keys())
        
        # Calculate suggested grid size from total words
        total_words = 0
        for g_data in groups.values():
            total_words += len(g_data.get('words', []))
            
        import math
        side = math.ceil(math.sqrt(total_words)) if total_words > 0 else 5
        self.spin_rows.setValue(side)
        self.spin_cols.setValue(side)
        
        if loaded_properties:
            # TODO: Handle loaded properties for re-importing
            pass

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
        self.toggle_order_view()

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
                
                global_files_config.append({
                    "file_name": new_name,
                    "path": f"media/{new_name}",
                    "original_name": base_name
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
