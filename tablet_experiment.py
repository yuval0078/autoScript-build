"""
Tablet Writing Experiment with Calibration
Stage 1: Calibrate physical paper grid to virtual canvas
"""

import sys
import math
import os
import subprocess
import argparse
import winsound
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QMessageBox, QInputDialog)
from PyQt5.QtCore import Qt, QTimer, QPointF, QEvent
from PyQt5.QtGui import QPainter, QPen, QColor, QTabletEvent, QFont, QBrush
from datetime import datetime
import time
import json


class PenDataRecorder:
    """Records pen movement data (position, pressure, speed, etc.)"""
    
    def __init__(self):
        """Initialize pen data recorder"""
        self.all_word_data = []  # List of all word recordings
        self.current_word_data = None
        self.last_point = None
        self.last_time = None
    
    def start_word(self, word_info):
        """Start recording a new word"""
        self.current_word_data = {
            'word': word_info['word'],
            'cell': word_info['cell'],
            'group': word_info.get('group', 'unknown'),  # Group name
            'start_time': time.time(),
            'end_time': None,
            'audio_start_time': None,  # When audio starts playing
            'audio_end_time': None,    # When audio finishes playing
            'pen_events': []  # List of pen events with full data
        }
        self.last_point = None
        self.last_time = None
        print(f"🖊 Recording pen data for word: '{word_info['word']}' (group: {word_info.get('group', 'unknown')})")
    
    def record_event(self, event_type, x, y, pressure, timestamp):
        """Record a pen event with position, pressure, and calculated speed"""
        if not self.current_word_data:
            return
        
        current_time = time.time()
        
        # Calculate speed if we have a previous point
        speed = 0.0
        if self.last_point and self.last_time:
            dx = x - self.last_point[0]
            dy = y - self.last_point[1]
            distance = math.sqrt(dx*dx + dy*dy)
            time_delta = current_time - self.last_time
            if time_delta > 0:
                speed = distance / time_delta  # pixels per second
        
        # Record event
        event_data = {
            'type': event_type,  # 'press', 'move', 'release'
            'x': x,
            'y': y,
            'pressure': pressure,
            'timestamp': timestamp,
            'absolute_time': current_time,
            'speed': speed
        }
        
        self.current_word_data['pen_events'].append(event_data)
        
        # Update last point and time
        self.last_point = (x, y)
        self.last_time = current_time
    
    def set_audio_start(self):
        """Mark when audio starts playing"""
        if self.current_word_data:
            self.current_word_data['audio_start_time'] = time.time()
    
    def set_audio_end(self):
        """Mark when audio finishes playing"""
        if self.current_word_data:
            self.current_word_data['audio_end_time'] = time.time()
    
    def end_word(self):
        """Finish recording current word"""
        if self.current_word_data:
            self.current_word_data['end_time'] = time.time()
            self.all_word_data.append(self.current_word_data)
            print(f"✓ Recorded {len(self.current_word_data['pen_events'])} pen events")
            self.current_word_data = None
            self.last_point = None
            self.last_time = None
    
    def save_to_file(self, filepath):
        """Save all recorded data to JSON file"""
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self.all_word_data, f, ensure_ascii=False, indent=2)
            
            print(f"✓ Pen data saved: {filepath}")
            print(f"  Total words recorded: {len(self.all_word_data)}")
            total_events = sum(len(word['pen_events']) for word in self.all_word_data)
            print(f"  Total pen events: {total_events}")
            return True
        except Exception as e:
            print(f"✗ Error saving pen data: {e}")
            return False


