"""
Stroke Analyzer - Pen Data Player for Experiment Results
Loads pen_data.json and animates recorded strokes

Refactored version with:
- Data classes for better organization
- Utility functions to reduce duplication
- Cached computations for performance
- Letter objects with stroke grouping
- Stroke selection with group mode
"""

import sys
import os
import json
import math
import csv
from typing import List, Dict, Tuple, Optional, Any, Set
from dataclasses import dataclass, field

from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QSlider, 
                             QFileDialog, QTreeWidget, QTreeWidgetItem, QSplitter, 
                             QMessageBox, QComboBox, QTreeWidgetItemIterator, QInputDialog,
                             QDialog, QLineEdit, QDialogButtonBox, QFormLayout)
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

# Selection colors
STROKE_SELECTED_COLOR = QColor(0, 120, 215)  # Blue for selected strokes
STROKE_NORMAL_COLOR = Qt.black
STROKE_FUTURE_COLOR = QColor(200, 200, 200)  # Gray for future strokes
LETTER_COLOR = QColor(0, 100, 0)  # Dark green for letter labels

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

def downsample_stroke_events(events: List[dict], target_interval_ms: float = 25, min_distance_px: float = 3) -> List[dict]:
    """Downsample pen events for ML training - reduces data size while preserving stroke shape.
    
    Args:
        events: List of pen events for a single stroke
        target_interval_ms: Minimum time between kept events in milliseconds (default: 25ms = 40Hz)
        min_distance_px: Minimum distance in pixels between kept points (default: 3px)
    
    Returns:
        Filtered list of events with press/release always kept
    """
    if not events or len(events) <= 2:
        return events
    
    filtered = []
    last_kept_event = None
    last_kept_time = None
    
    for i, event in enumerate(events):
        # Always keep press and release events
        if event["type"] in ("press", "release"):
            filtered.append(event)
            last_kept_event = event
            last_kept_time = event.get("absolute_time", 0)
            continue
        
        # For move events, apply filtering
        if last_kept_event is None:
            filtered.append(event)
            last_kept_event = event
            last_kept_time = event.get("absolute_time", 0)
            continue
        
        # Check time threshold
        current_time = event.get("absolute_time", 0)
        time_delta_ms = (current_time - last_kept_time) * 1000 if last_kept_time else 0
        
        # Check distance threshold
        dx = event["x"] - last_kept_event["x"]
        dy = event["y"] - last_kept_event["y"]
        distance = (dx**2 + dy**2)**0.5
        
        # Keep if either threshold exceeded
        if time_delta_ms >= target_interval_ms or distance >= min_distance_px:
            filtered.append(event)
            last_kept_event = event
            last_kept_time = current_time
    
    return filtered

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
# LETTER OBJECT
# =============================================================================

