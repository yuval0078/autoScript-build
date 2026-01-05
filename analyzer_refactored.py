"""
Stroke Analyzer - Pen Data Player for Experiment Results
Loads pen_data.json and animates recorded strokes

Refactored version with:
- Data classes for better organization
- Utility functions to reduce duplication
- Cached computations for performance
- Removed dead code
"""

import sys
import os
import json
import math
import csv
from typing import List, Dict, Tuple, Optional, Any
from dataclasses import dataclass, field

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QSlider, 
                             QFileDialog, QTreeWidget, QTreeWidgetItem, QSplitter, 
                             QMessageBox, QComboBox, QTreeWidgetItemIterator, QInputDialog)
from PyQt5.QtCore import Qt, QTimer, QPointF
from PyQt5.QtGui import QPainter, QPen, QColor, QBrush, QFont

# =============================================================================
# CONSTANTS
# =============================================================================

HEBREW_KEYBOARD_MAP = {
    'q': '/', 'w': "'", 'e': 'ק', 'r': 'ר', 't': 'א', 'y': 'ט', 'u': 'ו', 'i': 'ן', 'o': 'ם', 'p': 'פ',
    'a': 'ש', 's': 'ד', 'd': 'ג', 'f': 'כ', 'g': 'ע', 'h': 'י', 'j': 'ח', 'k': 'ל', 'l': 'ך', ';': 'ף',
    "'": ',', 'z': 'ז', 'x': 'ס', 'c': 'ב', 'v': 'ה', 'b': 'נ', 'n': 'מ', 'm': 'צ', ',': 'ת', '.': 'ץ',
    '/': '.'
}

TRAIN_MODE_MAP = {"Trainable": "trainable", "Low-Quality Trainable": "low-quality", "Untrainable": "untrainable"}
TRAIN_MODE_DISPLAY = {v: k for k, v in TRAIN_MODE_MAP.items()}