class CalibrationCanvas(QWidget):
    """Canvas for calibration - captures 4 corner points"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        # No minimum size - will fill entire screen
        self.setStyleSheet("background-color: white;")
        
        # Reference to main window
        self.main_window = None
        
        # Calibration state
        self.calibration_points = []  # Will store 4 corner points (any order)
        self.current_step = 0  # Number of corners captured (0-3)
        
        # Tablet state
        self.pen_x = 0
        self.pen_y = 0
        self.pen_touching = False
        self.touch_start_time = None
        self.touch_recorded = False  # Track if current touch already recorded
        
        # Enable tablet tracking
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_TabletTracking, True)
    
    def tabletEvent(self, event: QTabletEvent):
        """Handle tablet events"""
        import time
        
        # Get position - use globalPos for consistency
        global_pos = event.globalPos()
        self.pen_x = global_pos.x()
        self.pen_y = global_pos.y()
        
        # Get pressure
        pressure = event.pressure()
        
        event_type = event.type()
        
        if event_type == QTabletEvent.TabletPress:
            # Pen touched - start timing
            self.pen_touching = True
            self.touch_start_time = time.time()
            self.touch_recorded = False
            print(f"  Press detected at ({self.pen_x:.1f}, {self.pen_y:.1f})")
            
        elif event_type == QTabletEvent.TabletMove:
            if pressure > 0.01:
                self.pen_touching = True
                if not self.touch_start_time:
                    self.touch_start_time = time.time()
                    self.touch_recorded = False
                
                # Check if we've held long enough (check during move)
                if self.touch_start_time and not self.touch_recorded:
                    duration = time.time() - self.touch_start_time
                    if duration >= 0.5 and self.current_step < 4:
                        # Valid touch - record calibration point
                        self.calibration_points.append((self.pen_x, self.pen_y))
                        self.current_step += 1
                        self.touch_recorded = True
                        
                        if self.main_window:
                            self.main_window.update_calibration_status()
                        
                        print(f"✓ Recorded corner {self.current_step}/4: ({self.pen_x:.1f}, {self.pen_y:.1f})")
                        
                        # Check if calibration is complete
                        if self.current_step == 4:
                            if self.main_window:
                                self.main_window.calibration_complete()
            else:
                self.pen_touching = False
                
        elif event_type == QTabletEvent.TabletRelease:
            # Pen released - just reset state
            print(f"  Release detected")
            self.pen_touching = False
            self.touch_start_time = None
            self.touch_recorded = False
        
        self.update()
        event.accept()
    
    def paintEvent(self, event):
        """Draw the canvas with calibration points"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Check if waiting for spacebar (after successful calibration)
        if self.main_window and getattr(self.main_window, 'waiting_for_spacebar', False):
            # Draw "Press SPACE to continue" message
            painter.fillRect(self.rect(), QColor(255, 255, 255))
            painter.setPen(QPen(QColor(0, 128, 0)))
            painter.setFont(QFont("Arial", 32, QFont.Bold))
            painter.drawText(self.rect(), Qt.AlignCenter, "✓ Calibration successful!\n\nPress SPACE to start experiment")
            return
        
        # Draw instructions overlay at top center
        painter.setPen(QPen(QColor(0, 0, 0)))
        painter.setBrush(QBrush(QColor(255, 243, 205, 230)))
        
        # Instruction box
        box_width = 600
        box_height = 100
        box_x = (self.width() - box_width) // 2
        box_y = 20
        painter.drawRoundedRect(box_x, box_y, box_width, box_height, 10, 10)
        
        # Instruction text
        painter.setFont(QFont("Arial", 16, QFont.Bold))
        painter.setPen(QPen(QColor(102, 126, 234)))
        
        if self.current_step < 4:
            step_text = f"Touch {self.current_step + 1}/4: HOLD any paper corner (0.5s)"
        else:
            step_text = "All 4 corners captured! Press 'V' to validate or 'R' to reset"
        
        painter.drawText(box_x + 10, box_y + 35, box_width - 20, 30, Qt.AlignCenter, step_text)
        
        painter.setFont(QFont("Arial", 12))
        painter.setPen(QPen(QColor(100, 100, 100)))
        painter.drawText(box_x + 10, box_y + 60, box_width - 20, 30, Qt.AlignCenter, 
                        "Corners auto-detected by position | ESC=exit | R=reset | V=validate")
        
        # Draw recorded calibration points
        for i, (x, y) in enumerate(self.calibration_points):
            # Draw circle at each point
            painter.setPen(QPen(QColor(102, 126, 234, 2)))
            painter.setBrush(QBrush(QColor(102, 126, 234, 100)))
            painter.drawEllipse(QPointF(x, y), 10, 10)
            
            # Draw corner number
            painter.setPen(QPen(QColor(0, 0, 0)))
            painter.setFont(QFont("Arial", 10, QFont.Bold))
            painter.drawText(int(x + 15), int(y + 5), str(i + 1))
        
        # Draw current pen position if touching
        if self.pen_touching:
            painter.setPen(QPen(QColor(220, 53, 69), 3))
            painter.setBrush(QBrush(QColor(220, 53, 69, 150)))
            painter.drawEllipse(QPointF(self.pen_x, self.pen_y), 8, 8)
        
        # Draw preview of calibration rectangle if we have 2+ points
        if len(self.calibration_points) >= 2:
            painter.setPen(QPen(QColor(102, 126, 234, 100), 2, Qt.DashLine))
            
            if len(self.calibration_points) == 2:
                # Draw line between TL and TR
                painter.drawLine(
                    int(self.calibration_points[0][0]), int(self.calibration_points[0][1]),
                    int(self.calibration_points[1][0]), int(self.calibration_points[1][1])
                )
            elif len(self.calibration_points) == 3:
                # Draw three sides
                painter.drawLine(
                    int(self.calibration_points[0][0]), int(self.calibration_points[0][1]),
                    int(self.calibration_points[1][0]), int(self.calibration_points[1][1])
                )
                painter.drawLine(
                    int(self.calibration_points[0][0]), int(self.calibration_points[0][1]),
                    int(self.calibration_points[2][0]), int(self.calibration_points[2][1])
                )
                painter.drawLine(
                    int(self.calibration_points[1][0]), int(self.calibration_points[1][1]),
                    int(self.calibration_points[2][0]), int(self.calibration_points[2][1])
                )
            elif len(self.calibration_points) == 4:
                # Draw full rectangle
                points = self.calibration_points
                painter.drawLine(int(points[0][0]), int(points[0][1]), int(points[1][0]), int(points[1][1]))  # TL to TR
                painter.drawLine(int(points[0][0]), int(points[0][1]), int(points[2][0]), int(points[2][1]))  # TL to BL
                painter.drawLine(int(points[1][0]), int(points[1][1]), int(points[3][0]), int(points[3][1]))  # TR to BR
                painter.drawLine(int(points[2][0]), int(points[2][1]), int(points[3][0]), int(points[3][1]))  # BL to BR
    
    def reset_calibration(self):
        """Reset calibration and start over"""
        self.calibration_points = []
        self.current_step = 0
        self.touch_start_time = None
        self.update()