@dataclass
class LetterObject:
    """Represents an assigned letter with its associated strokes"""
    char: str  # The Hebrew character (empty string = blocker)
    stroke_ids: List[int]  # List of stroke indices that belong to this letter
    
    def to_dict(self) -> dict:
        return {
            'char': self.char,
            'stroke_ids': self.stroke_ids
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'LetterObject':
        return cls(
            char=data.get('char', ''),
            stroke_ids=data.get('stroke_ids', [])
        )

def get_stroke_id_for_event(event_idx: int, stroke_starts: List[int]) -> int:
    """Get the stroke ID (index) for a given event index"""
    for i in range(len(stroke_starts) - 1, -1, -1):
        if event_idx >= stroke_starts[i]:
            return i
    return 0

def letters_to_assigned_letters(letters: List[LetterObject], stroke_starts: List[int]) -> Dict[str, str]:
    """Convert letter objects to legacy assigned_letters format for backward compatibility"""
    assigned = {}
    for letter in letters:
        if letter.stroke_ids:
            # Use the first stroke's start event as the key
            first_stroke_idx = min(letter.stroke_ids)
            if first_stroke_idx < len(stroke_starts):
                event_idx = stroke_starts[first_stroke_idx]
                assigned[str(event_idx)] = letter.char
    return assigned

def assigned_letters_to_letters(assigned_letters: Dict[str, str], stroke_starts: List[int]) -> List[LetterObject]:
    """Convert legacy assigned_letters to letter objects"""
    if not assigned_letters:
        return []
    
    letters = []
    sorted_event_indices = sorted(int(k) for k in assigned_letters.keys())
    
    for event_idx in sorted_event_indices:
        char = assigned_letters[str(event_idx)]
        stroke_id = get_stroke_id_for_event(event_idx, stroke_starts)
        letters.append(LetterObject(char=char, stroke_ids=[stroke_id]))
    
    return letters

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
    """Canvas for drawing animated pen strokes with selection support"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(800, 600)
        self.setStyleSheet("background-color: white;")
        
        self.pen_events: List[dict] = []
        self.current_event_index = 0
        self.word_info: dict = {}
        
        # Letter objects (new system)
        self.letters: List[LetterObject] = []
        
        # Stroke selection state
        self.selected_strokes: Set[int] = set()  # Set of selected stroke indices
        self.stroke_starts: List[int] = []
        self.stroke_ends: List[int] = []
        
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
        
        # Calculate stroke indices
        self.stroke_starts, self.stroke_ends = find_stroke_indices(self.pen_events)
        
        # Load letters (new format) or convert from legacy assigned_letters
        if "letters" in word_data:
            self.letters = [LetterObject.from_dict(l) for l in word_data["letters"]]
        elif "assigned_letters" in word_data:
            self.letters = assigned_letters_to_letters(
                word_data["assigned_letters"], 
                self.stroke_starts
            )
        else:
            self.letters = []
        
        self.current_event_index = 0
        self._transform = None  # Invalidate cache
        
        # Select first stroke by default
        self.selected_strokes = {0} if self.stroke_starts else set()
        
        self.update()
    
    def set_event_index(self, index: int):
        """Set which events to display (0 to index)"""
        self.current_event_index = max(0, min(index, len(self.pen_events)))
        self.update()
    
    def get_selected_strokes(self) -> List[int]:
        """Get list of selected stroke indices in order"""
        return sorted(self.selected_strokes)
    
    def select_stroke(self, stroke_idx: int, add_to_selection: bool = False):
        """Select a stroke, optionally adding to current selection"""
        if stroke_idx < 0 or stroke_idx >= len(self.stroke_starts):
            return
        
        if add_to_selection:
            if stroke_idx in self.selected_strokes:
                # Clicking selected stroke in group mode: deselect unless it's the only one
                if len(self.selected_strokes) > 1:
                    self.selected_strokes.remove(stroke_idx)
            else:
                self.selected_strokes.add(stroke_idx)
        else:
            # Single selection mode
            self.selected_strokes = {stroke_idx}
        
        # Update event index to first event of first selected stroke
        if self.selected_strokes:
            first_selected = min(self.selected_strokes)
            self.current_event_index = self.stroke_starts[first_selected]
            if self.parent_player:
                self.parent_player.event_slider.blockSignals(True)
                self.parent_player.event_slider.setValue(self.current_event_index)
                self.parent_player.event_slider.blockSignals(False)
                self.parent_player.update_info()
        
        self.update()
    
    def select_next_stroke(self, add_to_selection: bool = False):
        """Select the next stroke (to the left in RTL)"""
        if not self.stroke_starts:
            return
        
        current_max = max(self.selected_strokes) if self.selected_strokes else -1
        next_stroke = current_max + 1
        
        if next_stroke < len(self.stroke_starts):
            self.select_stroke(next_stroke, add_to_selection)
    
    def select_prev_stroke(self, add_to_selection: bool = False):
        """Select the previous stroke (to the right in RTL)"""
        if not self.stroke_starts:
            return
        
        current_min = min(self.selected_strokes) if self.selected_strokes else len(self.stroke_starts)
        prev_stroke = current_min - 1
        
        if prev_stroke >= 0:
            self.select_stroke(prev_stroke, add_to_selection)
    
    def get_stroke_for_event(self, event_idx: int) -> int:
        """Get stroke index for an event index"""
        for i in range(len(self.stroke_starts) - 1, -1, -1):
            if event_idx >= self.stroke_starts[i]:
                return i
        return 0
    
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
        """Find the stroke index closest to the given position"""
        if not self.pen_events:
            return None
        
        scale, _, _, _, _, _, _ = self._ensure_transform()
        click_x, click_y = self.inverse_transform_point(pos.x(), pos.y())
        min_dist = float('inf')
        closest_stroke_idx = None
        current_stroke_idx = 0
        
        for i, event in enumerate(self.pen_events):
            if event["type"] == "press":
                current_stroke_idx = self.get_stroke_for_event(i)
            
            if event["type"] in ("press", "move"):
                dist = math.sqrt((click_x - event["x"])**2 + (click_y - event["y"])**2)
                if dist < min_dist:
                    min_dist = dist
                    closest_stroke_idx = current_stroke_idx
        
        threshold = 50 / scale if scale > 0 else 50
        return closest_stroke_idx if min_dist < threshold else None

    def get_letter_at_position(self, pos) -> Optional[int]:
        """Find letter index at the given screen position (for the letter labels below canvas)"""
        if not self.letters:
            return None
        
        scale, offset_x, offset_y, _, _, _, data_height = self._ensure_transform()
        drawing_bottom = offset_y + data_height * scale + 50
        letters_y_pos = min(int(drawing_bottom + LETTER_Y_OFFSET), self.height() - 10)
        
        # Check if click is near the letters row
        if abs(pos.y() - letters_y_pos) > HIT_THRESHOLD_PX:
            return None
        
        # Calculate average x position for each letter
        for idx, letter in enumerate(self.letters):
            if not letter.stroke_ids:
                continue
            
            # Get events for this letter's strokes
            sum_x, count = 0.0, 0
            for stroke_id in letter.stroke_ids:
                if stroke_id < len(self.stroke_starts):
                    start_evt = self.stroke_starts[stroke_id]
                    end_evt = self.stroke_ends[stroke_id] if stroke_id < len(self.stroke_ends) else len(self.pen_events) - 1
                    
                    for j in range(start_evt, end_evt + 1):
                        if j < len(self.pen_events):
                            evt = self.pen_events[j]
                            if evt["type"] in ("press", "move", "release"):
                                screen_x, _ = self.transform_point(evt["x"], evt["y"])
                                sum_x += screen_x
                                count += 1
            
            if count > 0:
                avg_x = sum_x / count
                if abs(pos.x() - avg_x) < HIT_THRESHOLD_PX:
                    return idx
        
        return None

    def mousePressEvent(self, event):
        """Handle mouse press - select stroke or edit letter"""
        if event.button() != Qt.LeftButton:
            return
        
        # Check if clicked on a letter label
        letter_idx = self.get_letter_at_position(event.pos())
        if letter_idx is not None:
            # Single click on letter - select its strokes
            letter = self.letters[letter_idx]
            self.selected_strokes = set(letter.stroke_ids)
            if self.selected_strokes and self.parent_player:
                first_stroke = min(self.selected_strokes)
                self.current_event_index = self.stroke_starts[first_stroke]
                self.parent_player.event_slider.blockSignals(True)
                self.parent_player.event_slider.setValue(self.current_event_index)
                self.parent_player.event_slider.blockSignals(False)
                self.parent_player.update_info()
            self.update()
            return
        
        # Check if clicked on a stroke
        stroke_idx = self.get_stroke_from_point(event.pos())
        if stroke_idx is not None:
            # Check modifiers for group selection
            add_to_selection = (event.modifiers() & Qt.ShiftModifier) or \
                              (self.parent_player and self.parent_player.group_select_mode)
            self.select_stroke(stroke_idx, add_to_selection)
                    
    def mouseDoubleClickEvent(self, event):
        """Handle double click - edit letter assignment"""
        if event.button() != Qt.LeftButton:
            return
        
        # Check if double-clicked on a letter label
        letter_idx = self.get_letter_at_position(event.pos())
        if letter_idx is not None and self.parent_player:
            self.parent_player.edit_letter(letter_idx)
            return
        
        # Double-click on stroke - no longer assigns letter (use button instead)

    def resizeEvent(self, event):
        """Handle resize - invalidate transform cache"""
        self._transform = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        """Draw the pen strokes up to current event, with selected strokes highlighted"""
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
        
        # Add stroke selection info
        if self.selected_strokes:
            title_text += f" | Selected: {sorted(self.selected_strokes)}"
        
        painter.drawText(10, 25, title_text)
        
        # Draw strokes with selection highlighting
        last_point = None
        current_stroke_idx = 0
        
        for i, event_data in enumerate(self.pen_events):
            screen_x, screen_y = self.transform_point(event_data["x"], event_data["y"])
            point = QPointF(screen_x, screen_y)
            pen_width = max(1, int(event_data["pressure"] * 5))
            
            # Track current stroke
            if event_data["type"] == "press":
                current_stroke_idx = self.get_stroke_for_event(i)
            
            # Determine color based on selection and timeline
            is_selected = current_stroke_idx in self.selected_strokes
            is_past = i <= self.current_event_index
            
            if is_selected:
                color = STROKE_SELECTED_COLOR
                pen_width = max(2, pen_width + 1)  # Make selected strokes slightly thicker
            elif is_past:
                color = Qt.black
            else:
                color = STROKE_FUTURE_COLOR
            
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

        # Draw letter assignments
        if self.letters:
            drawing_bottom = offset_y + data_height * scale + 50
            y_pos = min(int(drawing_bottom + LETTER_Y_OFFSET), self.height() - 10)
            
            painter.setFont(QFont("Arial", 24, QFont.Bold))
            
            # First pass: calculate average x positions for all letters
            letter_positions = []  # List of (avg_x, letter, is_selected)
            for letter in self.letters:
                if not letter.stroke_ids:
                    continue
                
                # Calculate average x position for this letter's strokes
                sum_x, count = 0.0, 0
                for stroke_id in letter.stroke_ids:
                    if stroke_id < len(self.stroke_starts):
                        start_evt = self.stroke_starts[stroke_id]
                        end_evt = self.stroke_ends[stroke_id] if stroke_id < len(self.stroke_ends) else len(self.pen_events) - 1
                        
                        for j in range(start_evt, end_evt + 1):
                            if j < len(self.pen_events):
                                evt = self.pen_events[j]
                                if evt["type"] in ("press", "move", "release"):
                                    screen_x, _ = self.transform_point(evt["x"], evt["y"])
                                    sum_x += screen_x
                                    count += 1
                
                if count > 0:
                    avg_x = sum_x / count
                    is_selected = bool(self.selected_strokes & set(letter.stroke_ids))
                    letter_positions.append((avg_x, letter, is_selected))
            
            # Second pass: adjust positions to avoid overlap
            min_spacing = 25  # Minimum pixels between letter centers
            adjusted_positions = []
            for i, (x, letter, is_selected) in enumerate(letter_positions):
                adjusted_x = x
                # Check against all previously placed letters
                for prev_x, _, _ in adjusted_positions:
                    if abs(adjusted_x - prev_x) < min_spacing:
                        # Push this letter to the right if too close
                        if adjusted_x >= prev_x:
                            adjusted_x = prev_x + min_spacing
                        else:
                            adjusted_x = prev_x - min_spacing
                adjusted_positions.append((adjusted_x, letter, is_selected))
            
            # Third pass: draw letters at adjusted positions
            for adjusted_x, letter, is_selected in adjusted_positions:
                if is_selected:
                    painter.setPen(QPen(STROKE_SELECTED_COLOR))
                else:
                    painter.setPen(QPen(LETTER_COLOR))
                
                display_char = letter.char if letter.char else '|'
                painter.drawText(int(adjusted_x) - 10, y_pos, display_char)

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
        
        # Selection mode
        self.group_select_mode = False
        
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
        
        # Selection controls row
        selection_layout = QHBoxLayout()
        
        self.group_select_btn = QPushButton("📦 Group Select")
        self.group_select_btn.setCheckable(True)
        self.group_select_btn.setChecked(False)
        self.group_select_btn.toggled.connect(self.toggle_group_select)
        self.group_select_btn.setToolTip("Toggle group selection mode (or hold Shift)")
        selection_layout.addWidget(self.group_select_btn)
        
        self.assign_letter_btn = QPushButton("🔤 Assign Letter")
        self.assign_letter_btn.clicked.connect(self.assign_letter_to_selection)
        self.assign_letter_btn.setEnabled(False)
        self.assign_letter_btn.setToolTip("Assign a letter to selected stroke(s)")
        selection_layout.addWidget(self.assign_letter_btn)
        
        self.clear_selection_btn = QPushButton("✖ Clear Selection")
        self.clear_selection_btn.clicked.connect(self.clear_stroke_selection)
        self.clear_selection_btn.setEnabled(False)
        selection_layout.addWidget(self.clear_selection_btn)
        
        right_layout.addLayout(selection_layout)
        
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
        # Check if shift is held for group selection
        add_to_selection = bool(event.modifiers() & Qt.ShiftModifier) or self.group_select_mode
        
        if event.key() == Qt.Key_Left:
            # Left = next stroke (RTL)
            self.canvas.select_next_stroke(add_to_selection)
            self._update_selection_ui()
        elif event.key() == Qt.Key_Right:
            # Right = previous stroke (RTL)
            self.canvas.select_prev_stroke(add_to_selection)
            self._update_selection_ui()
        elif event.key() == Qt.Key_Space:
            self.toggle_play()
        elif event.key() == Qt.Key_Up:
            self.previous_word()
        elif event.key() == Qt.Key_Down:
            self.next_word()
        elif event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter:
            # Assign letter to selected strokes
            if self.canvas.selected_strokes:
                self.assign_letter_to_selection()
        event.accept()
    
    def toggle_group_select(self, checked: bool):
        """Toggle group selection mode"""
        self.group_select_mode = checked
        self.group_select_btn.setText("📦 Group Select ON" if checked else "📦 Group Select")
    
    def clear_stroke_selection(self):
        """Clear all stroke selections"""
        if self.canvas.stroke_starts:
            self.canvas.selected_strokes = {0}
        else:
            self.canvas.selected_strokes = set()
        self._update_selection_ui()
        self.canvas.update()
    
    def _update_selection_ui(self):
        """Update UI elements based on current selection state"""
        has_selection = bool(self.canvas.selected_strokes)
        self.assign_letter_btn.setEnabled(has_selection and self.current_word_index >= 0)
        self.clear_selection_btn.setEnabled(has_selection)
    
    def assign_letter_to_selection(self):
        """Assign a letter to the currently selected strokes"""
        if self.current_word_index < 0 or not self.canvas.selected_strokes:
            return
        
        text, ok = QInputDialog.getText(
            self, "Assign Letter", 
            f"Enter a single character for stroke(s) {sorted(self.canvas.selected_strokes)}:\n"
            "(leave empty to unassign)"
        )
        
        if ok:
            stroke_ids = sorted(self.canvas.selected_strokes)
            
            # Empty text = unassign (remove letters for these strokes)
            if not text:
                # Remove any existing letters that used these stroke IDs
                remaining_letters = [
                    l for l in self.canvas.letters 
                    if not (set(l.stroke_ids) & set(stroke_ids))
                ]
                self.canvas.letters = remaining_letters
                print(f"Unassigned strokes {stroke_ids}")
            else:
                # Convert to Hebrew
                hebrew_char = map_to_hebrew(text[0])
                
                # Create new letter object
                new_letter = LetterObject(char=hebrew_char, stroke_ids=stroke_ids)
                
                # Remove any existing letters that used these stroke IDs
                remaining_letters = [
                    l for l in self.canvas.letters 
                    if not (set(l.stroke_ids) & set(stroke_ids))
                ]
                remaining_letters.append(new_letter)
                
                # Sort by first stroke ID
                remaining_letters.sort(key=lambda l: min(l.stroke_ids) if l.stroke_ids else 999)
                
                self.canvas.letters = remaining_letters
                print(f"Assigned '{hebrew_char}' to strokes {stroke_ids}")
            
            # Save to word data
            word_data = self.pen_data[self.current_word_index]
            word_data["letters"] = [l.to_dict() for l in self.canvas.letters]
            
            # Also update legacy assigned_letters for backward compatibility
            word_data["assigned_letters"] = letters_to_assigned_letters(
                self.canvas.letters, self.canvas.stroke_starts
            )
            
            self._update_train_mode_for_letters()
            
            # Recompute correctness
            original_word = word_data.get("word", "")
            is_correct, written = compute_correctness_and_written(
                word_data["assigned_letters"], original_word
            )
            self.word_correctness[self.current_word_index] = is_correct
            self.written_words[self.current_word_index] = written
            
            self.canvas.update()
        
        self.activateWindow()
        self.setFocus()
    
    def edit_letter(self, letter_idx: int):
        """Edit or delete a letter assignment"""
        if letter_idx < 0 or letter_idx >= len(self.canvas.letters):
            return
        
        letter = self.canvas.letters[letter_idx]
        current_char = letter.char if letter.char else "(blocker)"
        
        msg = QMessageBox(self)
        msg.setWindowTitle("Edit Letter Assignment")
        msg.setText(f"Current: '{current_char}' for strokes {letter.stroke_ids}")
        
        delete_btn = msg.addButton("Delete", QMessageBox.DestructiveRole)
        replace_btn = msg.addButton("Replace", QMessageBox.AcceptRole)
        msg.addButton(QMessageBox.Cancel)
        
        msg.exec_()
        
        if msg.clickedButton() == delete_btn:
            self.canvas.letters.pop(letter_idx)
            self._save_letters_to_word_data()
            self.canvas.update()
            print(f"Deleted letter '{current_char}'")
        elif msg.clickedButton() == replace_btn:
            # Select the strokes and re-assign
            self.canvas.selected_strokes = set(letter.stroke_ids)
            self._update_selection_ui()
            self.canvas.update()
            self.assign_letter_to_selection()
        
        self.activateWindow()
        self.setFocus()
    
    def _save_letters_to_word_data(self):
        """Save current letters to word data"""
        if self.current_word_index < 0:
            return
        
        word_data = self.pen_data[self.current_word_index]
        word_data["letters"] = [l.to_dict() for l in self.canvas.letters]
        
        # Also update legacy format
        word_data["assigned_letters"] = letters_to_assigned_letters(
            self.canvas.letters, self.canvas.stroke_starts
        )
        
        self._update_train_mode_for_letters()
        
        # Recompute correctness
        original_word = word_data.get("word", "")
        is_correct, written = compute_correctness_and_written(
            word_data["assigned_letters"], original_word
        )
        self.word_correctness[self.current_word_index] = is_correct
        self.written_words[self.current_word_index] = written
    
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
        """Navigate to previous stroke using selection system"""
        self.canvas.select_prev_stroke(self.group_select_mode)
        self._update_selection_ui()
    
    def next_stroke(self):
        """Navigate to next stroke using selection system"""
        self.canvas.select_next_stroke(self.group_select_mode)
        self._update_selection_ui()
    
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
        
        # Update selection UI
        self._update_selection_ui()
        
        self.update_info()
        self.activateWindow()
        self.setFocus()
        print(f"Loaded word {index + 1}/{len(self.pen_data)}: '{word_data.get('word', '')}'")
        print(f"  → Strokes: {len(self.canvas.stroke_starts)}, Letters: {len(self.canvas.letters)}")
    
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
    # Letter Assignment (Legacy methods kept for backward compatibility)
    # -------------------------------------------------------------------------
    
    def assign_letter_to_stroke(self, stroke_start_idx: int):
        """Legacy method - redirects to new selection-based system"""
        if self.current_word_index < 0:
            return
        
        # Find which stroke this event belongs to
        stroke_idx = self.canvas.get_stroke_for_event(stroke_start_idx)
        self.canvas.selected_strokes = {stroke_idx}
        self._update_selection_ui()
        self.canvas.update()
        self.assign_letter_to_selection()
    
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
        
        # To split a stroke, we just change the type of the current event to "release"
        # and the next event to "press", keeping all other move events as-is
        # This creates two separate strokes at the slice point
        slice_event = self.current_event_index
        
        # The current move event becomes a release (end of first stroke)
        pen_events[slice_event]["type"] = "release"
        
        # The next event becomes a press (start of second stroke)
        if slice_event + 1 < len(pen_events):
            pen_events[slice_event + 1]["type"] = "press"
        
        # No need to shift assigned_letters indices since we're not inserting events
        # But we do need to update letters' stroke_ids
        letters = word_data.get("letters", [])
        if letters:
            new_letters = []
            for letter_dict in letters:
                letter = LetterObject.from_dict(letter_dict) if isinstance(letter_dict, dict) else letter_dict
                new_stroke_ids = []
                for stroke_id in letter.stroke_ids:
                    if stroke_id > current_stroke_idx:
                        new_stroke_ids.append(stroke_id + 1)  # Shift by 1 (one stroke became two)
                    else:
                        new_stroke_ids.append(stroke_id)
                new_letters.append(LetterObject(char=letter.char, stroke_ids=new_stroke_ids).to_dict())
            word_data["letters"] = new_letters
        
        # Reload the word to refresh everything
        self.load_word(self.current_word_index)
        self.event_slider.setValue(slice_event + 1)  # Move to new press event
        
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
                    
                    # Reorganize pen_events into strokes with downsampling
                    stroke_starts, stroke_ends = find_stroke_indices(pen_events)
                    strokes = []
                    total_original = 0
                    total_downsampled = 0
                    
                    for stroke_id, (start_idx, end_idx) in enumerate(zip(stroke_starts, stroke_ends)):
                        stroke_events = []
                        for i in range(start_idx, end_idx + 1):
                            if i < len(pen_events):
                                event = pen_events[i].copy()
                                # Remove event_id if it exists (will be implicit by position in stroke)
                                event.pop('event_id', None)
                                stroke_events.append(event)
                        
                        total_original += len(stroke_events)
                        
                        # Apply downsampling for ML efficiency (25ms = 40Hz, 3px min distance)
                        downsampled_events = downsample_stroke_events(stroke_events, target_interval_ms=25, min_distance_px=3)
                        total_downsampled += len(downsampled_events)
                        
                        strokes.append({
                            'stroke_id': stroke_id,
                            'events': downsampled_events
                        })
                    
                    # Print compression stats for this word
                    if total_original > 0:
                        compression = (1 - total_downsampled / total_original) * 100
                        print(f"  Word '{original_word}': {total_original} → {total_downsampled} events ({compression:.1f}% reduction)")
                    
                    word_entry = {
                        'written_word': written,
                        'trainability': trainability,
                        'audio_start_time': word_data.get('audio_start_time'),
                        'audio_end_time': word_data.get('audio_end_time'),
                        'strokes': strokes,
                        'letters': word_data.get('letters', [])
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
