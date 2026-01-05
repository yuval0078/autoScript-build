import sys
import os
import json
import zipfile
import tempfile
import shutil
import numpy as np
from pathlib import Path
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QListWidget, QListWidgetItem, 
                             QTextEdit, QMessageBox, QGroupBox, QSplitter, 
                             QSpinBox, QComboBox, QRadioButton, QCheckBox, 
                             QLineEdit, QFormLayout, QDialog, QScrollArea,
                             QApplication)
from PyQt5.QtCore import Qt, QUrl, QPointF, QRectF
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
    """Widget for displaying and editing audio waveform with start/end markers"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.audio_data = None
        self.sample_rate = 44100
        self.duration_ms = 0
        
        self.start_ms = 0
        self.end_ms = 0
        
        self.zoom_level = 1.0
        self.scroll_offset = 0
        
        self.dragging = None  # 'start', 'end', or None
        
        # Colors
        self.wave_color = QColor(24, 119, 242)
        self.start_color = QColor(76, 175, 80)
        self.end_color = QColor(244, 67, 54)
        self.bg_color = QColor(255, 255, 255)
        
        self.setMouseTracking(True)

    def set_audio_data(self, file_path, start_ms, end_ms):
        """Load audio data for visualization"""
        try:
            # Load audio
            audio = AudioSegment.from_file(file_path)
            
            # Convert to mono and get samples
            audio = audio.set_channels(1)
            self.sample_rate = audio.frame_rate
            self.duration_ms = len(audio)
            
            # Get raw data as numpy array
            samples = np.array(audio.get_array_of_samples())
            
            # Downsample for display (max 10000 points for performance)
            target_points = 10000
            if len(samples) > target_points:
                step = len(samples) // target_points
                self.audio_data = samples[::step]
            else:
                self.audio_data = samples
                
            self.start_ms = start_ms
            self.end_ms = end_ms
            
            # Center view on the word
            self.zoom_to_fit_word()
            
            self.update()
            
        except Exception as e:
            print(f"Error loading waveform: {e}")

    def zoom_to_fit_word(self):
        """Zoom and scroll to show the word with some margin"""
        if self.duration_ms == 0:
            return
            
        word_len = self.end_ms - self.start_ms
        margin = word_len * 0.5  # 50% margin on each side
        
        view_start = max(0, self.start_ms - margin)
        view_end = min(self.duration_ms, self.end_ms + margin)
        view_len = view_end - view_start
        
        if view_len > 0:
            self.zoom_level = self.duration_ms / view_len
            self.scroll_offset = view_start / self.duration_ms
        else:
            self.zoom_level = 1.0
            self.scroll_offset = 0

    def ms_to_x(self, ms):
        """Convert time (ms) to x coordinate"""
        width = self.width()
        total_visible_ms = self.duration_ms / self.zoom_level
        start_visible_ms = self.scroll_offset * self.duration_ms
        
        rel_ms = ms - start_visible_ms
        return (rel_ms / total_visible_ms) * width

    def x_to_ms(self, x):
        """Convert x coordinate to time (ms)"""
        width = self.width()
        total_visible_ms = self.duration_ms / self.zoom_level
        start_visible_ms = self.scroll_offset * self.duration_ms
        
        rel_ms = (x / width) * total_visible_ms
        return start_visible_ms + rel_ms

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Background
        painter.fillRect(self.rect(), self.bg_color)
        
        if self.audio_data is None:
            painter.drawText(self.rect(), Qt.AlignCenter, "No Audio Data")
            return
            
        width = self.width()
        height = self.height()
        mid_y = height / 2
        
        # Draw Waveform
        painter.setPen(QPen(self.wave_color, 1))
        
        # Calculate visible range in samples
        total_samples = len(self.audio_data)
        visible_samples = int(total_samples / self.zoom_level)
        start_sample = int(self.scroll_offset * total_samples)
        end_sample = min(total_samples, start_sample + visible_samples)
        
        if visible_samples <= 0: 
            return
        
        # Draw points
        subset = self.audio_data[start_sample:end_sample]
        if len(subset) == 0: 
            return
        
        # Normalize amplitude to height
        max_amp = np.max(np.abs(self.audio_data)) if len(self.audio_data) > 0 else 1
        if max_amp == 0: 
            max_amp = 1
        
        # Create polygon points
        points = []
        step = max(1, len(subset) // width)
        
        for i in range(0, len(subset), step):
            x = (i / len(subset)) * width
            amp = subset[i] / max_amp
            y = mid_y - (amp * (height / 2) * 0.9)
            points.append(QPointF(x, y))
            
        if points:
            painter.drawPolyline(QPolygonF(points))
            
        # Draw Markers
        start_x = self.ms_to_x(self.start_ms)
        end_x = self.ms_to_x(self.end_ms)
        
        # Start Marker
        painter.setPen(QPen(self.start_color, 2))
        painter.drawLine(int(start_x), 0, int(start_x), height)
        painter.drawText(int(start_x) + 5, 20, "START")
        
        # End Marker
        painter.setPen(QPen(self.end_color, 2))
        painter.drawLine(int(end_x), 0, int(end_x), height)
        painter.drawText(int(end_x) - 30, height - 10, "END")
        
        # Highlight selected region
        region_rect = QRectF(start_x, 0, end_x - start_x, height)
        painter.fillRect(region_rect, QColor(76, 175, 80, 30))

    def mousePressEvent(self, event):
        x = event.x()
        
        # Check if clicking near markers (within 10px)
        start_x = self.ms_to_x(self.start_ms)
        end_x = self.ms_to_x(self.end_ms)
        
        if abs(x - start_x) < 10:
            self.dragging = 'start'
        elif abs(x - end_x) < 10:
            self.dragging = 'end'
        else:
            self.dragging = None

    def mouseMoveEvent(self, event):
        x = event.x()
        ms = self.x_to_ms(x)
        
        # Update cursor
        start_x = self.ms_to_x(self.start_ms)
        end_x = self.ms_to_x(self.end_ms)
        
        if abs(x - start_x) < 10 or abs(x - end_x) < 10:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.ArrowCursor)
            
        # Handle dragging
        if self.dragging == 'start':
            self.start_ms = max(0, min(ms, self.end_ms - 10))
            self.update()
        elif self.dragging == 'end':
            self.end_ms = min(self.duration_ms, max(ms, self.start_ms + 10))
            self.update()

    def mouseReleaseEvent(self, event):
        self.dragging = None

    def wheelEvent(self, event):
        # Zoom in/out
        delta = event.angleDelta().y()
        zoom_factor = 1.1 if delta > 0 else 0.9
        
        # Limit zoom
        new_zoom = self.zoom_level * zoom_factor
        new_zoom = max(1.0, min(new_zoom, 50.0))  # Max 50x zoom
        
        # Adjust scroll to keep center focused
        center_x = event.x()
        center_ms = self.x_to_ms(center_x)
        
        self.zoom_level = new_zoom
        
        # Recalculate scroll offset to keep center_ms at center_x
        total_visible_ms = self.duration_ms / self.zoom_level
        new_start_ms = center_ms - (center_x / self.width()) * total_visible_ms
        
        self.scroll_offset = max(0, min(1.0, new_start_ms / self.duration_ms))
        
        self.update()

    def get_range(self):
        """Get the current start/end range in milliseconds"""
        return int(self.start_ms), int(self.end_ms)


class AudioEditorWindow(QDialog):
    """Window for precise audio slicing"""
    
    def __init__(self, audio_groups, current_group, current_word_idx, processor, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Audio Slice Editor")
        self.resize(1000, 600)
        
        self.audio_groups = audio_groups
        self.current_group = current_group
        self.current_word_idx = current_word_idx
        self.processor = processor
        self.media_player = QMediaPlayer()
        
        self.init_ui()
        # Load the current word AFTER UI is fully built
        self.load_current_word()
        
    def init_ui(self):
        layout = QHBoxLayout(self)
        
        # Left: Word List
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        
        left_layout.addWidget(QLabel("Words in Group:"))
        self.word_list = QListWidget()
        left_layout.addWidget(self.word_list)
        
        # Add delete button
        btn_delete = QPushButton("🗑 Delete Selected Word")
        btn_delete.clicked.connect(self.delete_word)
        left_layout.addWidget(btn_delete)
        
        # Populate list (but don't connect signal yet)
        group_data = self.audio_groups[self.current_group]
        for i, seg in enumerate(group_data['segments']):
            item = QListWidgetItem(f"Word {i+1}: {int(seg['start'])}ms - {int(seg['end'])}ms")
            self.word_list.addItem(item)
        
        left_panel.setFixedWidth(250)
        layout.addWidget(left_panel)
        
        # Right: Editor
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        # Info
        self.lbl_info = QLabel()
        self.lbl_info.setStyleSheet("font-size: 16px; font-weight: bold;")
        right_layout.addWidget(self.lbl_info)
        
        # Waveform
        self.waveform = WaveformWidget()
        right_layout.addWidget(self.waveform, 1)
        
        # Controls
        controls = QHBoxLayout()
        
        btn_play = QPushButton("▶ Play Slice")
        btn_play.clicked.connect(self.play_slice)
        controls.addWidget(btn_play)
        
        right_layout.addLayout(controls)
        
        # Instructions
        instructions = QLabel(
            "Controls:\n"
            "• Drag Green/Red lines to adjust Start/End\n"
            "• Scroll mouse wheel to Zoom In/Out\n"
            "• Left/Right Arrows to fine-tune selected marker\n"
            "• Up/Down Arrows to switch words\n"
            "• Changes are saved automatically"
        )
        instructions.setStyleSheet("color: gray; margin-top: 10px;")
        right_layout.addWidget(instructions)
        
        layout.addWidget(right_panel)
        
        # Now connect the signal AFTER UI is built
        self.word_list.currentRowChanged.connect(self.change_word)
        # Set initial selection
        self.word_list.setCurrentRow(self.current_word_idx)

    def load_current_word(self):
        """Load waveform data for the current word"""
        idx = self.current_word_idx
        if idx < 0: 
            return
        
        group_data = self.audio_groups[self.current_group]
        segment = group_data['segments'][idx]
        file_path = segment.get('file_path', group_data['file_path'])
        
        self.lbl_info.setText(f"Editing Word {idx+1}")
        
        # Load into waveform
        self.waveform.set_audio_data(file_path, segment['start'], segment['end'])

    def change_word(self, idx):
        """Handle switching to a different word"""
        # Save the previous word before switching
        if self.current_word_idx != idx and self.current_word_idx >= 0:
            self.save_changes_internal(self.current_word_idx)
            
        self.current_word_idx = idx
        self.load_current_word()

    def save_changes_internal(self, idx):
        """Save changes to the audio groups data structure"""
        start, end = self.waveform.get_range()
        group_data = self.audio_groups[self.current_group]
        
        # Update data
        group_data['segments'][idx]['start'] = start
        group_data['segments'][idx]['end'] = end
        group_data['segments'][idx]['duration'] = end - start
        
        # Update list item text
        item = self.word_list.item(idx)
        item.setText(f"Word {idx+1}: {start}ms - {end}ms")

    def save_changes(self):
        """Explicitly save changes (with user confirmation)"""
        self.save_changes_internal(self.word_list.currentRow())
        QMessageBox.information(self, "Saved", "Changes saved successfully.")

    def delete_word(self):
        """Delete the currently selected word"""
        current_row = self.word_list.currentRow()
        if current_row < 0:
            QMessageBox.warning(self, "Warning", "No word selected for deletion.")
            return
        
        reply = QMessageBox.question(
            self, 
            "Confirm Delete", 
            f"Delete Word {current_row + 1}?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            group_data = self.audio_groups[self.current_group]
            
            # Delete from both segments and text_words to keep them in sync
            del group_data['segments'][current_row]
            if current_row < len(group_data.get('text_words', [])):
                del group_data['text_words'][current_row]
            
            # Temporarily disconnect signal to prevent change_word from being called
            self.word_list.currentRowChanged.disconnect(self.change_word)
            
            # Refresh the list
            self.word_list.clear()
            for i, seg in enumerate(group_data['segments']):
                item = QListWidgetItem(f"Word {i+1}: {int(seg['start'])}ms - {int(seg['end'])}ms")
                self.word_list.addItem(item)
            
            # Select next word or previous if last was deleted
            new_row = min(current_row, self.word_list.count() - 1)
            if new_row >= 0:
                self.word_list.setCurrentRow(new_row)
                self.current_word_idx = new_row
                self.load_current_word()
            else:
                self.current_word_idx = -1
            
            # Reconnect signal
            self.word_list.currentRowChanged.connect(self.change_word)

    def play_slice(self):
        """Play the current audio slice"""
        start, end = self.waveform.get_range()
        group_data = self.audio_groups[self.current_group]
        segment = group_data['segments'][self.current_word_idx]
        file_path = segment.get('file_path', group_data['file_path'])
        
        # Stop and clear previous media to release file lock
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        
        # Clean up old temp file if it exists
        old_temp = "temp_playback_editor.wav"
        if os.path.exists(old_temp):
            try:
                os.remove(old_temp)
            except:
                pass
        
        # Get temp file (use 'editor' context to avoid conflicts)
        temp_file = self.processor.get_temp_segment_file(file_path, start, end, context="editor")
        if temp_file:
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(temp_file))))
            self.media_player.play()

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts"""
        if event.key() == Qt.Key_Left:
            if self.waveform.dragging == 'start':
                self.waveform.start_ms = max(0, self.waveform.start_ms - 10)
            elif self.waveform.dragging == 'end':
                self.waveform.end_ms = max(self.waveform.start_ms + 10, self.waveform.end_ms - 10)
            self.waveform.update()
            
        elif event.key() == Qt.Key_Right:
            if self.waveform.dragging == 'start':
                self.waveform.start_ms = min(self.waveform.end_ms - 10, self.waveform.start_ms + 10)
            elif self.waveform.dragging == 'end':
                self.waveform.end_ms = min(self.waveform.duration_ms, self.waveform.end_ms + 10)
            self.waveform.update()
            
        elif event.key() == Qt.Key_Up:
            row = self.word_list.currentRow()
            if row > 0:
                self.word_list.setCurrentRow(row - 1)
                
        elif event.key() == Qt.Key_Down:
            row = self.word_list.currentRow()
            if row < self.word_list.count() - 1:
                self.word_list.setCurrentRow(row + 1)
                
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        """Handle dialog close - save changes and cleanup"""
        # Save current on close (only if valid index)
        if self.current_word_idx >= 0 and self.current_word_idx < len(self.audio_groups[self.current_group]['segments']):
            self.save_changes_internal(self.word_list.currentRow())
        
        # Cleanup temp files
        temp_files = ["temp_playback_editor.wav", "temp_full_source_editor.wav"]
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
        
        super().closeEvent(event)