class CalibrationWindow(QMainWindow):
    """Main calibration window"""
    
    def __init__(self, config=None):
        super().__init__()
        self.config = config
        self.waiting_for_spacebar = False
        self.resume_experiment_data = None  # For recalibration resume
        self.init_ui()
    
    def init_ui(self):
        """Initialize the user interface"""
        self.setWindowTitle("Tablet Experiment - Calibration")
        
        # Make window fullscreen
        self.showFullScreen()
        
        # Canvas as the only central widget (fullscreen)
        self.canvas = CalibrationCanvas(self)
        self.canvas.main_window = self
        self.setCentralWidget(self.canvas)
        
        # Enable tablet events
        self.setAttribute(Qt.WA_TabletTracking, True)
    
    def keyPressEvent(self, event):
        """Handle keyboard events"""
        if event.key() == Qt.Key_Escape:
            # ESC to exit fullscreen or close
            self.close()
        elif event.key() == Qt.Key_Space:
            # Space to proceed after calibration
            if self.waiting_for_spacebar:
                self.waiting_for_spacebar = False
                self.start_experiment()
        elif event.key() == Qt.Key_R:
            # R to reset calibration
            self.waiting_for_spacebar = False
            self.reset_calibration()
        elif event.key() == Qt.Key_V:
            # V to validate
            if self.canvas.current_step == 4:
                self.validate_calibration()
        event.accept()
    
    def update_calibration_status(self):
        """Update status based on calibration progress"""
        # Just trigger a repaint to update the overlay text
        self.canvas.update()
    
    def calibration_complete(self):
        """Called when all 4 corners are captured"""
        # Just trigger a repaint to update the overlay text
        self.canvas.update()
    
    def reset_calibration(self):
        """Reset and start calibration over"""
        self.canvas.reset_calibration()
        print("\n⟲ Calibration reset")
    
    def validate_calibration(self):
        """Validate the calibration and check if it's a good rectangle"""
        points = self.canvas.calibration_points
        
        if len(points) != 4:
            return
        
        # Auto-identify corners by position (regardless of order)
        print("\n🔍 Auto-identifying corners by position...")
        
        # Find the topmost point (smallest y)
        topmost = min(points, key=lambda p: p[1])
        # Find the bottommost point (largest y)
        bottommost = max(points, key=lambda p: p[1])
        # Find the leftmost point (smallest x)
        leftmost = min(points, key=lambda p: p[0])
        # Find the rightmost point (largest x)
        rightmost = max(points, key=lambda p: p[0])
        
        # Top-left: among points with smaller y, pick the one with smaller x
        top_candidates = [p for p in points if p[1] <= (topmost[1] + bottommost[1]) / 2]
        tl = min(top_candidates, key=lambda p: p[0])
        
        # Top-right: among points with smaller y, pick the one with larger x
        tr = max(top_candidates, key=lambda p: p[0])
        
        # Bottom-left: among points with larger y, pick the one with smaller x
        bottom_candidates = [p for p in points if p[1] > (topmost[1] + bottommost[1]) / 2]
        bl = min(bottom_candidates, key=lambda p: p[0])
        
        # Bottom-right: among points with larger y, pick the one with larger x
        br = max(bottom_candidates, key=lambda p: p[0])
        
        # Update the points in correct order
        self.canvas.calibration_points = [tl, tr, bl, br]
        
        print(f"  Top-Left: {tl}")
        print(f"  Top-Right: {tr}")
        print(f"  Bottom-Left: {bl}")
        print(f"  Bottom-Right: {br}")
        
        # Calculate side lengths
        top_length = math.sqrt((tr[0] - tl[0])**2 + (tr[1] - tl[1])**2)
        bottom_length = math.sqrt((br[0] - bl[0])**2 + (br[1] - bl[1])**2)
        left_length = math.sqrt((bl[0] - tl[0])**2 + (bl[1] - tl[1])**2)
        right_length = math.sqrt((br[0] - tr[0])**2 + (br[1] - tr[1])**2)
        
        # Calculate diagonals
        diag1 = math.sqrt((br[0] - tl[0])**2 + (br[1] - tl[1])**2)
        diag2 = math.sqrt((bl[0] - tr[0])**2 + (bl[1] - tr[1])**2)
        
        # Check if it's close to a rectangle
        # Opposite sides should be similar length
        horizontal_diff = abs(top_length - bottom_length)
        vertical_diff = abs(left_length - right_length)
        diagonal_diff = abs(diag1 - diag2)
        
        # Thresholds (in pixels, ~15mm assuming ~96 DPI = ~3.78 pixels/mm)
        tolerance_mm = 15  # 15mm tolerance (increased from 5mm)
        tolerance_pixels = tolerance_mm * 3.78
        
        print(f"\nCalibration Analysis:")
        print(f"  Top: {top_length:.1f}px, Bottom: {bottom_length:.1f}px (diff: {horizontal_diff:.1f}px)")
        print(f"  Left: {left_length:.1f}px, Right: {right_length:.1f}px (diff: {vertical_diff:.1f}px)")
        print(f"  Diagonals: {diag1:.1f}px, {diag2:.1f}px (diff: {diagonal_diff:.1f}px)")
        print(f"  Tolerance: {tolerance_pixels:.1f}px ({tolerance_mm}mm)")
        
        if (horizontal_diff < tolerance_pixels and 
            vertical_diff < tolerance_pixels and 
            diagonal_diff < tolerance_pixels * 1.5):
            
            # Good enough - use calibrated points as-is
            print("✓ Calibration accepted - using actual corner positions")
            
            # Store calibration data with actual points (no correction)
            self.calibration_data = {
                'corners': [tl, tr, bl, br]
            }
            
            # Show success and proceed
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("Calibration Successful")
            msg.setText("✓ Calibration successful!\n\nProceeding to experiment.")
            msg.exec_()
            
            print("✓ Calibration complete - waiting for spacebar to proceed")
            
            # Show "press spacebar to continue" message
            self.waiting_for_spacebar = True
            self.canvas.update()
            
        else:
            # Not a good rectangle - ask to redo
            print("✗ Shape is not rectangular enough - please recalibrate")
            
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("Calibration Issue")
            msg.setText(
                "The captured points don't form a good rectangle.\n\n"
                "Possible issues:\n"
                "• Paper is not aligned properly\n"
                "• Corners were not touched accurately\n\n"
                "Please try again."
            )
            msg.exec_()
            
            self.reset_calibration()
    
    def start_experiment(self):
        """Start the experiment stage after successful calibration"""
        # Check if this is a recalibration (resume existing experiment)
        if hasattr(self, 'resume_experiment_data') and self.resume_experiment_data:
            resume_data = self.resume_experiment_data
            self.hide()
            
            # Create experiment window with resumed state
            self.experiment_window = ExperimentWindow(
                self.calibration_data, 
                resume_data['participant_number'], 
                self.config,
                resume_data['age'],
                resume_data['gender']
            )
            # Restore experiment state
            self.experiment_window.canvas.current_cell = resume_data['current_cell']
            self.experiment_window.canvas.pen_recorder = resume_data['pen_recorder']
            self.experiment_window.canvas.all_data = resume_data['all_data']
            self.experiment_window.canvas.page_number = resume_data['page_number']
            self.experiment_window.show()
            
            # Continue playing current word
            self.experiment_window.canvas.play_current_word()
            print(f"✓ Resumed experiment at word {resume_data['current_cell'] + 1}")
            return
        
        # New experiment - ask for participant details
        participant_number, ok = QInputDialog.getInt(
            self,
            'Participant Number',
            'Enter participant number:',
            value=1,
            min=1,
            max=9999
        )
        
        if not ok:
            # User cancelled - go back to calibration
            return
        
        # Ask for age
        age, ok = QInputDialog.getInt(
            self,
            'Participant Age',
            'Enter participant age:',
            value=25,
            min=1,
            max=120
        )
        
        if not ok:
            return
        
        # Ask for gender
        gender, ok = QInputDialog.getItem(
            self,
            'Participant Gender',
            'Select participant gender:',
            ['Male', 'Female', 'Other', 'Prefer not to say'],
            0,
            False
        )
        
        if not ok:
            return
        
        self.hide()  # Hide calibration window
        
        # Create and show experiment window with age and gender
        self.experiment_window = ExperimentWindow(
            self.calibration_data, participant_number, self.config, age, gender
        )
        self.experiment_window.show()