HIT_THRESHOLD_PX = 30
LETTER_Y_OFFSET = 40
CANVAS_PADDING = 50

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def format_time(seconds: float) -> str:
    """Format time in MM:SS.mmm format"""
    minutes = int(seconds // 60)
    secs = seconds % 60
    return f"{minutes:02d}:{secs:06.3f}"

def map_to_hebrew(char: str) -> str:
    """Convert English keyboard char to Hebrew"""
    return HEBREW_KEYBOARD_MAP.get(char.lower(), char)

def calculate_bounds(pen_events: List[dict]) -> Tuple[float, float, float, float]:
    """Returns (min_x, min_y, max_x, max_y)"""
    if not pen_events:
        return (0, 0, 0, 0)
    xs = [e["x"] for e in pen_events]
    ys = [e["y"] for e in pen_events]
    return min(xs), min(ys), max(xs), max(ys)

def find_stroke_indices(pen_events: List[dict]) -> Tuple[List[int], List[int]]:
    """Returns (stroke_starts, stroke_ends)"""
    starts = [i for i, e in enumerate(pen_events) if e["type"] == "press"]
    ends = [i for i, e in enumerate(pen_events) if e["type"] == "release"]
    return starts, ends

def get_sorted_letter_indices(assigned_letters: Dict[str, str]) -> List[int]:
    """Get sorted integer indices from assigned_letters dict"""
    if not assigned_letters:
        return []
    return sorted(int(k) for k in assigned_letters.keys())

def build_written_word(assigned_letters: Dict[str, str]) -> str:
    """Build word from assigned letters in order (skipping blockers)"""
    if not assigned_letters:
        return ""
    sorted_indices = get_sorted_letter_indices(assigned_letters)
    # Skip empty values (blockers)
    return ''.join(assigned_letters[str(idx)] for idx in sorted_indices if assigned_letters[str(idx)])

def has_blocker(assigned_letters: Dict[str, str]) -> bool:
    """Check if assigned letters contain any blocker (empty value)"""
    if not assigned_letters:
        return False
    return any(not char for char in assigned_letters.values())

def check_partial_or_full_match(assigned_letters: Dict[str, str], target_word: str) -> bool:
    """Check if assigned letters partially or fully match the target word.
    
    Partial match: assigned letters appear in order in the target word (subsequence),
                   even if there are blockers.
    Full match: assigned letters exactly match the target word.
    
    Args:
        assigned_letters: Dict mapping stroke indices to characters (may include blockers)
        target_word: The original word to match against
    
    Returns:
        True if assigned letters partially or fully match the target word
    """
    if not assigned_letters:
        return False
    
    # Build the written word (skipping blockers)
    written = build_written_word(assigned_letters)
    
    if not written:
        return False
    
    # Full match
    if written == target_word:
        return True
    
    # Partial match: check if written is a subsequence of target_word
    # (all characters appear in order in the target)
    target_idx = 0
    for char in written:
        # Find char in target_word starting from target_idx
        found = False
        while target_idx < len(target_word):
            if target_word[target_idx] == char:
                target_idx += 1
                found = True
                break
            target_idx += 1
        
        if not found:
            return False
    
    return True


def compute_correctness_and_written(assigned_letters: Dict[str, str], original_word: str) -> Tuple[bool, str]:
    """Centralized correctness + written-word computation.
    Returns (is_correct, written_word) applying the rule set:
    - No assigned letters -> correct, written = original
    - Partial/full subsequence match -> correct, written = original
    - Otherwise -> incorrect; written = "" if any blocker else the assigned letters string
    """
    if not assigned_letters:
        return True, original_word

    if check_partial_or_full_match(assigned_letters, original_word):
        return True, original_word

    if has_blocker(assigned_letters):
        return False, ""

    return False, build_written_word(assigned_letters)

def build_letter_segments(assigned_letters: Dict[str, str], num_events: int) -> List[dict]:
    """Build letter segments with first/last event indices"""
    if not assigned_letters:
        return []
    sorted_indices = get_sorted_letter_indices(assigned_letters)
    segments = []
    for i, start_idx in enumerate(sorted_indices):
        end_idx = sorted_indices[i + 1] - 1 if i < len(sorted_indices) - 1 else num_events - 1
        segments.append({
            'char': assigned_letters[str(start_idx)],
            'first_event': start_idx,
            'last_event': end_idx
        })
    return segments

def add_event_ids(pen_events: List[dict]) -> List[dict]:
    """Add event_id to each pen event (starting from 0)"""
    return [{**event, 'event_id': idx} for idx, event in enumerate(pen_events)]

def should_be_low_quality(assigned_letters: Dict[str, str], pen_events: List[dict]) -> bool:
    """Check if word should be marked as low-quality trainable.
    Returns True if:
    - First assigned letter is not on the first stroke, OR
    - Any assigned letter is a blocker (empty value)
    """
    if not assigned_letters or not pen_events:
        return False
    
    # Check for blockers
    if any(not char for char in assigned_letters.values()):
        return True
    
    # Check if first letter starts at first stroke
    sorted_indices = get_sorted_letter_indices(assigned_letters)
    if not sorted_indices:
        return False
    
    first_letter_idx = sorted_indices[0]
    stroke_starts, _ = find_stroke_indices(pen_events)
    
    if stroke_starts and first_letter_idx != stroke_starts[0]:
        return True
    
    return False

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ParticipantData:
    """Encapsulates a participant's data"""
    file_path: str
    participant_number: str
    timestamp: str
    words: List[dict]
    calibration: Any = None
    group: str = None
    age: Any = None
    gender: str = None
    
    # Cached computations per word
    _stroke_cache: Dict[int, Tuple[List[int], List[int]]] = field(default_factory=dict, repr=False)
    _bounds_cache: Dict[int, Tuple[float, float, float, float]] = field(default_factory=dict, repr=False)
    
    @classmethod
    def from_file(cls, file_path: str) -> 'ParticipantData':
        """Load participant data from JSON file"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Handle both old (array) and new (dict with 'words') formats
        if isinstance(data, list):
            return cls(
                file_path=file_path,
                participant_number='Unknown',
                timestamp='Unknown',
                words=data
            )
        else:
            return cls(
                file_path=file_path,
                participant_number=data.get('participant_number', 'Unknown'),
                timestamp=data.get('timestamp', 'Unknown'),
                words=data.get('words', []),
                calibration=data.get('calibration'),
                group=data.get('group'),
                age=data.get('participant_age'),
                gender=data.get('participant_gender')
            )
    
    def get_stroke_indices(self, word_idx: int) -> Tuple[List[int], List[int]]:
        """Get cached stroke start/end indices for a word"""
        if word_idx not in self._stroke_cache:
            pen_events = self.words[word_idx].get('pen_events', [])
            self._stroke_cache[word_idx] = find_stroke_indices(pen_events)
        return self._stroke_cache[word_idx]
    
    def get_bounds(self, word_idx: int) -> Tuple[float, float, float, float]:
        """Get cached bounds for a word"""
        if word_idx not in self._bounds_cache:
            pen_events = self.words[word_idx].get('pen_events', [])
            self._bounds_cache[word_idx] = calculate_bounds(pen_events)
        return self._bounds_cache[word_idx]

# =============================================================================
# CANVAS
# =============================================================================

class AnimationCanvas(QWidget):
    """Canvas for drawing animated pen strokes"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(800, 600)
        self.setStyleSheet("background-color: white;")
        
        self.pen_events: List[dict] = []
        self.current_event_index = 0
        self.word_info: dict = {}
        self.assigned_letters: Dict[str, str] = {}
        
        # Transform state (cached)
        self._transform: Optional[Tuple[float, float, float, float, float, float, float]] = None
        self.parent_player = parent
    
    def load_word_data(self, word_data: dict):
        """Load pen data for a word"""
        self.word_info = {
            "word": word_data.get("word", ""),
            "cell": word_data.get("cell", 0),
            "audio_start_time": word_data.get("audio_start_time"),
            "audio_end_time": word_data.get("audio_end_time")
        }
        self.pen_events = word_data.get("pen_events", [])
        self.assigned_letters = word_data.get("assigned_letters", {})
        self.current_event_index = 0
        self._transform = None  # Invalidate cache
        self.update()
    
    def set_event_index(self, index: int):
        """Set which events to display (0 to index)"""
        self.current_event_index = max(0, min(index, len(self.pen_events)))
        self.update()
    
    def _ensure_transform(self) -> Tuple[float, float, float, float, float, float, float]:
        """Ensure transform is calculated, returns (scale, offset_x, offset_y, min_x, min_y, data_width, data_height)"""
        if self._transform is None and self.pen_events:
            min_x, min_y, max_x, max_y = calculate_bounds(self.pen_events)
            data_width = max_x - min_x
            data_height = max_y - min_y
            
            canvas_width = self.width() - 2 * CANVAS_PADDING
            canvas_height = self.height() - 2 * CANVAS_PADDING - 100
            
            if data_width > 0 and data_height > 0:
                scale = min(canvas_width / data_width, canvas_height / data_height, 1.0)
            else:
                scale = 1.0
            
            self._transform = (scale, CANVAS_PADDING, CANVAS_PADDING, min_x, min_y, data_width, data_height)
        
        return self._transform or (1.0, CANVAS_PADDING, CANVAS_PADDING, 0, 0, 0, 0)
    
    def transform_point(self, x: float, y: float) -> Tuple[float, float]:
        """Transform data coordinates to screen coordinates"""
        scale, offset_x, offset_y, min_x, min_y, _, _ = self._ensure_transform()
        screen_x = (x - min_x) * scale + offset_x
        screen_y = (y - min_y) * scale + offset_y + 50
        return screen_x, screen_y

    def inverse_transform_point(self, screen_x: float, screen_y: float) -> Tuple[float, float]:
        """Transform screen coordinates to data coordinates"""
        scale, offset_x, offset_y, min_x, min_y, _, _ = self._ensure_transform()
        x = (screen_x - offset_x) / scale + min_x if scale > 0 else min_x
        y = (screen_y - offset_y - 50) / scale + min_y if scale > 0 else min_y
        return x, y

    def get_stroke_from_point(self, pos) -> Optional[int]:
        """Find the closest stroke to the given position"""
        if not self.pen_events:
            return None
        
        scale, _, _, _, _, _, _ = self._ensure_transform()
        click_x, click_y = self.inverse_transform_point(pos.x(), pos.y())
        min_dist = float('inf')
        closest_stroke_start = None
        stroke_start_idx = None
        
        for i, event in enumerate(self.pen_events):
            if event["type"] == "press":
                stroke_start_idx = i
            
            if event["type"] in ("press", "move"):
                dist = math.sqrt((click_x - event["x"])**2 + (click_y - event["y"])**2)
                if dist < min_dist:
                    min_dist = dist
                    closest_stroke_start = stroke_start_idx
        
        threshold = 50 / scale if scale > 0 else 50
        return closest_stroke_start if min_dist < threshold else None

    def _get_letter_positions(self) -> List[Tuple[int, float, str]]:
        """Calculate letter positions for hit detection and drawing. Returns [(start_idx, avg_x, char), ...]"""
        if not self.assigned_letters:
            return []
        
        sorted_indices = get_sorted_letter_indices(self.assigned_letters)
        positions = []
        
        for i, start_idx in enumerate(sorted_indices):
            end_idx = sorted_indices[i + 1] if i < len(sorted_indices) - 1 else len(self.pen_events)
            char = self.assigned_letters[str(start_idx)]
            # Display blockers as '|'
            display_char = char if char else '|'
            
            sum_x, count = 0.0, 0
            for j in range(start_idx, end_idx):
                evt = self.pen_events[j]
                if evt["type"] in ("press", "move", "release"):
                    screen_x, _ = self.transform_point(evt["x"], evt["y"])
                    sum_x += screen_x
                    count += 1
            
            if count > 0:
                positions.append((start_idx, sum_x / count, display_char))
        
        return positions

    def mousePressEvent(self, event):
        """Handle mouse press"""
        if event.button() != Qt.LeftButton:
            return
        
        # Check if clicked on an assigned letter
        if self.assigned_letters:
            scale, offset_x, offset_y, _, _, _, data_height = self._ensure_transform()
            drawing_bottom = offset_y + data_height * scale + 50
            letters_y_pos = min(int(drawing_bottom + LETTER_Y_OFFSET), self.height() - 10)
            
            if abs(event.y() - letters_y_pos) < HIT_THRESHOLD_PX:
                for start_idx, avg_x, _ in self._get_letter_positions():
                    if abs(event.x() - avg_x) < HIT_THRESHOLD_PX:
                        self.parent_player.edit_assigned_letter(start_idx)
                        return

        # Check if clicked on a stroke
        stroke_start = self.get_stroke_from_point(event.pos())
        if stroke_start is not None and self.parent_player:
            self.parent_player.event_slider.setValue(stroke_start)
                    
    def mouseDoubleClickEvent(self, event):
        """Handle double click"""
        if event.button() == Qt.LeftButton:
            stroke_start = self.get_stroke_from_point(event.pos())
            if stroke_start is not None and self.parent_player:
                self.parent_player.event_slider.setValue(stroke_start)
                self.parent_player.assign_letter_to_stroke(stroke_start)

    def resizeEvent(self, event):
        """Handle resize - invalidate transform cache"""
        self._transform = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        """Draw the pen strokes up to current event"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), Qt.white)
        
        if not self.pen_events:
            painter.setPen(QPen(Qt.gray))
            painter.setFont(QFont("Arial", 16))
            painter.drawText(self.rect(), Qt.AlignCenter, "No pen data loaded")
            return
        
        # Ensure transform is calculated
        self._transform = None  # Recalculate on paint
        scale, offset_x, offset_y, min_x, min_y, data_width, data_height = self._ensure_transform()
        
        # Draw title
        painter.setPen(QPen(Qt.black))
        painter.setFont(QFont("Arial", 14, QFont.Bold))
        title_text = f"Word: '{self.word_info.get('word', '')}' (Cell {self.word_info.get('cell', 0)})"
        
        audio_start = self.word_info.get('audio_start_time')
        audio_end = self.word_info.get('audio_end_time')
        if audio_start and audio_end:
            duration = audio_end - audio_start
            title_text += f" | Reading: 00:00 - {format_time(duration)}"
        
        painter.drawText(10, 25, title_text)
        
        # Draw strokes
        last_point = None
        for i, event_data in enumerate(self.pen_events):
            screen_x, screen_y = self.transform_point(event_data["x"], event_data["y"])
            point = QPointF(screen_x, screen_y)
            pen_width = max(1, int(event_data["pressure"] * 5))
            color = Qt.black if i <= self.current_event_index else QColor(200, 200, 200)
            
            if event_data["type"] == "press":
                last_point = point
            elif event_data["type"] == "move" and last_point:
                painter.setPen(QPen(color, pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                painter.drawLine(last_point, point)
                last_point = point
            elif event_data["type"] == "release" and last_point:
                painter.setPen(QPen(color, pen_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                painter.drawLine(last_point, point)
                last_point = None
        
        # Draw current position indicator
        if self.current_event_index < len(self.pen_events):
            current = self.pen_events[self.current_event_index]
            screen_x, screen_y = self.transform_point(current["x"], current["y"])
            painter.setPen(QPen(Qt.red, 2))
            painter.setBrush(QBrush(Qt.red))
            painter.drawEllipse(QPointF(screen_x, screen_y), 5, 5)

        # Draw assigned letters
        if self.assigned_letters:
            drawing_bottom = offset_y + data_height * scale + 50
            y_pos = min(int(drawing_bottom + LETTER_Y_OFFSET), self.height() - 10)
            
            painter.setPen(QPen(Qt.blue))
            painter.setFont(QFont("Arial", 24, QFont.Bold))
            
            for _, avg_x, char in self._get_letter_positions():
                painter.drawText(int(avg_x) - 10, y_pos, char)

# =============================================================================
# MAIN WINDOW
# =============================================================================

class PenDataPlayer(QMainWindow):
    """Pen data player with animation control"""
    
    def __init__(self):
        super().__init__()
        # Data
        self.participants: List[ParticipantData] = []
        self.pen_data: List[dict] = []  # Flattened word list
        self.word_to_participant: Dict[int, int] = {}
        
        # State
        self.current_word_index = -1
        self.current_event_index = 0
        self.total_events = 0
        self.is_playing = False
        
        # Annotations (keyed by flattened word index)
        self.word_correctness: Dict[int, bool] = {}
        self.written_words: Dict[int, str] = {}
        self.train_mode: Dict[int, str] = {}
        
        self._init_ui()
    
    def _init_ui(self):
        """Initialize UI"""
        self.setWindowTitle("Stroke Analyzer - Pen Data Player")
        self.setGeometry(100, 100, 1400, 800)
        self.setFocusPolicy(Qt.StrongFocus)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        
        # Left panel
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        
        file_btn = QPushButton("📁 Load Participant Data")
        file_btn.clicked.connect(self.load_data_files)
        left_layout.addWidget(file_btn)
        
        self.loaded_files_label = QLabel("Loaded: 0 files")
        self.loaded_files_label.setStyleSheet("color: gray; font-size: 10px;")
        left_layout.addWidget(self.loaded_files_label)
        
        self.export_btn = QPushButton("📊 Export CSV")
        self.export_btn.clicked.connect(self.export_to_csv)
        self.export_btn.setEnabled(False)
        left_layout.addWidget(self.export_btn)
        
        self.export_json_btn = QPushButton("📦 Export Trainable JSON")
        self.export_json_btn.clicked.connect(self.export_trainable_json)
        self.export_json_btn.setEnabled(False)
        left_layout.addWidget(self.export_json_btn)
        
        self.word_tree = QTreeWidget()
        self.word_tree.setHeaderLabels(["Words by Group"])
        self.word_tree.itemClicked.connect(self.word_selected)
        left_layout.addWidget(self.word_tree)
        
        # Right panel
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        self.canvas = AnimationCanvas(self)
        right_layout.addWidget(self.canvas)
        
        self.event_info_label = QLabel("No data loaded")
        self.event_info_label.setAlignment(Qt.AlignCenter)
        right_layout.addWidget(self.event_info_label)
        
        # Slider
        slider_layout = QHBoxLayout()
        slider_layout.addWidget(QLabel("Event:"))
        self.event_slider = QSlider(Qt.Horizontal)
        self.event_slider.setMinimum(0)
        self.event_slider.setMaximum(0)
        self.event_slider.setInvertedAppearance(True)  # RTL - right is start, left is end
        self.event_slider.valueChanged.connect(self.slider_changed)
        slider_layout.addWidget(self.event_slider)
        right_layout.addLayout(slider_layout)
        
        # Playback controls
        control_layout = QHBoxLayout()
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self.toggle_play)
        self.play_btn.setEnabled(False)
        control_layout.addWidget(self.play_btn)
        
        self.next_event_btn = QPushButton("◀ Event")
        self.next_event_btn.clicked.connect(self.next_event)
        self.next_event_btn.setEnabled(False)
        control_layout.addWidget(self.next_event_btn)
        
        self.prev_event_btn = QPushButton("Event ▶")
        self.prev_event_btn.clicked.connect(self.previous_event)
        self.prev_event_btn.setEnabled(False)
        control_layout.addWidget(self.prev_event_btn)
        right_layout.addLayout(control_layout)
        
        # Slice button (separate row for visibility)
        slice_layout = QHBoxLayout()
        self.slice_btn = QPushButton("✂ Slice Stroke Here")
        self.slice_btn.clicked.connect(self.slice_stroke_at_current)
        self.slice_btn.setEnabled(False)
        self.slice_btn.setToolTip("Slice the current stroke at this event (must be a 'move' event)")
        slice_layout.addWidget(self.slice_btn)
        right_layout.addLayout(slice_layout)
        
        # Navigation controls
        stroke_layout = QHBoxLayout()
        self.next_stroke_btn = QPushButton("⏮ Next Stroke")
        self.next_stroke_btn.clicked.connect(self.next_stroke)
        self.next_stroke_btn.setEnabled(False)
        stroke_layout.addWidget(self.next_stroke_btn)
        
        self.prev_stroke_btn = QPushButton("Previous Stroke ⏭")
        self.prev_stroke_btn.clicked.connect(self.previous_stroke)
        self.prev_stroke_btn.setEnabled(False)
        stroke_layout.addWidget(self.prev_stroke_btn)
        
        self.next_word_btn = QPushButton("⏮⏮ Next Word")
        self.next_word_btn.clicked.connect(self.next_word)
        self.next_word_btn.setEnabled(False)
        stroke_layout.addWidget(self.next_word_btn)
        
        self.prev_word_btn = QPushButton("Previous Word ⏭⏭")
        self.prev_word_btn.clicked.connect(self.previous_word)
        self.prev_word_btn.setEnabled(False)
        stroke_layout.addWidget(self.prev_word_btn)
        right_layout.addLayout(stroke_layout)
        
        # Train mode
        train_mode_layout = QHBoxLayout()
        train_mode_layout.addWidget(QLabel("Train Mode:"))
        self.train_mode_combo = QComboBox()
        self.train_mode_combo.addItems(list(TRAIN_MODE_MAP.keys()))
        self.train_mode_combo.currentTextChanged.connect(self.on_train_mode_changed)
        train_mode_layout.addWidget(self.train_mode_combo)
        right_layout.addLayout(train_mode_layout)
        
        # Splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        main_layout.addWidget(splitter)
        
        # Timer
        self.play_timer = QTimer()
        self.play_timer.timeout.connect(self.next_event)
    
    # -------------------------------------------------------------------------
    # Keyboard & Navigation
    # -------------------------------------------------------------------------
    
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.next_stroke()  # Left = next stroke (RTL)
        elif event.key() == Qt.Key_Right:
            self.previous_stroke()  # Right = previous stroke (RTL)
        elif event.key() == Qt.Key_Space:
            self.toggle_play()
        elif event.key() == Qt.Key_Up:
            self.previous_word()
        elif event.key() == Qt.Key_Down:
            self.next_word()
        elif event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            # If on first event of a stroke, assign letter (like double-click)
            if self.current_word_index >= 0:
                stroke_starts = self._get_current_stroke_starts()
                if self.current_event_index in stroke_starts:
                    self.assign_letter_to_stroke(self.current_event_index)
        event.accept()
    
    def _get_all_word_items(self) -> List[QTreeWidgetItem]:
        """Get all word items from tree (flattened)"""
        items = []
        iterator = QTreeWidgetItemIterator(self.word_tree)
        while iterator.value():
            item = iterator.value()
            if item.data(0, Qt.UserRole) is not None:
                items.append(item)
            iterator += 1
        return items
    
    def _navigate_word(self, direction: int):
        """Navigate to previous (-1) or next (+1) word"""
        all_items = self._get_all_word_items()
        if not all_items:
            return
        
        current_item = self.word_tree.currentItem()
        current_idx = -1
        
        if current_item:
            current_word_idx = current_item.data(0, Qt.UserRole)
            if current_word_idx is not None:
                for i, item in enumerate(all_items):
                    if item.data(0, Qt.UserRole) == current_word_idx:
                        current_idx = i
                        break
        
        if direction < 0:  # Previous
            new_idx = current_idx - 1 if current_idx > 0 else (len(all_items) - 1 if current_idx == -1 else 0)
        else:  # Next
            new_idx = current_idx + 1 if current_idx != -1 and current_idx < len(all_items) - 1 else 0
        
        if 0 <= new_idx < len(all_items):
            new_item = all_items[new_idx]
            self.word_tree.setCurrentItem(new_item)
            self.word_selected(new_item, 0)
    
    def previous_word(self):
        self._navigate_word(-1)
    
    def next_word(self):
        self._navigate_word(1)
    
    def previous_stroke(self):
        stroke_starts = self._get_current_stroke_starts()
        if not stroke_starts:
            return
        
        for i in range(len(stroke_starts) - 1, -1, -1):
            if stroke_starts[i] < self.current_event_index:
                self.event_slider.setValue(stroke_starts[i])
                return
        
        self.event_slider.setValue(stroke_starts[0])
    
    def next_stroke(self):
        stroke_starts = self._get_current_stroke_starts()
        if not stroke_starts:
            return
        
        for start in stroke_starts:
            if start > self.current_event_index:
                self.event_slider.setValue(start)
                return
        
        if self.total_events > 0:
            self.event_slider.setValue(self.total_events - 1)
    
    def previous_event(self):
        if self.current_event_index > 0:
            self.event_slider.setValue(self.current_event_index - 1)
    
    def next_event(self):
        if self.current_event_index < self.total_events - 1:
            self.event_slider.setValue(self.current_event_index + 1)
            
            if self.is_playing and self.current_event_index < self.total_events - 1:
                word_data = self.pen_data[self.current_word_index]
                events = word_data["pen_events"]
                delay_ms = int((events[self.current_event_index + 1]["absolute_time"] - 
                               events[self.current_event_index]["absolute_time"]) * 1000)
                self.play_timer.start(max(1, min(delay_ms, 1000)))
        else:
            self.stop_playback()
    
    # -------------------------------------------------------------------------
    # Playback
    # -------------------------------------------------------------------------
    
    def toggle_play(self):
        if self.is_playing:
            self.stop_playback()
        else:
            self.start_playback()
    
    def start_playback(self):
        self.is_playing = True
        self.play_btn.setText("Pause")
        self.next_event()
    
    def stop_playback(self):
        self.is_playing = False
        self.play_btn.setText("Play")
        self.play_timer.stop()
    
    # -------------------------------------------------------------------------
    # Data Loading
    # -------------------------------------------------------------------------
    
    def load_data_files(self):
        """Load single or multiple data files"""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self, "Select Participant Data File(s)", "", "JSON Files (*.json)")
        
        if not file_paths:
            return
        
        existing_paths = {p.file_path for p in self.participants}
        newly_added = 0
        
        for file_path in file_paths:
            if file_path in existing_paths:
                print(f"Skipped (already loaded): {file_path}")
                continue
            
            try:
                participant = ParticipantData.from_file(file_path)
                self.participants.append(participant)
                newly_added += 1
                print(f"Loaded: {file_path} (Participant {participant.participant_number})")
            except Exception as e:
                print(f"Failed to load {file_path}: {e}")
                QMessageBox.warning(self, "Load Error", f"Failed to load {file_path}:\n{str(e)}")
        
        if self.participants:
            self._rebuild_flattened_data()
            self._populate_tree()
            self._update_loaded_label()
            self.export_btn.setEnabled(True)
            self.export_json_btn.setEnabled(True)
            self.setFocus()
            
            if newly_added > 0:
                QMessageBox.information(self, "Files Loaded", 
                    f"Added {newly_added} file(s).\nTotal: {len(self.participants)} file(s) loaded.")
    
    def _rebuild_flattened_data(self):
        """Rebuild flattened pen_data and mapping"""
        self.pen_data = []
        self.word_to_participant = {}
        
        for p_idx, participant in enumerate(self.participants):
            for word_data in participant.words:
                self.word_to_participant[len(self.pen_data)] = p_idx
                self.pen_data.append(word_data)
    
    def _populate_tree(self):
        """Populate tree widget"""
        self.word_tree.clear()
        flat_idx = 0
        
        for participant in self.participants:
            p_item = QTreeWidgetItem(self.word_tree)
            p_item.setText(0, f"Participant {participant.participant_number} ({len(participant.words)} words)")
            p_item.setExpanded(True)
            
            # Build a mapping of word_data to their flat index
            word_to_flat_idx = {}
            for i, word_data in enumerate(participant.words):
                word_to_flat_idx[id(word_data)] = flat_idx + i
            
            # Group by group name
            groups: Dict[str, List[Tuple[dict, int]]] = {}
            for i, word_data in enumerate(participant.words):
                group = word_data.get("group", "unknown")
                groups.setdefault(group, []).append((word_data, flat_idx + i))
            
            for group_name in sorted(groups.keys()):
                g_item = QTreeWidgetItem(p_item)
                g_item.setText(0, f"  {group_name} ({len(groups[group_name])} words)")
                
                for word_data, word_flat_idx in groups[group_name]:
                    w_item = QTreeWidgetItem(g_item)
                    w_item.setText(0, f"    Cell {word_data.get('cell', 0)}: '{word_data.get('word', '')}' ({len(word_data.get('pen_events', []))} events)")
                    w_item.setData(0, Qt.UserRole, word_flat_idx)
            
            flat_idx += len(participant.words)
    
    def _update_loaded_label(self):
        n = len(self.participants)
        self.loaded_files_label.setText(f"Loaded: {n} files")
        style = "color: green; font-weight: bold; font-size: 10px;" if n else "color: gray; font-size: 10px;"
        self.loaded_files_label.setStyleSheet(style)
    
    # -------------------------------------------------------------------------
    # Word Loading & Display
    # -------------------------------------------------------------------------
    
    def word_selected(self, item, column):
        if item.parent() is not None:
            index = item.data(0, Qt.UserRole)
            if index is not None and 0 <= index < len(self.pen_data):
                self.load_word(index)
    
    def load_word(self, index: int):
        self.stop_playback()
        self.current_word_index = index
        word_data = self.pen_data[index]
        
        self.canvas.load_word_data(word_data)
        
        self.total_events = len(word_data.get("pen_events", []))
        self.current_event_index = 0
        self.event_slider.setMaximum(max(0, self.total_events - 1))
        self.event_slider.setValue(0)
        
        # Enable controls
        for btn in [self.play_btn, self.prev_event_btn, self.next_event_btn, 
                    self.prev_stroke_btn, self.next_stroke_btn, self.slice_btn]:
            btn.setEnabled(True)
        self.prev_word_btn.setEnabled(index > 0)
        self.next_word_btn.setEnabled(index < len(self.pen_data) - 1)
        
        # Auto-populate written word and correctness
        assigned_letters = word_data.get("assigned_letters", {})
        original_word = word_data.get("word", "")
        is_correct, written = compute_correctness_and_written(assigned_letters, original_word)

        if assigned_letters:
            debug_written = build_written_word(assigned_letters)
            print(f"load_word({index}): '{original_word}', assigned={assigned_letters}, written='{debug_written}'")
            print(f"  → computed: is_correct={is_correct}, written_word='{written}'")
        else:
            print(f"load_word({index}): '{original_word}', no assigned letters → is_correct=True")

        self.word_correctness[index] = is_correct
        self.written_words[index] = written
        
        # Update train mode
        mode = self.train_mode.get(index, "trainable")
        self.train_mode_combo.setCurrentText(TRAIN_MODE_DISPLAY.get(mode, "Trainable"))
        
        self.update_info()
        self.activateWindow()
        self.setFocus()
        print(f"Loaded word {index + 1}/{len(self.pen_data)}: '{word_data.get('word', '')}'")
    
    def _get_current_stroke_starts(self) -> List[int]:
        if self.current_word_index < 0 or self.current_word_index >= len(self.pen_data):
            return []
        pen_events = self.pen_data[self.current_word_index].get("pen_events", [])
        return find_stroke_indices(pen_events)[0]
    
    def slider_changed(self, value: int):
        self.current_event_index = value
        self.canvas.set_event_index(value)
        self.update_info()
    
    def update_info(self):
        if self.total_events == 0 or self.current_event_index >= self.total_events:
            self.event_info_label.setText("No events")
            return
        
        word_data = self.pen_data[self.current_word_index]
        event = word_data["pen_events"][self.current_event_index]
        
        audio_start = word_data.get("audio_start_time") or event["absolute_time"]
        event_time = event["absolute_time"] - audio_start
        
        info = (f"Event: {self.current_event_index + 1}/{self.total_events} | "
                f"Time: {format_time(event_time)} | Type: {event['type']} | "
                f"Pos: ({event['x']:.1f}, {event['y']:.1f}) | "
                f"Pressure: {event['pressure']:.2f} | Speed: {event['speed']:.1f} px/s")
        
        if audio_end := word_data.get("audio_end_time"):
            info += f" | Reading: {format_time(audio_end - audio_start)}"
        
        self.event_info_label.setText(info)
    
    def on_train_mode_changed(self, text: str):
        if self.current_word_index >= 0:
            self.train_mode[self.current_word_index] = TRAIN_MODE_MAP.get(text, "trainable")
    
    # -------------------------------------------------------------------------
    # Letter Assignment
    # -------------------------------------------------------------------------
    
    def edit_assigned_letter(self, stroke_start_idx: int):
        if self.current_word_index < 0:
            return
        
        word_data = self.pen_data[self.current_word_index]
        assigned = word_data.get("assigned_letters", {})
        current_char = assigned.get(str(stroke_start_idx))
        # Check if this key exists (even if empty/blocker)
        if str(stroke_start_idx) not in assigned:
            return
        
        display_char = current_char if current_char else "(blocker)"
        
        msg = QMessageBox(self)
        msg.setWindowTitle("Edit Letter")
        msg.setText(f"Selected letter: {display_char}")
        msg.setInformativeText("Press 'Delete' to remove, or 'Replace' to change.")
        
        delete_btn = msg.addButton("Delete", QMessageBox.DestructiveRole)
        replace_btn = msg.addButton("Replace", QMessageBox.ActionRole)
        msg.addButton(QMessageBox.Cancel)
        msg.exec_()
        
        if msg.clickedButton() == delete_btn:
            del assigned[str(stroke_start_idx)]
            self.canvas.assigned_letters = assigned
            self._update_train_mode_for_letters()
            self.canvas.update()
            print(f"Deleted assignment at event {stroke_start_idx}")
        elif msg.clickedButton() == replace_btn:
            self.assign_letter_to_stroke(stroke_start_idx)
        
        self.activateWindow()
        self.setFocus()

    def assign_letter_to_stroke(self, stroke_start_idx: int):
        if self.current_word_index < 0:
            return
        
        text, ok = QInputDialog.getText(self, "Assign Letter", 
                                         "Enter a single character:\n(leave empty for blocker)")
        if ok:  # Accept even empty text for blockers
            # Empty text = blocker, otherwise convert to Hebrew
            hebrew_char = "" if not text else map_to_hebrew(text[0])
            
            word_data = self.pen_data[self.current_word_index]
            if "assigned_letters" not in word_data:
                word_data["assigned_letters"] = {}
            
            word_data["assigned_letters"][str(stroke_start_idx)] = hebrew_char
            self.canvas.assigned_letters = word_data["assigned_letters"]
            self._update_train_mode_for_letters()
            # Recompute correctness/written now that letters changed
            original_word = word_data.get("word", "")
            is_correct, written = compute_correctness_and_written(word_data["assigned_letters"], original_word)
            self.word_correctness[self.current_word_index] = is_correct
            self.written_words[self.current_word_index] = written
            print(f"  → recomputed: is_correct={is_correct}, written_word='{written}'")
            self.canvas.update()
            display = hebrew_char if hebrew_char else "(blocker)"
            print(f"Assigned '{display}' to event {stroke_start_idx}")
        
        self.activateWindow()
        self.setFocus()
    
    def _update_train_mode_for_letters(self):
        """Auto-update train mode to low-quality if conditions are met"""
        if self.current_word_index < 0:
            return
        
        word_data = self.pen_data[self.current_word_index]
        assigned = word_data.get("assigned_letters", {})
        pen_events = word_data.get("pen_events", [])
        
        if should_be_low_quality(assigned, pen_events):
            self.train_mode[self.current_word_index] = "low-quality"
            self.train_mode_combo.blockSignals(True)
            self.train_mode_combo.setCurrentText("Low-Quality Trainable")
            self.train_mode_combo.blockSignals(False)
    
    def slice_stroke_at_current(self):
        """Slice the current stroke at the current event position"""
        if self.current_word_index < 0:
            return
        
        word_data = self.pen_data[self.current_word_index]
        pen_events = word_data.get("pen_events", [])
        
        if not pen_events or self.current_event_index <= 0:
            QMessageBox.warning(self, "Cannot Slice", "No valid event selected.")
            return
        
        current_event = pen_events[self.current_event_index]
        
        # Can only slice on "move" events (not press/release)
        if current_event["type"] != "move":
            QMessageBox.warning(self, "Cannot Slice", 
                              "Can only slice at 'move' events (not press/release).")
            return
        
        # Check we're not at first or last event of stroke
        stroke_starts, stroke_ends = find_stroke_indices(pen_events)
        
        # Find which stroke we're in
        current_stroke_idx = None
        for i, start in enumerate(stroke_starts):
            end = stroke_ends[i] if i < len(stroke_ends) else len(pen_events) - 1
            if start <= self.current_event_index <= end:
                current_stroke_idx = i
                break
        
        if current_stroke_idx is None:
            QMessageBox.warning(self, "Cannot Slice", "Could not find current stroke.")
            return
        
        stroke_start = stroke_starts[current_stroke_idx]
        stroke_end = stroke_ends[current_stroke_idx] if current_stroke_idx < len(stroke_ends) else len(pen_events) - 1
        
        # Cannot slice at first or last event of stroke
        if self.current_event_index == stroke_start or self.current_event_index == stroke_end:
            QMessageBox.warning(self, "Cannot Slice", 
                              "Cannot slice at the first or last event of a stroke.")
            return
        
        # Insert release at current position, then press at next position
        # Create new release event (copy current with type=release)
        slice_event = self.current_event_index
        current_data = pen_events[slice_event]
        
        # Insert a release event after current, and a press event after that
        release_event = dict(current_data)
        release_event["type"] = "release"
        
        press_event = dict(current_data)
        press_event["type"] = "press"
        
        # Insert: after current move, add release, then press
        pen_events.insert(slice_event + 1, release_event)
        pen_events.insert(slice_event + 2, press_event)
        
        # Update assigned_letters indices that are after the slice point
        assigned = word_data.get("assigned_letters", {})
        if assigned:
            new_assigned = {}
            for idx_str, char in assigned.items():
                idx = int(idx_str)
                if idx > slice_event:
                    new_assigned[str(idx + 2)] = char  # Shift by 2 (release + press)
                else:
                    new_assigned[idx_str] = char
            word_data["assigned_letters"] = new_assigned
        
        # Reload the word to refresh everything
        self.load_word(self.current_word_index)
        self.event_slider.setValue(slice_event + 2)  # Move to new press event
        
        QMessageBox.information(self, "Stroke Sliced", 
                               f"Stroke sliced at event {slice_event}. You can now assign letters to each part.")

    # -------------------------------------------------------------------------
    # Export
    # -------------------------------------------------------------------------
    
    def _get_data_source(self) -> List[ParticipantData]:
        """Get participants for export"""
        return self.participants
    
    def export_to_csv(self):
        if not self.participants and not self.pen_data:
            QMessageBox.warning(self, "No Data", "No data loaded to export.")
            return
        
        default_name = "combined_analysis.csv" if len(self.participants) > 1 else f"participant_analysis.csv"
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Analysis Data", default_name, "CSV Files (*.csv)")
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'w', newline='', encoding='utf-8-sig') as csvfile:
                writer = csv.writer(csvfile)
                
                header = [
                    'Exp Step', 'Participant', 'Age', 'Gender', 'Word', 'Correct', 'Written Word',
                    'Reading End', 'Writing Start', 'Writing End', 'Strokes', 'Avg Interval (ms)',
                    'Letters'
                ]
                writer.writerow(header)
                
                flat_idx = 0
                total_words = 0
                
                for participant in self.participants:
                    for word_idx, word_data in enumerate(participant.words):
                        pen_events = word_data.get("pen_events", [])
                        if not pen_events:
                            flat_idx += 1
                            continue
                        
                        total_words += 1
                        audio_start = word_data.get("audio_start_time") or pen_events[0]["absolute_time"]
                        audio_end = word_data.get("audio_end_time")
                        
                        stroke_starts, stroke_ends = find_stroke_indices(pen_events)
                        
                        # First stroke time as reference for Letters column
                        first_stroke_time = pen_events[stroke_starts[0]]["absolute_time"] if stroke_starts else audio_start
                        
                        writing_start = first_stroke_time - audio_start if stroke_starts else 0
                        writing_end = pen_events[stroke_ends[-1]]["absolute_time"] - audio_start if stroke_ends else writing_start
                        reading_end = (audio_end - audio_start) if audio_end else 0
                        
                        # Avg interval
                        if len(stroke_starts) > 1:
                            intervals = [pen_events[stroke_starts[i]]["absolute_time"] - pen_events[stroke_starts[i-1]]["absolute_time"]
                                        for i in range(1, len(stroke_starts))]
                            avg_interval = sum(intervals) / len(intervals) * 1000
                        else:
                            avg_interval = 0
                        
                        # Build Letters column: each letter with start/end time relative to audio_start (Reading Start)
                        assigned_letters = word_data.get("assigned_letters", {})
                        letters_parts = []
                        if assigned_letters:
                            sorted_indices = get_sorted_letter_indices(assigned_letters)
                            # Last letter ends at last release event (same as Writing End)
                            last_event_idx = stroke_ends[-1] if stroke_ends else len(pen_events) - 1
                            for i, start_idx in enumerate(sorted_indices):
                                char = assigned_letters[str(start_idx)]
                                # End index: next letter's start - 1, or last release event
                                end_idx = sorted_indices[i + 1] - 1 if i < len(sorted_indices) - 1 else last_event_idx
                                
                                # Times in ms relative to audio_start (Reading Start)
                                start_time_ms = int((pen_events[start_idx]["absolute_time"] - audio_start) * 1000)
                                end_time_ms = int((pen_events[end_idx]["absolute_time"] - audio_start) * 1000)
                                
                                letters_parts.append(f"{char} {start_time_ms}/{end_time_ms}")
                        
                        letters_str = ", ".join(letters_parts)
                        
                        # Calculate is_correct and written_word for this word
                        original_word = word_data.get("word", "")
                        
                        # Debug: print assigned letters for this word
                        if assigned_letters:
                            written = build_written_word(assigned_letters)
                            print(f"DEBUG word {word_idx + 1}: '{original_word}', assigned: {assigned_letters}, written: '{written}'")
                        
                        # Check if cached in dictionaries (from UI)
                        if flat_idx in self.word_correctness and flat_idx in self.written_words:
                            is_correct = self.word_correctness[flat_idx]
                            written_word = self.written_words[flat_idx]
                            print(f"  Using cached: is_correct={is_correct}, written_word='{written_word}'")
                        else:
                            # Calculate based on logic
                            if not assigned_letters:
                                is_correct = True
                                written_word = original_word
                            elif check_partial_or_full_match(assigned_letters, original_word):
                                is_correct = True
                                written_word = original_word
                                print(f"  Calculated: MATCH -> is_correct=True")
                            else:
                                is_correct = False
                                if has_blocker(assigned_letters):
                                    written_word = ""
                                    print(f"  Calculated: NO MATCH + BLOCKER -> is_correct=False, written_word=''")
                                else:
                                    written_word = build_written_word(assigned_letters)
                                    print(f"  Calculated: NO MATCH + NO BLOCKER -> is_correct=False, written_word='{written_word}'")
                        
                        row = [
                            word_idx + 1,
                            participant.participant_number,
                            participant.age if participant.age else '',
                            participant.gender if participant.gender else '',
                            original_word,
                            "Yes" if is_correct else "No",
                            written_word,
                            format_time(reading_end),
                            format_time(writing_start),
                            format_time(writing_end),
                            len(stroke_starts),
                            f"{avg_interval:.1f}",
                            letters_str
                        ]
                        
                        writer.writerow(row)
                        flat_idx += 1
            
            QMessageBox.information(self, "Export Complete", f"Exported {total_words} words to:\n{file_path}")
            print(f"✓ Exported CSV: {file_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export:\n{str(e)}")
    
    def export_trainable_json(self):
        if not self.participants and not self.pen_data:
            QMessageBox.warning(self, "No Data", "No data loaded to export.")
            return
        
        default_name = "combined_trainable_data.json" if len(self.participants) > 1 else "trainable_data.json"
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Trainable JSON", default_name, "JSON Files (*.json)")
        
        if not file_path:
            return
        
        try:
            flat_idx = 0
            output = []
            
            for participant in self.participants:
                p_output = {
                    'participant_number': participant.participant_number,
                    'participant_age': participant.age,
                    'participant_gender': participant.gender,
                    'timestamp': participant.timestamp,
                    'calibration': participant.calibration,
                    'group': participant.group,
                    'words': []
                }
                
                for word_data in participant.words:
                    # Calculate written_word based on the new logic
                    assigned_letters = word_data.get("assigned_letters", {})
                    original_word = word_data.get("word", "")
                    
                    # Check if we have cached values
                    written = self.written_words.get(flat_idx)
                    is_correct = self.word_correctness.get(flat_idx)
                    
                    # If not cached, calculate based on logic
                    if written is None or is_correct is None:
                        if not assigned_letters:
                            is_correct = True
                            written = original_word
                        elif check_partial_or_full_match(assigned_letters, original_word):
                            is_correct = True
                            written = original_word
                        else:
                            is_correct = False
                            if has_blocker(assigned_letters):
                                written = ""
                            else:
                                written = build_written_word(assigned_letters)
                    
                    trainability = self.train_mode.get(flat_idx, "trainable")
                    assigned = word_data.get("assigned_letters", {})
                    pen_events = word_data.get('pen_events', [])
                    
                    word_entry = {
                        'written_word': written,
                        'trainability': trainability,
                        'pen_events': add_event_ids(pen_events),
                        'audio_start_time': word_data.get('audio_start_time'),
                        'audio_end_time': word_data.get('audio_end_time'),
                        'letter_segments': build_letter_segments(assigned, len(pen_events))
                    }
                    
                    p_output['words'].append(word_entry)
                    flat_idx += 1
                
                output.append(p_output)
            
            # Single participant: unwrap array
            final_output = output[0] if len(output) == 1 else output
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(final_output, f, ensure_ascii=False, indent=2)
            
            total_words = sum(len(p.words) for p in self.participants)
            QMessageBox.information(self, "Export Complete", f"Exported {total_words} words to:\n{file_path}")
            print(f"✓ Exported trainable JSON: {file_path}")
            
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export:\n{str(e)}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    from qt_bootstrap import ensure_qt_platform_plugin_path
    ensure_qt_platform_plugin_path()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    player = PenDataPlayer()
    player.show()
    print("Stroke Analyzer - Pen Data Player (Refactored)")
    print("Left/Right: Word Nav | Up/Down: Word | Space: Play/Pause")
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