class NewExperimentWizard(QWidget):
    """Wizard for creating a new experiment with audio groups and word lists"""
    
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.processor = AudioProcessor(verbose=False) if AudioProcessor else None
        self.audio_groups = {}  # {group_name: {'file_path': str, 'segments': [], 'text_words': []}}
        self.current_group = None
        self.media_player = QMediaPlayer()
        self.loaded_properties = None  # Store loaded experiment properties
        
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QHBoxLayout()
        btn_back = QPushButton("← Back")
        btn_back.clicked.connect(self.parent.show_main_menu)
        header.addWidget(btn_back)
        header.addStretch()
        title = QLabel("New Experiment Setup")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)
        
        # Main Content
        splitter = QSplitter(Qt.Horizontal)
        
        # Left Panel: Groups & Audio
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        
        # Step 1: Upload Audio
        group_box = QGroupBox("1. Audio Groups")
        group_layout = QVBoxLayout()
        
        # Buttons layout
        btn_layout = QHBoxLayout()
        
        btn_upload = QPushButton("Upload Audio Files (mp3, wav, m4a)")
        btn_upload.setProperty("class", "primary")
        btn_upload.clicked.connect(self.upload_audio)
        btn_layout.addWidget(btn_upload)
        
        btn_load_exp = QPushButton("Load Experiment")
        btn_load_exp.clicked.connect(self.load_experiment_zip)
        btn_layout.addWidget(btn_load_exp)
        
        group_layout.addLayout(btn_layout)
        
        self.group_list = QListWidget()
        self.group_list.itemClicked.connect(self.select_group)
        group_layout.addWidget(self.group_list)
        
        group_box.setLayout(group_layout)
        left_layout.addWidget(group_box)
        
        # Audio Preview List
        preview_box = QGroupBox("Audio Slices")
        preview_layout = QVBoxLayout()
        self.slice_list = QListWidget()
        self.slice_list.itemDoubleClicked.connect(self.play_slice)
        preview_layout.addWidget(self.slice_list)
        
        # Edit Button
        self.btn_edit_slices = QPushButton("✂ Edit Slices")
        self.btn_edit_slices.clicked.connect(self.open_slice_editor)
        self.btn_edit_slices.setEnabled(False)
        preview_layout.addWidget(self.btn_edit_slices)
        
        lbl_hint = QLabel("Double-click to play")
        lbl_hint.setStyleSheet("color: gray; font-size: 12px;")
        preview_layout.addWidget(lbl_hint)
        
        preview_box.setLayout(preview_layout)
        left_layout.addWidget(preview_box)
        
        splitter.addWidget(left_panel)
        
        # Right Panel: Text Input
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        text_box = QGroupBox("2. Word List")
        text_layout = QVBoxLayout()
        
        text_controls = QHBoxLayout()
        btn_load_text = QPushButton("Load Text File")
        btn_load_text.clicked.connect(self.load_text_file)
        text_controls.addWidget(btn_load_text)
        text_controls.addStretch()
        text_layout.addLayout(text_controls)
        
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("Enter words here (one per line)\nNumber of lines must match number of audio slices.")
        self.text_edit.textChanged.connect(self.validate_current_group)
        text_layout.addWidget(self.text_edit)
        
        self.lbl_status = QLabel("Waiting for input...")
        self.lbl_status.setStyleSheet("font-weight: bold; color: #666;")
        text_layout.addWidget(self.lbl_status)
        
        text_box.setLayout(text_layout)
        right_layout.addWidget(text_box)
        
        splitter.addWidget(right_panel)
        splitter.setSizes([400, 600])
        layout.addWidget(splitter)
        
        # Footer
        footer = QHBoxLayout()
        self.btn_next = QPushButton("Next: Properties →")
        self.btn_next.setProperty("class", "primary")
        self.btn_next.setFixedSize(200, 50)
        self.btn_next.setEnabled(False)
        self.btn_next.clicked.connect(self.go_next)
        footer.addStretch()
        footer.addWidget(self.btn_next)
        footer.addStretch()
        layout.addLayout(footer)

    def upload_audio(self):
        """Upload and process audio files"""
        files, _ = QFileDialog.getOpenFileNames(self, "Select Audio Files", "", "Audio Files (*.mp3 *.wav *.m4a)")
        if not files:
            return
            
        if not self.processor:
            QMessageBox.critical(self, "Error", "AudioProcessor not initialized.")
            return

        # Process files
        for file_path in files:
            path = Path(file_path)
            group_name = path.stem
            
            # Check if already exists
            if group_name in self.audio_groups:
                continue
                
            # Slice audio (get segments)
            try:
                QApplication.setOverrideCursor(Qt.WaitCursor)
                # Use process_single_file to get segments
                segments = self.processor.process_single_file(str(path))
                QApplication.restoreOverrideCursor()
                
                if not segments:
                    QMessageBox.warning(self, "Warning", f"No words detected in {path.name}")
                    continue
                    
                self.audio_groups[group_name] = {
                    'file_path': str(path),
                    'segments': segments,
                    'text_words': [],
                    'valid': False
                }
                
                self.group_list.addItem(group_name)
                
                # Check for matching text file
                txt_path = path.with_suffix('.txt')
                if txt_path.exists():
                    with open(txt_path, 'r', encoding='utf-8') as f:
                        text = f.read()
                        # Store temporarily, will be set when group is selected
                        self.audio_groups[group_name]['pending_text'] = text
                        
            except Exception as e:
                QApplication.restoreOverrideCursor()
                QMessageBox.critical(self, "Error", f"Failed to process {path.name}: {e}")

        # Select last added
        if self.group_list.count() > 0:
            self.group_list.setCurrentRow(self.group_list.count() - 1)
            self.select_group(self.group_list.currentItem())

    def load_experiment_zip(self):
        """Load a previously exported experiment ZIP file"""
        # Warn if there's existing data
        if self.audio_groups:
            reply = QMessageBox.warning(
                self,
                "Confirm Load",
                "Loading an experiment will override all current data.\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
        
        # Open file dialog
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Experiment Package",
            "",
            "ZIP Files (*.zip)"
        )
        if not file_path:
            return
        
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            
            # Create temporary directory for extraction
            temp_dir = tempfile.mkdtemp()
            
            # Extract ZIP
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
            
            # Find and load JSON config
            json_files = list(Path(temp_dir).glob("*.json"))
            if not json_files:
                raise FileNotFoundError("No configuration JSON found in ZIP")
            
            json_file = json_files[0]
            with open(json_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            # Clear existing data
            self.audio_groups.clear()
            self.loaded_properties = config.get('properties', {})
            
            # Load audio files and word associations
            audio_dir = Path(temp_dir) / "audio"
            words_data = config.get('words', {})
            
            # Create audio groups from the loaded data
            for group_name, words_list in words_data.items():
                segments = []
                text_words = []
                
                for i, word_item in enumerate(words_list):
                    audio_file = word_item.get('file', '')
                    word_text = word_item.get('word', '')
                    
                    # Construct full path to audio file
                    audio_path = audio_dir / audio_file
                    
                    if not audio_path.exists():
                        raise FileNotFoundError(f"Audio file not found: {audio_file}")
                    
                    # Get audio duration
                    audio = AudioSegment.from_file(str(audio_path))
                    duration = len(audio)
                    
                    segments.append({
                        'start': 0,
                        'end': duration,
                        'duration': duration,
                        'file_path': str(audio_path)
                    })
                    text_words.append(word_text)
                
                # Store the actual audio file path (copy to temp location or reference original)
                # We'll reference the extracted audio directly
                if segments:
                    self.audio_groups[group_name] = {
                        # Keep a group-level file_path for backward compatibility; per-word audio comes from segment['file_path']
                        'file_path': str(audio_dir / words_data[group_name][0]['file']),
                        'segments': segments,
                        'text_words': text_words,
                        'valid': len(segments) == len(text_words),
                        'temp_dir': temp_dir,  # Store for cleanup later
                        'audio_dir': str(audio_dir)  # Store audio directory
                    }
            
            QApplication.restoreOverrideCursor()
            
            # Update UI
            self.group_list.clear()
            for group_name in sorted(self.audio_groups.keys()):
                self.group_list.addItem(group_name)
            
            # Select first group
            if self.group_list.count() > 0:
                self.group_list.setCurrentRow(0)
                self.select_group(self.group_list.currentItem())
            
            # Show confirmation
            exp_name = self.loaded_properties.get('experiment_name', 'Experiment')
            QMessageBox.information(
                self,
                "Success",
                f"Loaded experiment: {exp_name}\n\n"
                f"Groups: {len(self.audio_groups)}\n"
                f"Total words: {sum(len(g['segments']) for g in self.audio_groups.values())}"
            )
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to load experiment: {e}")
            import traceback
            traceback.print_exc()

    def select_group(self, item):
        """Handle selection of an audio group"""
        if not item:
            self.btn_edit_slices.setEnabled(False)
            return
            
        group_name = item.text()
        self.current_group = group_name
        data = self.audio_groups[group_name]
        
        # Populate slice list
        self.slice_list.clear()
        for i, seg in enumerate(data['segments']):
            self.slice_list.addItem(f"Word {i+1} ({int(seg['duration'])}ms)")
            
        self.btn_edit_slices.setEnabled(True)
            
        # Populate text
        if 'pending_text' in data:
            self.text_edit.setPlainText(data['pending_text'])
            del data['pending_text']
        else:
            self.text_edit.setPlainText("\n".join(data['text_words']))
            
        self.validate_current_group()

    def open_slice_editor(self):
        """Open the audio slice editor dialog"""
        if not self.current_group: 
            return
        
        current_row = self.slice_list.currentRow()
        if current_row < 0: 
            current_row = 0
        
        editor = AudioEditorWindow(
            self.audio_groups, 
            self.current_group, 
            current_row, 
            self.processor, 
            self
        )
        editor.exec_()
        
        # Refresh list after edit
        self.select_group(self.group_list.currentItem())
        # Restore selection
        if current_row < self.slice_list.count():
            self.slice_list.setCurrentRow(current_row)

    def load_text_file(self):
        """Load word list from a text file"""
        if not self.current_group:
            return
            
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Text File", "", "Text Files (*.txt)")
        if file_path:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.text_edit.setPlainText(f.read())

    def validate_current_group(self):
        """Validate that text words match audio slices"""
        if not self.current_group:
            return
            
        text = self.text_edit.toPlainText().strip()
        lines = [l.strip() for l in text.split('\n') if l.strip()]
        
        data = self.audio_groups[self.current_group]
        audio_count = len(data['segments'])
        text_count = len(lines)
        
        data['text_words'] = lines
        
        if audio_count == text_count:
            self.lbl_status.setText(f"✓ Match! {audio_count} words.")
            self.lbl_status.setStyleSheet("color: green; font-weight: bold;")
            data['valid'] = True
        else:
            self.lbl_status.setText(f"⚠ Mismatch: {audio_count} audio slices vs {text_count} text lines.")
            self.lbl_status.setStyleSheet("color: red; font-weight: bold;")
            data['valid'] = False
            
        self.check_all_valid()

    def check_all_valid(self):
        """Check if all groups are valid and enable/disable Next button"""
        if not self.audio_groups:
            self.btn_next.setEnabled(False)
            return
            
        all_valid = all(g['valid'] for g in self.audio_groups.values())
        self.btn_next.setEnabled(all_valid)

    def play_slice(self, item):
        """Play the selected audio slice"""
        if not self.processor or not self.current_group:
            return
            
        row = self.slice_list.row(item) if item else self.slice_list.currentRow()
        if row < 0:
            return
        data = self.audio_groups[self.current_group]
        segment = data['segments'][row]
        file_path = segment.get('file_path', data['file_path'])
        
        # Stop and clear previous media to release file lock
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        
        # Clean up old temp file if it exists
        old_temp = "temp_playback_main.wav"
        if os.path.exists(old_temp):
            try:
                os.remove(old_temp)
            except:
                pass
        
        # Get temp file for playback (use 'main' context to avoid conflicts)
        temp_file = self.processor.get_temp_segment_file(file_path, segment['start'], segment['end'], context="main")
        
        if temp_file and os.path.exists(temp_file):
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(os.path.abspath(temp_file))))
            self.media_player.play()

    def play_current_slice(self):
        """Play the currently selected slice"""
        self.play_slice(None)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts"""
        if event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            self.play_current_slice()
        else:
            super().keyPressEvent(event)

    def go_next(self):
        """Proceed to the experiment properties page"""
        # Cleanup temp files before moving to next page
        temp_files = ["temp_playback_main.wav", "temp_full_source_main.wav"]
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
        self.parent.show_experiment_properties(self.audio_groups, self.loaded_properties)

    def delete_selected_words(self):
        """Delete selected words from the current group"""
        selected_items = self.slice_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Warning", "No words selected for deletion.")
            return

        group_data = self.audio_groups[self.current_group]
        for item in selected_items:
            row = self.slice_list.row(item)
            del group_data['segments'][row]

        self.select_group(self.group_list.currentItem())

    def delete_group(self):
        """Delete the currently selected group"""
        current_item = self.group_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "Warning", "No group selected for deletion.")
            return

        group_name = current_item.text()
        del self.audio_groups[group_name]
        self.group_list.takeItem(self.group_list.row(current_item))
        self.slice_list.clear()
        self.text_edit.clear()
        self.lbl_status.setText("Waiting for input...")
        self.btn_edit_slices.setEnabled(False)


class ExperimentPropertiesPage(QWidget):
    """Page for configuring experiment properties and exporting the package"""
    
    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.audio_groups = {}
        self.loaded_properties = None
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Header
        header = QHBoxLayout()
        btn_back = QPushButton("← Back")
        btn_back.clicked.connect(self.parent.show_new_experiment)
        header.addWidget(btn_back)
        header.addStretch()
        title = QLabel("Experiment Properties")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)
        
        # Scroll Area for content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form_layout = QVBoxLayout(content)
        form_layout.setSpacing(20)
        
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
        
        # 3. Word Reading Order
        order_box = QGroupBox("2. Word Reading Order")
        order_layout = QVBoxLayout()
        self.radio_random = QRadioButton("Randomized (Mix all groups)")
        self.radio_random.setChecked(True)
        self.radio_ordinal = QRadioButton("Ordinal (Group by group)")
        order_layout.addWidget(self.radio_random)
        order_layout.addWidget(self.radio_ordinal)
        order_box.setLayout(order_layout)
        form_layout.addWidget(order_box)
        
        # 4. Proceed Condition
        proceed_box = QGroupBox("3. Proceed to Next Word")
        proceed_layout = QVBoxLayout()
        
        # Option 1: Key Press
        key_layout = QHBoxLayout()
        self.radio_key = QRadioButton("Press Key:")
        self.radio_key.setChecked(True)
        key_layout.addWidget(self.radio_key)
        self.combo_key = QComboBox()
        self.combo_key.addItems(["Space", "Enter", "Right Arrow", "Down Arrow"])
        key_layout.addWidget(self.combo_key)
        key_layout.addStretch()
        proceed_layout.addLayout(key_layout)
        
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
        
        proceed_box.setLayout(proceed_layout)
        form_layout.addWidget(proceed_box)
        
        # 4. Repetitions
        repeat_box = QGroupBox("4. Word Repetitions")
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
            "• Ordinal order: All groups played once, then entire sequence repeats"
        )
        repeat_note.setStyleSheet("color: gray; font-size: 11px; font-style: italic; margin-top: 5px;")
        repeat_layout.addWidget(repeat_note)
        
        repeat_box.setLayout(repeat_layout)
        form_layout.addWidget(repeat_box)
        
        # 5. Beep Sounds
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

    def set_data(self, audio_groups, loaded_properties=None):
        """Set the audio groups data and configure UI with optional loaded properties"""
        self.audio_groups = audio_groups
        self.loaded_properties = loaded_properties
        
        # Auto-calculate grid size suggestion
        total_words = sum(len(g['segments']) for g in audio_groups.values())
        import math
        side = math.ceil(math.sqrt(total_words))
        
        # Set experiment name
        if loaded_properties:
            original_name = loaded_properties.get('experiment_name', 'Experiment')
            self.txt_exp_name.setText(f"{original_name}var")
            
            # Apply grid settings
            grid = loaded_properties.get('grid', {})
            self.spin_rows.setValue(grid.get('rows', side))
            self.spin_cols.setValue(grid.get('cols', side))
            
            # Apply word reading order
            order = loaded_properties.get('order', 'random')
            if order == 'ordinal':
                self.radio_ordinal.setChecked(True)
                self.radio_random.setChecked(False)
            else:
                self.radio_random.setChecked(True)
                self.radio_ordinal.setChecked(False)
            
            # Apply proceed condition
            proceed = loaded_properties.get('proceed_condition', {})
            if proceed.get('type') == 'time':
                self.radio_time.setChecked(True)
                self.radio_key.setChecked(False)
                self.spin_delay.setValue(proceed.get('delay_ms', 2000))
            else:
                self.radio_key.setChecked(True)
                self.radio_time.setChecked(False)
                key_name = proceed.get('key', 'Space')
                idx = self.combo_key.findText(key_name)
                if idx >= 0:
                    self.combo_key.setCurrentIndex(idx)
            
            # Apply beep settings
            beeps = loaded_properties.get('beeps', {})
            before = beeps.get('before', {})
            after = beeps.get('after', {})
            
            self.chk_beep_before.setChecked(before.get('enabled', False))
            self.spin_beep_before.setValue(before.get('delay_ms', 100))
            
            self.chk_beep_after.setChecked(after.get('enabled', False))
            self.spin_beep_after.setValue(after.get('delay_ms', 100))
        else:
            self.txt_exp_name.setText("My Experiment")
            self.spin_rows.setValue(side)
            self.spin_cols.setValue(side)
        
        # Clear and populate repeat widgets
        while self.repeat_form.count():
            child = self.repeat_form.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        self.repeat_widgets.clear()
        
        for group_name in sorted(audio_groups.keys()):
            spin = QSpinBox()
            spin.setRange(1, 100)
            
            # Apply loaded repetitions if available
            default_value = 1
            if loaded_properties:
                repetitions = loaded_properties.get('repetitions', {})
                default_value = repetitions.get(group_name, 1)
            
            spin.setValue(default_value)
            spin.setFixedWidth(80)
            self.repeat_widgets[group_name] = spin
            self.repeat_form.addRow(f"{group_name}:", spin)

    def export_package(self):
        """Export the experiment as a ZIP package"""
        exp_name = self.txt_exp_name.text().strip()
        if not exp_name:
            QMessageBox.warning(self, "Warning", "Please enter an experiment name.")
            return
            
        # Gather properties
        properties = {
            "experiment_name": exp_name,
            "grid": {
                "rows": self.spin_rows.value(),
                "cols": self.spin_cols.value()
            },
            "order": "random" if self.radio_random.isChecked() else "ordinal",
            "repetitions": {group: spin.value() for group, spin in self.repeat_widgets.items()},
            "proceed_condition": {
                "type": "key" if self.radio_key.isChecked() else "time",
                "key": self.combo_key.currentText(),
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
            }
        }
        
        # Gather words
        words_data = {}
        for group_name, data in self.audio_groups.items():
            words_data[group_name] = []
            for i, (segment, word_text) in enumerate(zip(data['segments'], data['text_words'])):
                words_data[group_name].append({
                    'segment': segment,
                    'word': word_text,
                    'source_file': data['file_path']
                })
        
        # Create package
        save_path, _ = QFileDialog.getSaveFileName(self, "Save Experiment Package", f"{exp_name}.zip", "ZIP Files (*.zip)")
        if not save_path:
            return
            
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            
            # Get processor for audio extraction
            processor = self.parent.new_experiment.processor
            
            # Create temporary directory for audio files
            temp_dir = tempfile.mkdtemp()
            
            # Extract audio segments to temp files
            audio_files_map = {}  # Map segment to filename
            for group_name, data in self.audio_groups.items():
                for i, segment in enumerate(data['segments']):
                    filename = f"{group_name}_word_{i+1:03d}.wav"
                    temp_audio_path = os.path.join(temp_dir, filename)
                    
                    # Extract segment using pydub
                    source_path = segment.get('file_path', data['file_path'])
                    
                    # Convert to wav if needed
                    temp_wav = os.path.join(temp_dir, f"temp_source_{group_name}_{i+1:03d}.wav")
                    processor.convert_to_wav(source_path, temp_wav)
                    
                    sound = AudioSegment.from_wav(temp_wav)
                    audio_segment = sound[segment['start']:segment['end']]
                    audio_segment.export(temp_audio_path, format="wav")
                    
                    audio_files_map[(group_name, i)] = filename
            
            # Update words_data with filenames
            for group_name, words_list in words_data.items():
                for i, word_item in enumerate(words_list):
                    word_item['file'] = audio_files_map[(group_name, i)]
                    del word_item['segment']
                    del word_item['source_file']
            
            # Create temporary JSON file
            json_data = {
                "properties": properties,
                "words": words_data
            }
            
            json_filename = os.path.join(temp_dir, f"{exp_name}.json")
            with open(json_filename, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
                
            # Create ZIP
            with zipfile.ZipFile(save_path, 'w') as zipf:
                # Add JSON
                zipf.write(json_filename, arcname=f"{exp_name}.json")
                
                # Add Audio Files
                for (group_name, idx), filename in audio_files_map.items():
                    src_path = os.path.join(temp_dir, filename)
                    if os.path.exists(src_path):
                        zipf.write(src_path, arcname=f"audio/{filename}")
            
            # Cleanup temp directory
            shutil.rmtree(temp_dir, ignore_errors=True)
            
            QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "Success", f"Experiment package saved to:\n{save_path}")
            
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Error", f"Failed to export package: {e}")