class ExperimentCanvas(QWidget):
    """Canvas for the main experiment with configurable grid"""
    
    def __init__(self, calibration_data, participant_number, config=None, age=None, gender=None, parent=None):
        super().__init__(parent)
        self.calibration_data = calibration_data
        self.participant_number = participant_number
        self.config = config
        self.participant_age = age
        self.participant_gender = gender
        self.current_cell = 0
        
        # Default settings
        self.grid_rows = 5
        self.grid_cols = 5
        self.proceed_mode = 'key'
        self.proceed_key = Qt.Key_Space
        self.proceed_delay = 2000
        self.beep_before = False
        self.beep_before_delay = 100
        self.beep_after = False
        self.beep_after_delay = 100
        self.exp_name = "experiment"
        
        # Parse config if available
        if self.config:
            props = self.config.get('properties', {})
            self.exp_name = props.get('experiment_name', 'experiment')
            
            grid = props.get('grid', {})
            self.grid_rows = grid.get('rows', 5)
            self.grid_cols = grid.get('cols', 5)
            
            proceed = props.get('proceed_condition', {})
            self.proceed_mode = proceed.get('type', 'key')
            self.proceed_delay = proceed.get('delay_ms', 2000)
            
            key_map = {
                "Space": Qt.Key_Space,
                "Enter": Qt.Key_Return,
                "Right Arrow": Qt.Key_Right,
                "Down Arrow": Qt.Key_Down
            }
            self.proceed_key = key_map.get(proceed.get('key', 'Space'), Qt.Key_Space)
            
            beeps = props.get('beeps', {})
            self.beep_before = beeps.get('before', {}).get('enabled', False)
            self.beep_before_delay = beeps.get('before', {}).get('delay_ms', 100)
            self.beep_after = beeps.get('after', {}).get('enabled', False)
            self.beep_after_delay = beeps.get('after', {}).get('delay_ms', 100)
            
        self.grid_size = self.grid_rows # For compatibility with some methods, though we should use rows/cols
        self.total_cells = self.grid_rows * self.grid_cols
        
        self.current_strokes = []  # Strokes for current cell
        self.all_data = []  # All experiment data
        
        # Pagination state
        self.page_number = 1
        self.is_paused_for_refresh = False
        
        # Timing data for current word
        self.current_word_data = {
            'reading_start': None,
            'reading_end': None,
            'writing_start': None,
            'writing_end': None,
            'video_file': None
        }
        
        # Get the 4 calibrated corner points (actual screen coordinates where user touched)
        corners = calibration_data['corners']
        self.calib_tl = corners[0]  # Top-Left
        self.calib_tr = corners[1]  # Top-Right
        self.calib_bl = corners[2]  # Bottom-Left
        self.calib_br = corners[3]  # Bottom-Right
        
        # Load word files
        self.load_words()
        
        # Track tablet state
        self.is_drawing = False
        self.current_stroke = []
        
        # Pen data recorder
        self.pen_recorder = PenDataRecorder()
        
        # Audio monitoring timer
        self.audio_monitor_timer = QTimer()
        self.audio_monitor_timer.timeout.connect(self.check_audio_finished)
        self.audio_monitor_timer.setInterval(100)  # Check every 100ms
        
        # Auto-proceed timer
        self.auto_proceed_timer = QTimer()
        self.auto_proceed_timer.setSingleShot(True)
        self.auto_proceed_timer.timeout.connect(self.advance_to_next_word)
        
        # Set up for tablet events
        self.setAttribute(Qt.WA_TabletTracking, True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        
    def load_words(self):
        """Load words from config or legacy file"""
        import random
        
        self.words = []
        
        if self.config:
            # Load from config object
            words_data = self.config.get('words', {})
            base_dir = os.path.dirname(self.config.get('__file_path__', '.'))
            audio_dir = os.path.join(base_dir, 'audio')
            repetitions = self.config.get('properties', {}).get('repetitions', {})
            order = self.config.get('properties', {}).get('order', 'random')
            
            # First, load all unique words
            unique_words = []
            for group_name, word_list in words_data.items():
                for word_entry in word_list:
                    # Audio file path relative to config location
                    word_file = os.path.join(audio_dir, word_entry['file'])
                    unique_words.append({
                        'file': word_file,
                        'word': word_entry['word'],
                        'group': group_name
                    })
            
            # Handle repetitions
            if order == 'random':
                # Random order: repeat each word X times based on group, then shuffle with spacing
                word_pool = []
                for word_data in unique_words:
                    group_name = word_data['group']
                    repeat_count = repetitions.get(group_name, 1)
                    # Add this word multiple times
                    for rep in range(repeat_count):
                        word_pool.append(word_data.copy())
                
                # Shuffle with spacing constraint: same words should be spaced apart
                self.words = self._shuffle_with_spacing(word_pool)
                
            else:
                # Ordinal order: play all groups in order, then repeat entire sequence
                # Use maximum repetition count
                max_repeats = max(repetitions.values()) if repetitions else 1
                
                for rep in range(max_repeats):
                    for word_data in unique_words:
                        group_name = word_data['group']
                        group_repeats = repetitions.get(group_name, 1)
                        # Only add if this repetition is within the group's repeat count
                        if rep < group_repeats:
                            self.words.append(word_data.copy())
            
        else:
            # Legacy loading
            json_path = os.path.join('src', 'word_labels.json')
            if not os.path.exists(json_path):
                print("✗ word_labels.json not found!")
                return
            
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            for recording_name, word_list in data.items():
                for word_entry in word_list:
                    word_file = os.path.join('src', 'sliced_words', word_entry['file'])
                    self.words.append({
                        'file': word_file,
                        'word': word_entry['word'],
                        'group': recording_name
                    })
            random.shuffle(self.words)
        
        # Note: We do NOT limit to grid size anymore, as we support multiple pages
        # self.words = self.words[:self.total_cells] 
        print(f"✓ Loaded {len(self.words)} words for experiment")
    
    def _shuffle_with_spacing(self, word_pool):
        """
        Shuffle words while ensuring same words are spaced apart.
        Uses a greedy algorithm to maximize spacing between identical words.
        """
        import random
        from collections import defaultdict
        
        if not word_pool:
            return []
        
        # Group words by their text
        word_groups = defaultdict(list)
        for word_data in word_pool:
            word_groups[word_data['word']].append(word_data)
        
        # If all words are unique, just shuffle
        if all(len(group) == 1 for group in word_groups.values()):
            shuffled = word_pool.copy()
            random.shuffle(shuffled)
            return shuffled
        
        # Build result list by picking words that maximize spacing
        result = []
        available_groups = {word: group.copy() for word, group in word_groups.items()}
        
        while any(available_groups.values()):
            # Find which words are available (not recently used)
            recent_words = set()
            lookback = min(5, len(result))  # Look back at last 5 words
            if result:
                recent_words = {result[-i]['word'] for i in range(1, lookback + 1) if i <= len(result)}
            
            # Get words that haven't been used recently
            available_now = [word for word, group in available_groups.items() 
                           if group and word not in recent_words]
            
            # If no words available (all were recent), allow any remaining word
            if not available_now:
                available_now = [word for word, group in available_groups.items() if group]
            
            if not available_now:
                break
            
            # Randomly pick from available words
            chosen_word = random.choice(available_now)
            word_data = available_groups[chosen_word].pop(0)
            result.append(word_data)
            
            # Remove empty groups
            if not available_groups[chosen_word]:
                del available_groups[chosen_word]
        
        return result
    
    def check_audio_finished(self):
        """Check if audio has finished playing"""
        try:
            import pygame
            if pygame.mixer.get_init() and not pygame.mixer.music.get_busy():
                # Audio finished playing
                if self.pen_recorder.current_word_data and self.pen_recorder.current_word_data['audio_end_time'] is None:
                    self.pen_recorder.set_audio_end()
                    print(f"♪ Audio finished playing")
                    
                    # Beep after
                    if self.beep_after:
                        QTimer.singleShot(self.beep_after_delay, lambda: winsound.Beep(800, 150))
                    
                    # Auto proceed
                    if self.proceed_mode == 'time':
                        delay = self.proceed_delay
                        if self.beep_after:
                            delay += self.beep_after_delay + 150
                        print(f"⏳ Auto-advancing in {delay}ms...")
                        self.auto_proceed_timer.start(delay)
                        
                # Stop the timer
                self.audio_monitor_timer.stop()
        except Exception as e:
            pass
    
    def play_current_word(self):
        """Play audio for current word"""
        if self.current_cell >= len(self.words):
            print("✓ Experiment complete!")
            return
        
        # Beep before
        if self.beep_before:
            winsound.Beep(800, 150)
            # We need to delay the rest, but for simplicity we'll just sleep (blocking UI briefly is ok here)
            # or use a timer. Let's use a timer to start the actual logic.
            QTimer.singleShot(self.beep_before_delay, self._start_word_logic)
        else:
            self._start_word_logic()

    def _start_word_logic(self):
        # Reset timing data for new word
        self.current_word_data = {
            'reading_start': time.time(),
            'reading_end': None,
            'writing_start': None,
            'writing_end': None,
            'video_file': None
        }
        
        # Start pen data recording for this word
        word_data = self.words[self.current_cell]
        self.pen_recorder.start_word({
            'word': word_data['word'],
            'cell': self.current_cell,
            'group': word_data['group']
        })
        
        # Mark audio start time
        self.pen_recorder.set_audio_start()
        
        audio_file = word_data['file']
        
        # Try to use wav version if available (better compatibility)
        if audio_file.lower().endswith('.m4a'):
            wav_file = audio_file[:-4] + '.wav'
            if os.path.exists(wav_file):
                audio_file = wav_file
        
        if not os.path.exists(audio_file):
            print(f"✗ Audio file not found: {audio_file}")
            return
        
        # Convert to absolute path
        abs_audio_file = os.path.abspath(audio_file)
        
        # Try pygame mixer with wav files
        try:
            import pygame
            if not pygame.mixer.get_init():
                # Initialize with higher frequency for better quality
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            
            # Load and play the sound
            pygame.mixer.music.load(abs_audio_file)
            pygame.mixer.music.play()
            
            # Start monitoring for audio completion
            self.audio_monitor_timer.start()
            
            print(f"♪ Playing word {self.current_cell + 1}: '{word_data['word']}' ({os.path.basename(audio_file)})")
        except Exception as e:
            print(f"✗ Error playing with pygame: {e}")
            # Fallback to opening in external player
            try:
                os.startfile(abs_audio_file)
                print(f"♪ Playing word {self.current_cell + 1} (via media player): '{word_data['word']}'")
            except Exception as e2:
                print(f"✗ Error playing audio: {e2}")
    
    def advance_to_next_word(self):
        """Move to next cell and play next word"""
        # End pen data recording for current word
        self.pen_recorder.end_word()
        
        # Mark reading end time (when user advances)
        if self.current_word_data['reading_end'] is None:
            self.current_word_data['reading_end'] = time.time()
        
        # Save current cell data with timing information
        if self.current_strokes or self.current_cell < len(self.words):
            cell_data = {
                'cell': self.current_cell,
                'word': self.words[self.current_cell]['word'] if self.current_cell < len(self.words) else '',
                'strokes': self.current_strokes,
                'reading_start_time': self.current_word_data['reading_start'],
                'reading_end_time': self.current_word_data['reading_end'],
                'writing_start_time': self.current_word_data['writing_start'],
                'writing_end_time': self.current_word_data['writing_end'],
                'video_file': self.current_word_data['video_file']
            }
            self.all_data.append(cell_data)
        
        # Move to next cell
        self.current_cell += 1
        self.current_strokes = []
        
        # Check if experiment is complete
        if self.current_cell >= len(self.words):
            self.finish_experiment()
            return
            
        # Check for page refresh (if grid is full)
        if self.current_cell > 0 and self.current_cell % self.total_cells == 0:
            print("⚠ Page full - pausing for refresh")
            self.is_paused_for_refresh = True
            self.update()
            return
        
        # Play next word
        self.play_current_word()
        self.update()
    
    def finish_experiment(self):
        """Save data and finish experiment"""
        import json
        from pathlib import Path

        from app_paths import ensure_dir, user_data_dir
        
        # End any ongoing pen recording
        if self.pen_recorder.current_word_data:
            self.pen_recorder.end_word()
        
        # Extract experiment name from config file path (zip name)
        exp_name_for_file = self.exp_name
        if self.config and '__file_path__' in self.config:
            config_path = Path(self.config['__file_path__'])
            # Get the parent directory name (should be the unzipped experiment folder)
            exp_folder = config_path.parent.name
            if exp_folder and exp_folder != 'current_experiment':
                exp_name_for_file = exp_folder
            else:
                # Fallback to JSON filename without extension
                exp_name_for_file = config_path.stem
        
        # Create results directory
        results_dir = ensure_dir(user_data_dir() / 'results')
        results_dir.mkdir(exist_ok=True)
        
        # Create data filename: experimentname_pN_timestamp.json
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        data_file = results_dir / f'{exp_name_for_file}_p{self.participant_number}_{timestamp}.json'
        
        # Combine all data into single structure
        combined_data = {
            'experiment_name': self.exp_name,
            'participant_number': self.participant_number,
            'participant_age': self.participant_age,
            'participant_gender': self.participant_gender,
            'timestamp': timestamp,
            'calibration': self.calibration_data,
            'config': self.config,
            'words': self.pen_recorder.all_word_data  # Contains pen events and audio timing
        }
        
        # Save combined data
        try:
            with open(str(data_file), 'w', encoding='utf-8') as f:
                json.dump(combined_data, f, ensure_ascii=False, indent=2)
            
            print(f"✓ Experiment complete! Data saved to {data_file}")
            print(f"  Participant: {self.participant_number}")
            print(f"  Total words recorded: {len(self.pen_recorder.all_word_data)}")
            total_events = sum(len(word['pen_events']) for word in self.pen_recorder.all_word_data)
            print(f"  Total pen events: {total_events}")
            
            # Show completion message
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("Experiment Complete")
            msg.setText(f"Experiment finished!\n\nData saved to:\n{str(data_file)}")
            msg.exec_()
            
        except Exception as e:
            print(f"✗ Error saving data: {e}")
            QMessageBox.critical(None, "Error", f"Failed to save data:\n{str(e)}")
        
        # Ensure audio resources are released
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
                pygame.mixer.quit()
        except Exception:
            pass

        # Close application
        QApplication.quit()
    
    def transform_point(self, pen_x, pen_y):
        """
        Transform physical pen position to virtual grid position using bilinear interpolation.
        This properly maps the quadrilateral formed by calibration points to a normalized square.
        """
        # We need to find (u, v) in [0,1]x[0,1] such that:
        # pen_pos = (1-v)*(1-u)*TL + (1-v)*u*TR + v*(1-u)*BL + v*u*BR
        
        # For simplicity, use inverse bilinear interpolation
        # We'll solve this iteratively using Newton's method
        
        # Start with initial guess based on bounding box
        width = max(self.calib_tr[0], self.calib_br[0]) - min(self.calib_tl[0], self.calib_bl[0])
        height = max(self.calib_bl[1], self.calib_br[1]) - min(self.calib_tl[1], self.calib_tr[1])
        
        u = (pen_x - self.calib_tl[0]) / width if width > 0 else 0.5
        v = (pen_y - self.calib_tl[1]) / height if height > 0 else 0.5
        
        # Iterative refinement (5 iterations should be enough)
        for _ in range(5):
            # Calculate current position based on u, v
            x_est = ((1-v)*(1-u)*self.calib_tl[0] + (1-v)*u*self.calib_tr[0] + 
                     v*(1-u)*self.calib_bl[0] + v*u*self.calib_br[0])
            y_est = ((1-v)*(1-u)*self.calib_tl[1] + (1-v)*u*self.calib_tr[1] + 
                     v*(1-u)*self.calib_bl[1] + v*u*self.calib_br[1])
            
            # Calculate error
            dx = pen_x - x_est
            dy = pen_y - y_est
            
            # If error is small enough, stop
            if abs(dx) < 0.1 and abs(dy) < 0.1:
                break
            
            # Calculate Jacobian (partial derivatives)
            dxdu = (1-v)*(self.calib_tr[0] - self.calib_tl[0]) + v*(self.calib_br[0] - self.calib_bl[0])
            dxdv = (1-u)*(self.calib_bl[0] - self.calib_tl[0]) + u*(self.calib_br[0] - self.calib_tr[0])
            dydu = (1-v)*(self.calib_tr[1] - self.calib_tl[1]) + v*(self.calib_br[1] - self.calib_bl[1])
            dydv = (1-u)*(self.calib_bl[1] - self.calib_tl[1]) + u*(self.calib_br[1] - self.calib_tr[1])
            
            # Solve 2x2 system: J * delta = error
            det = dxdu * dydv - dxdv * dydu
            if abs(det) > 0.001:
                du = (dydv * dx - dxdv * dy) / det
                dv = (-dydu * dx + dxdu * dy) / det
                u += du
                v += dv
        
        # Clamp to valid range
        u = max(0, min(1, u))
        v = max(0, min(1, v))
        
        # The virtual position is just the pen position (we draw the grid at the calibrated corners)
        virtual_x = pen_x
        virtual_y = pen_y
        
        return virtual_x, virtual_y, u, v
    
    def get_cell_from_position(self, x_ratio, y_ratio):
        """Determine which grid cell a position is in using normalized ratios (0-1)"""
        # Check if outside canvas bounds
        if x_ratio < 0 or x_ratio > 1 or y_ratio < 0 or y_ratio > 1:
            return -1  # Outside grid
        
        # Determine cell
        col = int(x_ratio * self.grid_cols)
        row = int(y_ratio * self.grid_rows)
        
        # Clamp to valid range
        col = max(0, min(self.grid_cols - 1, col))
        row = max(0, min(self.grid_rows - 1, row))
        
        # Hebrew reading order: right-to-left, top-to-bottom
        # Cell numbering: right column is 0-4, next column left is 5-9, etc.
        cell = row * self.grid_cols + (self.grid_cols - 1 - col)
        
        return cell
    
    def tabletEvent(self, event):
        """Handle tablet events for drawing"""
        # Get physical pen coordinates (use globalPos for consistency with calibration)
        global_pos = event.globalPos()
        pen_x = global_pos.x()
        pen_y = global_pos.y()
        pressure = event.pressure()
        timestamp = event.timestamp()
        
        # Transform to virtual grid coordinates and get position ratios
        virtual_x, virtual_y, x_ratio, y_ratio = self.transform_point(pen_x, pen_y)
        
        # Determine which cell this is in
        cell = self.get_cell_from_position(x_ratio, y_ratio)
        
        # If paused for refresh, ignore input
        if getattr(self, 'is_paused_for_refresh', False):
            return

        # Only record strokes in the current cell (ignore if outside grid)
        # Note: self.current_cell is global index, cell is local grid index
        if cell == -1 or cell != (self.current_cell % self.total_cells):
            return
        
        if event.type() == QEvent.TabletPress:
            # Mark writing start on first stroke
            if self.current_word_data['writing_start'] is None:
                self.current_word_data['writing_start'] = time.time()
                # Also mark reading end (when user starts writing)
                if self.current_word_data['reading_end'] is None:
                    self.current_word_data['reading_end'] = time.time()
                # Mark audio end time only if not already set (from audio finishing naturally)
                if self.pen_recorder.current_word_data and self.pen_recorder.current_word_data['audio_end_time'] is None:
                    self.pen_recorder.set_audio_end()
            
            # Record pen event
            self.pen_recorder.record_event('press', virtual_x, virtual_y, pressure, timestamp)
            
            self.is_drawing = True
            self.current_stroke = [{
                'x': virtual_x,
                'y': virtual_y,
                'pressure': pressure,
                'time': timestamp
            }]
            
        elif event.type() == QEvent.TabletMove and self.is_drawing:
            # Record pen event
            self.pen_recorder.record_event('move', virtual_x, virtual_y, pressure, timestamp)
            
            self.current_stroke.append({
                'x': virtual_x,
                'y': virtual_y,
                'pressure': pressure,
                'time': timestamp
            })
            self.update()
            
        elif event.type() == QEvent.TabletRelease:
            if self.is_drawing and self.current_stroke:
                # Record pen event
                self.pen_recorder.record_event('release', virtual_x, virtual_y, pressure, timestamp)
                
                self.current_stroke.append({
                    'x': virtual_x,
                    'y': virtual_y,
                    'pressure': pressure,
                    'time': timestamp
                })
                self.current_strokes.append(self.current_stroke)
                self.current_stroke = []
                
                # Update writing end time with each completed stroke
                self.current_word_data['writing_end'] = time.time()
            
            self.is_drawing = False
            self.update()
        
        event.accept()
    
    def keyPressEvent(self, event):
        """Handle keyboard events"""
        # Handle Ctrl+R for recalibration
        if event.key() == Qt.Key_R and event.modifiers() == Qt.ControlModifier:
            print("\n⚠ Ctrl+R pressed - initiating recalibration...")
            self.request_recalibration()
            return
        
        # Handle resume from pause
        if getattr(self, 'is_paused_for_refresh', False):
            if event.key() == Qt.Key_Space:
                print("✓ Resuming experiment after refresh")
                self.is_paused_for_refresh = False
                self.page_number += 1
                self.play_current_word()
                self.update()
            return

        if self.proceed_mode == 'key' and event.key() == self.proceed_key:
            print("→ Advancing to next word...")
            self.advance_to_next_word()
        elif event.key() == Qt.Key_Escape:
            # Confirm exit
            reply = QMessageBox.question(
                self,
                'Exit Experiment',
                'Are you sure you want to exit?\nProgress will be saved.',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.finish_experiment()
        event.accept()
    
    def request_recalibration(self):
        """Request recalibration - signal to parent window"""
        # Stop any audio
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass
        
        # Stop timers
        self.audio_monitor_timer.stop()
        self.auto_proceed_timer.stop()
        
        # Signal parent window to start recalibration
        parent = self.parent()
        if parent and hasattr(parent, 'start_recalibration'):
            parent.start_recalibration()
    
    def paintEvent(self, event):
        """Draw the quadrilateral and current strokes"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Fill background
        painter.fillRect(self.rect(), Qt.white)
        
        # Draw quadrilateral using ACTUAL calibrated corners (original points)
        # This matches where the user actually touched the paper corners
        from PyQt5.QtGui import QPolygon
        from PyQt5.QtCore import QPoint
        
        painter.setPen(QPen(Qt.black, 2))
        painter.setBrush(Qt.NoBrush)
        
        polygon = QPolygon([
            QPoint(int(self.calib_tl[0]), int(self.calib_tl[1])),
            QPoint(int(self.calib_tr[0]), int(self.calib_tr[1])),
            QPoint(int(self.calib_br[0]), int(self.calib_br[1])),
            QPoint(int(self.calib_bl[0]), int(self.calib_bl[1]))
        ])
        painter.drawPolygon(polygon)
        
        # Draw current strokes
        painter.setPen(QPen(Qt.black, 2))
        
        for stroke in self.current_strokes:
            if len(stroke) > 1:
                for i in range(len(stroke) - 1):
                    p1 = stroke[i]
                    p2 = stroke[i + 1]
                    painter.drawLine(
                        int(p1['x']), int(p1['y']),
                        int(p2['x']), int(p2['y'])
                    )
        
        # Draw current stroke being drawn
        if self.is_drawing and len(self.current_stroke) > 1:
            painter.setPen(QPen(Qt.blue, 2))
            for i in range(len(self.current_stroke) - 1):
                p1 = self.current_stroke[i]
                p2 = self.current_stroke[i + 1]
                painter.drawLine(
                    int(p1['x']), int(p1['y']),
                    int(p2['x']), int(p2['y'])
                )
        
        # Draw pause overlay if needed
        if getattr(self, 'is_paused_for_refresh', False):
            painter.fillRect(self.rect(), QColor(255, 255, 255, 230))
            painter.setPen(QPen(Qt.red, 1))
            painter.setFont(QFont('Arial', 24, QFont.Bold))
            painter.drawText(self.rect(), Qt.AlignCenter, "Please refresh paper\nand press SPACE to continue")
            return
        
        # Draw instructions at top
        painter.setPen(QPen(Qt.black, 1))
        painter.setFont(QFont('Arial', 14, QFont.Bold))
        
        if self.current_cell < len(self.words):
            word = self.words[self.current_cell]['word']
            key_name = "SPACE"
            if self.proceed_mode == 'key':
                if self.proceed_key == Qt.Key_Space: key_name = "SPACE"
                elif self.proceed_key == Qt.Key_Return: key_name = "ENTER"
                elif self.proceed_key == Qt.Key_Right: key_name = "RIGHT ARROW"
                elif self.proceed_key == Qt.Key_Down: key_name = "DOWN ARROW"
                text = f"Cell {self.current_cell + 1}/{self.total_cells} - Write: '{word}' - Press {key_name} for next"
            else:
                text = f"Cell {self.current_cell + 1}/{self.total_cells} - Write: '{word}' - Auto-advance enabled"
        else:
            text = "Experiment Complete!"
        
        painter.drawText(self.rect(), Qt.AlignTop | Qt.AlignHCenter, text)


class ExperimentWindow(QMainWindow):
    """Main window for the experiment stage"""
    
    def __init__(self, calibration_data, participant_number, config=None, age=None, gender=None):
        super().__init__()
        self.calibration_data = calibration_data
        self.participant_number = participant_number
        self.participant_age = age
        self.participant_gender = gender
        self.config = config
        
        self.setWindowTitle("Tablet Experiment - Experiment Stage")
        
        # Keep window on top of all other windows (including media player)
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        
        self.showFullScreen()
        
        # Create canvas
        self.canvas = ExperimentCanvas(calibration_data, participant_number, config, age, gender)
        self.setCentralWidget(self.canvas)
        
        print("✓ Experiment stage started")
        print("  Press ESC to exit, Ctrl+R to recalibrate")
        
        # Play first word
        self.canvas.play_current_word()
    
    def start_recalibration(self):
        """Start recalibration process"""
        print("⟲ Starting recalibration...")
        self.hide()
        
        # Create new calibration window
        self.recalib_window = CalibrationWindow(self.config)
        # Store current experiment state to resume after recalibration
        self.recalib_window.resume_experiment_data = {
            'participant_number': self.participant_number,
            'age': self.participant_age,
            'gender': self.participant_gender,
            'current_cell': self.canvas.current_cell,
            'pen_recorder': self.canvas.pen_recorder,
            'all_data': self.canvas.all_data,
            'page_number': self.canvas.page_number
        }
        self.recalib_window.show()
    
    def keyPressEvent(self, event):
        """Handle keyboard events at window level"""
        if event.key() == Qt.Key_Escape:
            # Confirm exit
            reply = QMessageBox.question(
                self,
                'Exit Experiment',
                'Are you sure you want to exit?\nProgress will be saved.',
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                self.canvas.finish_experiment()
        else:
            # Pass other keys to canvas
            self.canvas.keyPressEvent(event)
        event.accept()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser()
    parser.add_argument("config_path", nargs="?", help="Path to experiment configuration JSON")
    args = parser.parse_args()
    
    config = None
    if args.config_path and os.path.exists(args.config_path):
        try:
            with open(args.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            config['__file_path__'] = os.path.abspath(args.config_path)
            print(f"✓ Loaded configuration from {args.config_path}")
        except Exception as e:
            print(f"✗ Failed to load config: {e}")
    
    from qt_bootstrap import ensure_qt_platform_plugin_path
    ensure_qt_platform_plugin_path()
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    window = CalibrationWindow(config)
    window.show()
    
    print("Tablet Experiment - Calibration Stage")
    print("Touch and hold (0.5s) any 4 corners of your paper")
    print("Corners will be automatically identified by position")
    print("Tolerance: 15mm deviation allowed")
    
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
