"""
Tablet Writing Experiment with Calibration
Stage 1: Calibrate physical paper grid to virtual canvas
"""

import sys
import math
import os
import subprocess
import argparse
import hashlib
import winsound
from pathlib import Path
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QPushButton, QMessageBox, QInputDialog)
from PyQt5.QtCore import Qt, QTimer, QPointF, QEvent
from PyQt5.QtGui import QPainter, QPen, QColor, QTabletEvent, QFont, QBrush, QKeySequence
from datetime import datetime
import time
import json
import uuid
from component_versions import get_component_version
from runner_launch_contract import read_runtime_session_seed


RUNNER_VERSION = get_component_version("runner")


def _configure_console_output():
    """Keep diagnostic Unicode from crashing Windows legacy consoles."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


_configure_console_output()

# Import AudioProcessor for segment playback
try:
    from audio_processor import AudioProcessor
    HAVE_AUDIO_PROCESSOR = True
except ImportError:
    HAVE_AUDIO_PROCESSOR = False
    print("Warning: AudioProcessor not found (using full files only)")


def _is_manifest(config: dict) -> bool:
    """Detect new-style Experiment Manifest (schema_version + prompts)."""
    return isinstance(config, dict) and 'prompts' in config


def _manifest_to_legacy_config(config_path: str, manifest: dict) -> dict:
    """
    Convert a v1.0 Experiment Manifest into the legacy config structure
    expected by the runner (properties + words grouped by group name).
    Keeps the original manifest info for later export.
    """
    base_dir = os.path.dirname(config_path)

    props = manifest.get('global_parameters', {}) or {}

    properties = {
        'experiment_name': manifest.get('experiment_id', 'experiment'),
        'grid': props.get('grid', {'rows': 5, 'cols': 5}),
        'order': props.get('order', 'random'),
        'repetitions': props.get('repetitions', {}),
        'proceed_condition': props.get('proceed_condition', {
            'type': 'key',
            'key': 'Space',
            'delay_ms': 2000
        }),
        'beeps': props.get('beeps', {
            'before': {'enabled': False, 'delay_ms': 100},
            'after': {'enabled': False, 'delay_ms': 100}
        })
    }

    words_data = {}
    for prompt in manifest.get('prompts', []):
        meta = prompt.get('metadata', {}) or {}
        group_name = meta.get('group', 'default')
        label = prompt.get('label', '')
        audio_path = prompt.get('audio_path')

        # Resolve local relative paths against config directory; keep URLs as-is
        if audio_path:
            if audio_path.startswith(('http://', 'https://')):
                resolved_audio = audio_path
            elif os.path.isabs(audio_path):
                resolved_audio = audio_path
            else:
                resolved_audio = os.path.join(base_dir, audio_path)
        else:
            resolved_audio = ''

        words_data.setdefault(group_name, []).append({
            'word': label,
            'file': resolved_audio
        })

    legacy_config = {
        'properties': properties,
        'words': words_data,
        '__file_path__': config_path,
        '__source_schema_version__': manifest.get('schema_version', '1.0'),
        'experiment_id': manifest.get('experiment_id'),
        'experiment_version': manifest.get('experiment_version')
    }

    for identity_key in (
        'experiment_name',
        'block_name',
        'block_id',
        'block_index',
        'block_count',
    ):
        if identity_key in manifest:
            legacy_config[identity_key] = manifest[identity_key]

    return legacy_config


def load_experiment_config(config_path: str) -> dict:
    """Load one experiment config path, supporting legacy JSON and manifests."""
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, 'r', encoding='utf-8') as f:
        raw_config = json.load(f)

    if _is_manifest(raw_config):
        config = _manifest_to_legacy_config(os.path.abspath(config_path), raw_config)
        config['__manifest__'] = raw_config
        print(f"✓ Loaded manifest (schema {raw_config.get('schema_version', '1.0')}) from {config_path}")
    else:
        config = raw_config
        config['__file_path__'] = os.path.abspath(config_path)
        print(f"✓ Loaded legacy configuration from {config_path}")

    prepare_experiment_runtime(config)

    return config


def _first_identity_value(*values):
    """Return the first identity value that is present and non-empty."""
    for value in values:
        if value is not None and value != '':
            return value
    return None


def _positive_identity_int(value, fallback):
    """Coerce a 1-based index/count, falling back for missing legacy metadata."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed >= 1 else fallback


def get_block_run_identity(config: dict, session_index=0, session_total=1) -> dict:
    """Resolve parent-experiment and block identity for old and new configs.

    New launchers inject the six public identity keys at the top level. The
    nested contexts are accepted for early bundles, while a legacy ZIP whose
    config only has ``name`` is treated as a one-block experiment.
    """
    config = config if isinstance(config, dict) else {}
    experiment_context = config.get('__experiment__')
    if not isinstance(experiment_context, dict):
        experiment_context = {}
    block_context = config.get('__block__')
    if not isinstance(block_context, dict):
        block_context = {}
    properties = config.get('properties')
    if not isinstance(properties, dict):
        properties = {}

    legacy_name = _first_identity_value(
        config.get('name'),
        properties.get('experiment_name'),
    )
    if legacy_name is None and config.get('__file_path__'):
        legacy_name = Path(str(config['__file_path__'])).stem
    legacy_name = str(legacy_name or 'experiment')

    block_name = _first_identity_value(
        config.get('block_name'),
        config.get('__block_name__'),
        block_context.get('name'),
        legacy_name,
    )
    experiment_name = _first_identity_value(
        config.get('experiment_name'),
        config.get('__experiment_name__'),
        config.get('parent_experiment_name'),
        experiment_context.get('name'),
        block_name,
    )
    experiment_id = _first_identity_value(
        config.get('experiment_id'),
        config.get('__experiment_id__'),
        config.get('parent_experiment_id'),
        experiment_context.get('id'),
        experiment_name,
    )
    block_id = _first_identity_value(
        config.get('block_id'),
        config.get('__block_id__'),
        block_context.get('id'),
    )

    default_index = max(1, int(session_index or 0) + 1)
    default_count = max(default_index, int(session_total or 1))
    block_index = _positive_identity_int(
        _first_identity_value(
            config.get('block_index'),
            config.get('__block_index__'),
            block_context.get('index'),
        ),
        default_index,
    )
    block_count = _positive_identity_int(
        _first_identity_value(
            config.get('block_count'),
            config.get('__block_count__'),
            experiment_context.get('block_count'),
        ),
        default_count,
    )
    block_count = max(block_count, block_index)

    return {
        'experiment_name': str(experiment_name),
        'experiment_id': experiment_id,
        'block_name': str(block_name),
        'block_id': block_id,
        'block_index': block_index,
        'block_count': block_count,
    }


def ensure_shared_run_session_id(configs, participant_number) -> str:
    """Assign one run/session id to every block config in an experiment run."""
    usable_configs = [config for config in (configs or []) if isinstance(config, dict)]
    existing = next(
        (
            config.get('__run_session_id__')
            for config in usable_configs
            if config.get('__run_session_id__')
        ),
        None,
    )
    session_id = existing or (
        f"{participant_number}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:6]}"
    )
    for config in usable_configs:
        config['__run_session_id__'] = session_id
    return session_id


def initialize_cloud_run(configs, participant_number, age, gender, test_mode=False):
    """Create one server Run before the first Block starts; local files remain a fallback."""
    if test_mode or not configs:
        return None
    existing = next((config.get('__server_run_id__') for config in configs if config.get('__server_run_id__')), None)
    if existing:
        return existing
    revision_id = configs[0].get('experiment_revision_id')
    experiment_id = configs[0].get('experiment_id')
    try:
        uuid.UUID(str(revision_id))
        uuid.UUID(str(experiment_id))
    except (ValueError, TypeError, AttributeError):
        return None
    session_id = ensure_shared_run_session_id(configs, participant_number)
    try:
        from autoscript_api import AutoScriptAPI
        api = AutoScriptAPI()
        run = api.create_run(
            revision_id, session_id, participant_number, age, gender
        )
        api.start_run(run['id'])
        for config in configs:
            config['__server_run_id__'] = run['id']
        return run['id']
    except Exception as exc:
        print(f"Cloud Run initialization failed; results will use the durable queue: {exc}")
        return None


def queue_cloud_run_failure(configs, api_factory=None):
    """Best-effort durable failure reporting for an uncaught Runner exception."""
    run_ids = {
        str(config.get('__server_run_id__'))
        for config in (configs or [])
        if isinstance(config, dict) and config.get('__server_run_id__')
    }
    if not run_ids:
        return 0, []
    from autoscript_api import AutoScriptAPI
    from result_upload_queue import drain_upload_queue, enqueue_transition

    for run_id in run_ids:
        enqueue_transition(run_id, 'fail')
    try:
        api = api_factory() if api_factory is not None else AutoScriptAPI()
        return drain_upload_queue(api)
    except Exception as exc:
        return 0, [f"Cloud connection: {exc}"]


def _make_word_shuffle_rng(config: dict, session_seed=None):
    """Create a reproducible RNG for one config when a session seed is available."""
    import random

    if not session_seed:
        return random.Random()

    seed_source = f"{session_seed}::{config.get('__file_path__', '')}"
    digest = hashlib.sha256(seed_source.encode('utf-8')).hexdigest()
    return random.Random(int(digest[:16], 16))


def _shuffle_words_with_spacing(word_pool, rng=None):
    """Shuffle words while ensuring identical prompts are spaced apart."""
    import random
    from collections import defaultdict

    rng = rng or random.Random()

    if not word_pool:
        return []

    word_groups = defaultdict(list)
    for word_data in word_pool:
        word_groups[word_data['word']].append(word_data)

    if all(len(group) == 1 for group in word_groups.values()):
        shuffled = word_pool.copy()
        rng.shuffle(shuffled)
        return shuffled

    result = []
    available_groups = {word: group.copy() for word, group in word_groups.items()}

    while any(available_groups.values()):
        recent_words = set()
        lookback = min(5, len(result))
        if result:
            recent_words = {result[-i]['word'] for i in range(1, lookback + 1) if i <= len(result)}

        available_now = [
            word for word, group in available_groups.items()
            if group and word not in recent_words
        ]
        if not available_now:
            available_now = [word for word, group in available_groups.items() if group]
        if not available_now:
            break

        chosen_word = rng.choice(available_now)
        word_data = available_groups[chosen_word].pop(0)
        result.append(word_data)

        if not available_groups[chosen_word]:
            del available_groups[chosen_word]

    return result


def _build_word_sequence(config: dict, session_seed=None):
    """Build the full prompt sequence once so startup and resume reuse the same order."""
    if not config:
        return []

    words = []
    rng = _make_word_shuffle_rng(config, session_seed=session_seed)
    base_dir = os.path.dirname(config.get('__file_path__', '.'))

    if 'groups' in config and isinstance(config['groups'], list):
        file_map = {}
        for f_entry in config.get('files', []):
            fname = f_entry.get('file_name', '')
            rel_path = f_entry.get('path', '')
            abs_path = os.path.join(base_dir, rel_path)
            if fname:
                file_map[fname] = abs_path
            orig = f_entry.get('original_name', '')
            if orig:
                file_map[orig] = abs_path
            file_map[rel_path] = abs_path

        word_lookup = {}
        for grp in config.get('groups', []):
            grp_name = grp.get('name', 'Default')
            for word_entry in grp.get('words', []):
                src_ref = word_entry.get('source_file')
                active_file = file_map.get(src_ref)
                if not active_file and src_ref and os.path.exists(os.path.join(base_dir, 'media', src_ref)):
                    active_file = os.path.join(base_dir, 'media', src_ref)

                word_lookup[word_entry.get('id')] = {
                    'id': word_entry.get('id'),
                    'word': word_entry.get('text', ''),
                    'group': grp_name,
                    'file': active_file,
                    'start_ms': word_entry.get('start_ms'),
                    'end_ms': word_entry.get('end_ms')
                }

        order_type = config.get('order', 'random')
        if order_type == 'stiff':
            for word_id in config.get('sequence', []):
                if word_id in word_lookup:
                    words.append(word_lookup[word_id].copy())
                else:
                    print(f"Warning: Sequence ID {word_id} not found in groups.")
        elif order_type == 'random':
            repetitions = config.get('repetitions', {})
            pool = []
            for grp in config.get('groups', []):
                grp_name = grp.get('name', 'Default')
                count = repetitions.get(grp_name, 1)
                grp_words = []
                for word_entry in grp.get('words', []):
                    if word_entry.get('id') in word_lookup:
                        grp_words.append(word_lookup[word_entry.get('id')])
                for _ in range(count):
                    for word_data in grp_words:
                        pool.append(word_data.copy())
            words = _shuffle_words_with_spacing(pool, rng=rng)
    else:
        words_data = config.get('words', {})
        audio_dir = os.path.join(base_dir, 'audio')
        repetitions = config.get('properties', {}).get('repetitions', {})
        order = config.get('properties', {}).get('order', 'random')

        unique_words = []
        for group_name, word_list in words_data.items():
            for word_entry in word_list:
                file_ref = word_entry.get('file', '')
                if file_ref.startswith(('http://', 'https://')):
                    word_file = file_ref
                elif os.path.isabs(file_ref):
                    word_file = file_ref
                else:
                    word_file = os.path.join(audio_dir, file_ref)
                unique_words.append({
                    'file': word_file,
                    'word': word_entry['word'],
                    'group': group_name
                })

        if order == 'random':
            word_pool = []
            for word_data in unique_words:
                repeat_count = repetitions.get(word_data['group'], 1)
                for _ in range(repeat_count):
                    word_pool.append(word_data.copy())
            words = _shuffle_words_with_spacing(word_pool, rng=rng)
        else:
            max_repeats = max(repetitions.values()) if repetitions else 1
            for rep in range(max_repeats):
                for word_data in unique_words:
                    group_repeats = repetitions.get(word_data['group'], 1)
                    if rep < group_repeats:
                        words.append(word_data.copy())

    return words


def _warm_first_prompt_audio(config: dict):
    """Precompute the first prompt playback file so session start can be immediate."""
    prepared_words = config.get('__prepared_words__') or []
    if not prepared_words:
        return

    first_word = prepared_words[0]
    source_audio = first_word.get('file')
    if not isinstance(source_audio, str) or not source_audio:
        return
    if source_audio.lower().startswith(('http://', 'https://')):
        return

    if source_audio.lower().endswith('.m4a'):
        wav_file = source_audio[:-4] + '.wav'
        if os.path.exists(wav_file):
            source_audio = wav_file

    if not os.path.exists(source_audio):
        return

    prepared_audio_file = os.path.abspath(source_audio)
    start_ms = first_word.get('start_ms')
    end_ms = first_word.get('end_ms')
    needs_processed_playback = (
        (start_ms is not None and end_ms is not None) or
        not prepared_audio_file.lower().endswith('.wav')
    )

    if needs_processed_playback and HAVE_AUDIO_PROCESSOR:
        try:
            processor = AudioProcessor(verbose=False)
            playback_file = processor.get_playback_file(
                prepared_audio_file,
                start_ms,
                end_ms,
                context='startup'
            )
            if playback_file and os.path.exists(playback_file):
                prepared_audio_file = os.path.abspath(playback_file)
        except Exception as e:
            print(f"⚠ Failed to warm first prompt audio: {e}")

    first_word['prepared_audio_file'] = prepared_audio_file


def prepare_experiment_runtime(config: dict):
    """Freeze runtime word order and warm first-prompt audio ahead of calibration."""
    if not config:
        return config

    config_path = config.get('__file_path__')
    session_seed = read_runtime_session_seed(config_path) if config_path else None
    config['__session_seed__'] = session_seed

    prepared_seed = config.get('__prepared_words_seed__')
    if config.get('__prepared_words__') is None or prepared_seed != session_seed:
        config['__prepared_words__'] = _build_word_sequence(config, session_seed=session_seed)
        config['__prepared_words_seed__'] = session_seed
        config.pop('__preloaded_first_audio_seed__', None)

    if config.get('__preloaded_first_audio_seed__') != session_seed:
        _warm_first_prompt_audio(config)
        config['__preloaded_first_audio_seed__'] = session_seed

    return config


def _default_session_layout(index: int = 0) -> dict:
    """Return the fallback runner layout when no launcher plan is supplied."""
    return {
        'start_cell_offset': 0,
        'same_page_as_previous': False,
        'recalibrate_before_start': index > 0,
        'recalibrate_during_page_refresh': False,
    }


def load_session_plan(plan_path: str):
    """Load the launcher-produced session plan JSON if one was provided."""
    if not plan_path:
        return None

    with open(plan_path, 'r', encoding='utf-8') as handle:
        return json.load(handle)


def apply_session_plan(configs, session_plan=None):
    """Attach per-config page-layout metadata from the launcher session plan."""
    plan_by_path = {}
    session_recalibrate_between_pages = bool((session_plan or {}).get('recalibrate_between_pages', False))
    if session_plan:
        for entry in session_plan.get('experiments', []):
            config_path = entry.get('config_path')
            if config_path:
                plan_by_path[os.path.abspath(config_path)] = dict(entry)

    for index, config in enumerate(configs):
        config_path = os.path.abspath(config.get('__file_path__', '')) if config.get('__file_path__') else ''
        layout = _default_session_layout(index)
        if config_path in plan_by_path:
            layout.update({
                'display_name': plan_by_path[config_path].get('display_name'),
                'start_cell_offset': int(plan_by_path[config_path].get('start_cell_offset', 0) or 0),
                'same_page_as_previous': bool(plan_by_path[config_path].get('same_page_as_previous', False)),
                'recalibrate_before_start': bool(plan_by_path[config_path].get('recalibrate_before_start', False)),
                'recalibrate_during_page_refresh': bool(plan_by_path[config_path].get('recalibrate_during_page_refresh', False)),
            })

        config['__session_layout__'] = layout
        config['__session_recalibrate_between_pages__'] = session_recalibrate_between_pages

    return configs


class PenDataRecorder:
    """Records pen movement data (position, pressure, speed, etc.)"""
    
    def __init__(self):
        """Initialize pen data recorder"""
        self.all_word_data = []  # List of all word recordings
        self.current_word_data = None
        self.last_point = None
        self.last_time = None
        self._word_motion_state = {}
        self._finished_word_ids = set()
    
    def create_word_record(self, word_info):
        """Create a word recording without making it the only active record."""
        word_data = {
            'word': word_info['word'],
            'cell': word_info['cell'],
            'group': word_info.get('group', 'unknown'),  # Group name
            'start_time': time.time(),
            'end_time': None,
            'audio_start_time': None,  # When audio starts playing
            'audio_end_time': None,    # When audio finishes playing
            'pen_events': []  # List of pen events with full data
        }
        self._word_motion_state[id(word_data)] = {
            'last_point': None,
            'last_time': None
        }
        return word_data
    
    def start_word(self, word_info):
        """Start recording a new word"""
        self.current_word_data = self.create_word_record(word_info)
        self.last_point = None
        self.last_time = None
        print(f"🖊 Recording pen data for word: '{word_info['word']}' (group: {word_info.get('group', 'unknown')})")
    
    def record_event_for_word(self, word_data, event_type, x, y, pressure, timestamp):
        """Record a pen event into a specific word record."""
        if not word_data:
            return
        
        current_time = time.time()
        state = self._word_motion_state.setdefault(id(word_data), {
            'last_point': None,
            'last_time': None
        })
        
        speed = 0.0
        if state['last_point'] and state['last_time']:
            dx = x - state['last_point'][0]
            dy = y - state['last_point'][1]
            distance = math.sqrt(dx*dx + dy*dy)
            time_delta = current_time - state['last_time']
            if time_delta > 0:
                speed = distance / time_delta
        
        event_data = {
            'type': event_type,
            'x': x,
            'y': y,
            'pressure': pressure,
            'timestamp': timestamp,
            'absolute_time': current_time,
            'speed': speed
        }
        
        word_data['pen_events'].append(event_data)
        state['last_point'] = (x, y)
        state['last_time'] = current_time
    
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
    
    def set_audio_start_for_word(self, word_data):
        """Mark when audio starts playing for a specific word record."""
        if word_data and word_data['audio_start_time'] is None:
            word_data['audio_start_time'] = time.time()
    
    def set_audio_end_for_word(self, word_data):
        """Mark when audio finishes playing for a specific word record."""
        if word_data and word_data['audio_end_time'] is None:
            word_data['audio_end_time'] = time.time()
    
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
            self.end_word_record(self.current_word_data)
            self.current_word_data = None
            self.last_point = None
            self.last_time = None
    
    def end_word_record(self, word_data):
        """Finish and store a specific word record once."""
        if not word_data:
            return
        
        word_id = id(word_data)
        if word_id in self._finished_word_ids:
            return
        
        word_data['end_time'] = time.time()
        self.all_word_data.append(word_data)
        self._finished_word_ids.add(word_id)
        self._word_motion_state.pop(word_id, None)
        print(f"✓ Recorded {len(word_data['pen_events'])} pen events")
    
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
    
    def __init__(self, parent=None, test_mode=False):
        super().__init__(parent)
        # No minimum size - will fill entire screen
        self.setStyleSheet("background-color: white;")
        self.test_mode = test_mode
        
        # Reference to main window
        self.main_window = None
        
        # Calibration state
        self.calibration_points = []  # Will store 4 corner points (any order)
        self.current_step = 0  # Number of corners captured (0-3)
        
        # Tablet state
        self.pen_x = 0
        self.pen_y = 0
        self.pen_touching = False
        self.tablet_press_active = False
        self.touch_start_time = None
        self.touch_recorded = False  # Track if current touch already recorded
        
        # Enable tablet tracking
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_TabletTracking, True)

    def _record_calibration_point(self):
        self.calibration_points.append((self.pen_x, self.pen_y))
        self.current_step += 1
        self.touch_recorded = True

        if self.main_window:
            self.main_window.update_calibration_status()

        print(f"✓ Recorded corner {self.current_step}/4: ({self.pen_x:.1f}, {self.pen_y:.1f})")

        if self.current_step == 4 and self.main_window:
            self.main_window.calibration_complete()

    def _record_touch_if_ready(self, now=None):
        if self.touch_recorded or self.current_step >= 4 or not self.touch_start_time:
            return False

        now = now or time.time()
        if now - self.touch_start_time < 0.5:
            return False

        self._record_calibration_point()
        return True
    
    def tabletEvent(self, event: QTabletEvent):
        """Handle tablet events"""
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
            self.tablet_press_active = True
            self.touch_start_time = time.time()
            self.touch_recorded = False
            print(f"  Press detected at ({self.pen_x:.1f}, {self.pen_y:.1f})")
            
        elif event_type == QTabletEvent.TabletMove:
            # A calibration touch must begin with a new TabletPress delivered to
            # this canvas.  When a later calibration window receives focus, some
            # tablet drivers continue sending pressured move events from the
            # previous experiment.  Treating those moves as a new press records a
            # phantom first corner and makes an otherwise valid rectangle fail.
            if self.tablet_press_active and pressure > 0.01:
                self.pen_touching = True

                # Check if we've held long enough (check during move)
                self._record_touch_if_ready()
            elif pressure <= 0.01:
                self.pen_touching = False
                self.tablet_press_active = False
                self.touch_start_time = None
                self.touch_recorded = False
                
        elif event_type == QTabletEvent.TabletRelease:
            if self.tablet_press_active:
                self._record_touch_if_ready()
            # Pen released - just reset state
            print(f"  Release detected")
            self.pen_touching = False
            self.tablet_press_active = False
            self.touch_start_time = None
            self.touch_recorded = False
        
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if not self.test_mode or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        global_pos = event.globalPos()
        self.pen_x = global_pos.x()
        self.pen_y = global_pos.y()
        self.pen_touching = True
        self.touch_start_time = time.time()
        self.touch_recorded = False
        print(f"  Mouse press detected at ({self.pen_x:.1f}, {self.pen_y:.1f})")
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.test_mode:
            return super().mouseMoveEvent(event)

        global_pos = event.globalPos()
        self.pen_x = global_pos.x()
        self.pen_y = global_pos.y()
        if event.buttons() & Qt.LeftButton:
            self.pen_touching = True
            self._record_touch_if_ready()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self.test_mode or event.button() != Qt.LeftButton:
            return super().mouseReleaseEvent(event)

        global_pos = event.globalPos()
        self.pen_x = global_pos.x()
        self.pen_y = global_pos.y()
        self._record_touch_if_ready()
        print("  Mouse release detected")
        self.pen_touching = False
        self.touch_start_time = None
        self.touch_recorded = False
        self.update()
        event.accept()
    
    def paintEvent(self, event):
        """Draw the canvas with calibration points"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
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
            if self.test_mode:
                step_text = f"Click {self.current_step + 1}/4: HOLD any paper corner (0.5s)"
            else:
                step_text = f"Touch {self.current_step + 1}/4: HOLD any paper corner (0.5s)"
        else:
            step_text = "All 4 corners captured! Press 'V' to validate or 'R' to reset"
        
        painter.drawText(box_x + 10, box_y + 35, box_width - 20, 30, Qt.AlignCenter, step_text)
        
        painter.setFont(QFont("Arial", 12))
        painter.setPen(QPen(QColor(100, 100, 100)))
        detail_text = "Mouse test mode | ESC=exit | R=reset | V=validate" if self.test_mode else "Corners auto-detected by position | ESC=exit | R=reset | V=validate"
        painter.drawText(box_x + 10, box_y + 60, box_width - 20, 30, Qt.AlignCenter, 
                detail_text)
        
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
        self.pen_touching = False
        self.tablet_press_active = False
        self.touch_start_time = None
        self.touch_recorded = False
        self.update()


class CalibrationWindow(QMainWindow):
    """Main calibration window"""
    
    def __init__(self, config=None, session_configs=None, session_config_index=0,
                 session_results=None, test_mode=False,
                 show_start_screen_after_calibration=True):
        super().__init__()
        self.session_configs = list(session_configs) if session_configs else ([config] if config else [])
        self.session_config_index = session_config_index
        self.session_results = session_results if session_results is not None else []
        self.config = config or (self.session_configs[0] if self.session_configs else None)
        self.test_mode = test_mode
        self.show_start_screen_after_calibration = show_start_screen_after_calibration
        self.resume_experiment_data = None  # For recalibration resume
        self.pending_participant_number = None
        self.pending_participant_age = None
        self.pending_participant_gender = None
        self.init_ui()
    
    def init_ui(self):
        """Initialize the user interface"""
        title = "Tablet Experiment - Calibration (Test Mode)" if self.test_mode else "Tablet Experiment - Calibration"
        self.setWindowTitle(title)
        
        # Make window fullscreen
        self.showFullScreen()
        
        # Canvas as the only central widget (fullscreen)
        self.canvas = CalibrationCanvas(self, test_mode=self.test_mode)
        self.canvas.main_window = self
        self.setCentralWidget(self.canvas)
        
        # Enable tablet events
        self.setAttribute(Qt.WA_TabletTracking, True)
    
    def keyPressEvent(self, event):
        """Handle keyboard events"""
        if event.key() == Qt.Key_Escape:
            # ESC to exit fullscreen or close
            self.close()
        elif event.key() == Qt.Key_R:
            # R to reset calibration
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
            msg.setText("✓ Calibration successful!")
            msg.exec_()

            self.prepare_experiment_start()
            
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
    
    def prepare_experiment_start(self):
        """Collect required data, then open the experiment stage."""
        
        if (
            not (hasattr(self, 'resume_experiment_data') and self.resume_experiment_data)
            and self.pending_participant_number is None
        ):
            participant_number, ok = QInputDialog.getInt(
                self,
                'Participant Number',
                'Enter participant number:',
                value=1,
                min=1,
                max=9999
            )
            
            if not ok:
                return
            
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
            
            self.pending_participant_number = participant_number
            self.pending_participant_age = age
            self.pending_participant_gender = gender
        
        print("✓ Participant details collected - opening experiment stage")
        self.start_experiment()
    
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
                resume_data['gender'],
                auto_start=False,
                show_start_screen=False,
                session_configs=self.session_configs,
                session_config_index=self.session_config_index,
                session_results=self.session_results,
                test_mode=self.test_mode,
                start_cell_offset=resume_data.get('start_cell_offset', 0),
                initial_page_number=resume_data.get('page_number', 1)
            )
            # Restore experiment state
            self.experiment_window.canvas.current_cell = resume_data['current_cell']
            self.experiment_window.canvas.start_cell_offset = resume_data.get('start_cell_offset', 0)
            self.experiment_window.canvas.pen_recorder = resume_data['pen_recorder']
            self.experiment_window.canvas.all_data = resume_data['all_data']
            self.experiment_window.canvas.page_number = resume_data['page_number']
            self.experiment_window.canvas.time_mode_page_start = resume_data.get('time_mode_page_start')
            self.experiment_window.canvas.time_mode_word_records = resume_data.get('time_mode_word_records', {})
            self.experiment_window.canvas.time_mode_strokes_by_cell = resume_data.get('time_mode_strokes_by_cell', {})
            self.experiment_window.canvas.current_stroke_cell = resume_data.get('current_stroke_cell')
            self.experiment_window.canvas.current_stroke = resume_data.get('current_stroke', [])
            self.experiment_window.canvas.is_drawing = resume_data.get('is_drawing', False)
            self.experiment_window.canvas.pending_time_mode_page_refresh = resume_data.get('pending_time_mode_page_refresh', False)
            self.experiment_window.canvas.pending_time_mode_finish = resume_data.get('pending_time_mode_finish', False)
            self.experiment_window.show()
            
            # Continue playing current word
            self.experiment_window.canvas.play_current_word()
            print(f"✓ Resumed experiment at word {resume_data['current_cell'] + 1}")
            return

        participant_number = self.pending_participant_number
        age = self.pending_participant_age
        gender = self.pending_participant_gender
        if participant_number is None:
            self.prepare_experiment_start()
            return
        
        self.hide()  # Hide calibration window
        
        # Create and show experiment window with age and gender
        self.experiment_window = ExperimentWindow(
            self.calibration_data,
            participant_number,
            self.config,
            age,
            gender,
            show_start_screen=self.show_start_screen_after_calibration,
            session_configs=self.session_configs,
            session_config_index=self.session_config_index,
            session_results=self.session_results,
            test_mode=self.test_mode
        )
        self.experiment_window.show()


class ExperimentCanvas(QWidget):
    """Canvas for the main experiment with configurable grid"""
    
    def __init__(self, calibration_data, participant_number, config=None, age=None, gender=None,
                 parent=None, session_index=0, session_total=1, test_mode=False,
                 start_cell_offset=0, initial_page_number=1):
        super().__init__(parent)
        self.calibration_data = calibration_data
        self.participant_number = participant_number
        self.config = config
        self.test_mode = test_mode
        self._cloud_upload_outcome = None
        self.participant_age = age
        self.participant_gender = gender
        self.session_index = session_index
        self.session_total = session_total
        self.current_cell = 0
        self.session_layout = (self.config or {}).get('__session_layout__', {}) if self.config else {}
        self.start_cell_offset = max(0, int(self.session_layout.get('start_cell_offset', start_cell_offset) or 0))
        self.recalibrate_during_page_refresh = bool(self.session_layout.get('recalibrate_during_page_refresh', False))
        
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

        self.run_identity = get_block_run_identity(
            self.config,
            session_index=self.session_index,
            session_total=self.session_total,
        )
        self.exp_name = self.run_identity['block_name']
            
        self.grid_size = self.grid_rows # For compatibility with some methods, though we should use rows/cols
        self.total_cells = self.grid_rows * self.grid_cols
        
        self.current_strokes = []  # Strokes for current cell
        # Currently unused in final export. Kept for possible detailed stroke/timing export
        # and restored during recalibration resume.
        self.all_data = []
        self.time_mode_page_start = None
        self.time_mode_word_records = {}
        self.time_mode_strokes_by_cell = {}
        self.current_stroke_cell = None
        self.pending_time_mode_page_refresh = False
        self.pending_time_mode_finish = False
        self.waiting_for_save_spacebar = False
        self.waiting_for_next_experiment_spacebar = False
        self.completed_data = None
        self.waiting_for_experiment_start = False
        self.starting_experiment_after_space = False
        self.start_gate_after_key_definition = False
        
        # Pagination state
        self.page_number = max(1, int(initial_page_number or 1))
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
        
        # Audio slicing support
        self.processor = AudioProcessor(verbose=False) if HAVE_AUDIO_PROCESSOR else None
        
        # Special State: Waiting for Key definition
        self.waiting_for_proceed_key = (self.proceed_mode == 'key')
        self.proceed_key_name = "Space" # Default display name
        
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
        self._ensure_audio_backend_ready()
        
    def load_words(self):
        """Load words from all supported config formats."""
        self.words = []
        if not self.config:
            print("✗ No experiment configuration provided.")
            return

        # --- A. DETECT NEW JSON FORMAT ---
        if 'groups' in self.config and isinstance(self.config['groups'], list):
            # Parse Properties
            self.exp_name = self.config.get('name', 'My Experiment')
            
            grid = self.config.get('grid', {})
            self.grid_rows = grid.get('rows', 5)
            self.grid_cols = grid.get('cols', 5)
            
            # Note: Active proceeding condition logic will be handled outside load_words
            proceed = self.config.get('proceed_condition', {})
            self.proceed_mode = proceed.get('type', 'key')
            self.proceed_delay = proceed.get('delay_ms', 2000)

            beeps = self.config.get('beeps', {})
            self.beep_before = beeps.get('before', {}).get('enabled', False)
            self.beep_before_delay = beeps.get('before', {}).get('delay_ms', 100)
            self.beep_after = beeps.get('after', {}).get('enabled', False)
            self.beep_after_delay = beeps.get('after', {}).get('delay_ms', 100)

        session_seed = self.config.get('__session_seed__')
        prepared_words = self.config.get('__prepared_words__')
        if prepared_words is None or self.config.get('__prepared_words_seed__') != session_seed:
            prepared_words = _build_word_sequence(self.config, session_seed=session_seed)
            self.config['__prepared_words__'] = [word.copy() for word in prepared_words]
            self.config['__prepared_words_seed__'] = session_seed

        self.words = [word.copy() for word in (prepared_words or [])]
            
        # Common Finalization
        self.grid_size = self.grid_rows # For compatibility with some methods, though we should use rows/cols
        self.total_cells = self.grid_rows * self.grid_cols
        
        self.exp_name = get_block_run_identity(
            self.config,
            session_index=self.session_index,
            session_total=self.session_total,
        )['block_name']
        print(f"✓ Loaded {len(self.words)} words for block")
    
    
    def _shuffle_with_spacing(self, word_pool):
        """
        Shuffle words while ensuring same words are spaced apart.
        Uses a greedy algorithm to maximize spacing between identical words.
        """
        return _shuffle_words_with_spacing(word_pool)

    def _current_experiment_display_name(self):
        """Return the current block label shown in the session banner."""
        display_name = self.session_layout.get('display_name') if self.session_layout else None
        if display_name:
            return str(display_name)

        if self.config and self.config.get('__file_path__'):
            return Path(self.config['__file_path__']).stem

        return self.exp_name or 'experiment'

    def _current_word_progress_text(self):
        """Return the 1-based word progress for the current block."""
        total_words = len(self.words)
        if total_words <= 0:
            return '0/0'

        word_number = min(max(self.current_cell, 0), total_words - 1) + 1
        return f"{word_number}/{total_words}"

    def _current_session_experiment_text(self):
        """Return the current block index/count (legacy method name)."""
        identity = get_block_run_identity(
            self.config,
            session_index=self.session_index,
            session_total=self.session_total,
        )
        return f"{identity['block_index']}/{identity['block_count']}"

    def _current_heading_prefix(self):
        """Return the shared heading prefix for the current block state."""
        return (
            f"{self._current_experiment_display_name()} "
            f"({self._current_session_experiment_text()}) - "
            f"Word {self._current_word_progress_text()}"
        )

    def _absolute_cell_index(self, word_index=None):
        """Return the absolute cell position for a word index within this experiment."""
        if word_index is None:
            word_index = self.current_cell
        return self.start_cell_offset + word_index

    def _current_page_cell_index(self, word_index=None):
        """Return the visible cell index on the current page."""
        return self._absolute_cell_index(word_index) % self.total_cells

    def _is_page_boundary_after_index(self, word_index=None):
        """Return True when a word index begins a fresh physical page."""
        absolute_index = self._absolute_cell_index(word_index)
        return absolute_index > 0 and absolute_index % self.total_cells == 0

    def _should_recalibrate_for_page_refresh(self):
        """Return True when the session plan requires a fresh calibration on page turns."""
        return self.recalibrate_during_page_refresh
    
    def _is_time_mode(self):
        """Return True when prompts advance by timer instead of a participant key."""
        return self.proceed_mode == 'time'
    
    def _get_page_start(self):
        """Return the global word index of the first cell on the visible page."""
        return (self._absolute_cell_index() // self.total_cells) * self.total_cells
    
    def _ensure_time_mode_page_records(self):
        """Create one active word record per visible cell for time-advance mode."""
        if not self._is_time_mode():
            return
        
        page_start = self._get_page_start()
        if self.time_mode_page_start == page_start and self.time_mode_word_records:
            return
        
        if self.time_mode_word_records:
            self._finalize_time_mode_page()
        
        self.time_mode_page_start = page_start
        self.time_mode_word_records = {}
        self.time_mode_strokes_by_cell = {}
        self.current_stroke_cell = None
        
        page_end = page_start + self.total_cells
        for global_idx, word_data in enumerate(self.words):
            absolute_idx = self._absolute_cell_index(global_idx)
            if absolute_idx < page_start or absolute_idx >= page_end:
                continue

            local_cell = absolute_idx % self.total_cells
            self.time_mode_word_records[local_cell] = self.pen_recorder.create_word_record({
                'word': word_data['word'],
                'cell': absolute_idx,
                'group': word_data.get('group', 'unknown')
            })
            self.time_mode_strokes_by_cell[local_cell] = []
    
    def _time_mode_record_for_cell(self, local_cell):
        """Return the word record assigned to a visible local cell."""
        if self.time_mode_word_records:
            return self.time_mode_word_records.get(local_cell)
        
        self._ensure_time_mode_page_records()
        return self.time_mode_word_records.get(local_cell)
    
    def _current_time_mode_word_record(self):
        """Return the word record for the currently playing prompt."""
        if not self._is_time_mode():
            return None
        return self._time_mode_record_for_cell(self._current_page_cell_index())
    
    def _finalize_time_mode_page(self):
        """Finish every word record on the active time-mode page."""
        if not self.time_mode_word_records:
            return
        
        for local_cell in sorted(self.time_mode_word_records):
            self.pen_recorder.end_word_record(self.time_mode_word_records[local_cell])
        
        self.time_mode_word_records = {}
        self.time_mode_strokes_by_cell = {}
        self.current_stroke = []
        self.current_stroke_cell = None
        self.is_drawing = False
        self.pending_time_mode_page_refresh = False
    
    def _complete_pending_time_mode_transition(self):
        """Finish a deferred page transition after an in-progress stroke ends."""
        if self.pending_time_mode_finish:
            self.pending_time_mode_finish = False
            self.wait_for_save_spacebar()
            return
        
        if self.pending_time_mode_page_refresh:
            print("⚠ Page full - pausing for refresh")
            self._finalize_time_mode_page()
            self.is_paused_for_refresh = True
            self.update()
    
    def request_initial_start_screen(self):
        """Show the experiment-stage Space gate before the first prompt starts."""
        if self.waiting_for_proceed_key:
            self.start_gate_after_key_definition = True
            self.update()
            return
        
        self.show_experiment_start_screen()
    
    def show_experiment_start_screen(self):
        """Wait for Space inside the experiment window before starting audio."""
        self.waiting_for_experiment_start = True
        self.starting_experiment_after_space = False
        self.update()
    
    def begin_after_start_screen(self):
        """Start playback as soon as the initial Space gate is cleared."""
        self.waiting_for_experiment_start = False
        self.starting_experiment_after_space = False
        
        if self.waiting_for_proceed_key:
            self.update()
            return
        
        self.play_current_word()
        self.update()

    def _ensure_audio_backend_ready(self):
        """Initialize pygame's mixer before the user reaches the first start prompt."""
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
        except Exception:
            pass
    
    def _audio_is_playing(self):
        """Return True while pygame is still playing the current prompt."""
        try:
            import pygame
            return bool(pygame.mixer.get_init() and pygame.mixer.music.get_busy())
        except Exception:
            return False
    
    def wait_for_save_spacebar(self):
        """Enter the end-wait state while keeping pen input active until Space."""
        if self.session_index < self.session_total - 1:
            self.waiting_for_next_experiment_spacebar = True
        else:
            self.waiting_for_save_spacebar = True
        self.auto_proceed_timer.stop()
        if not self._audio_is_playing():
            self.audio_monitor_timer.stop()
        self.update()
    
    def check_audio_finished(self):
        """Check if audio has finished playing"""
        try:
            import pygame
            if pygame.mixer.get_init() and not pygame.mixer.music.get_busy():
                if self._is_time_mode():
                    word_record = self._current_time_mode_word_record()
                    if word_record and word_record['audio_end_time'] is None:
                        self.pen_recorder.set_audio_end_for_word(word_record)
                        print(f"♫ Audio finished playing")
                        
                        if self.beep_after:
                            QTimer.singleShot(self.beep_after_delay, lambda: winsound.Beep(800, 150))
                        
                        delay = self.proceed_delay
                        if self.beep_after:
                            delay += self.beep_after_delay + 150
                        print(f"⏳ Auto-advancing in {delay}ms...")
                        self.auto_proceed_timer.start(delay)
                        
                    self.audio_monitor_timer.stop()
                    self.update()
                    return
                
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
                self.update()
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

    def _is_url(self, path):
        """Return True for HTTP(S) audio references."""
        return isinstance(path, str) and path.lower().startswith(('http://', 'https://'))

    def _download_audio_url(self, audio_url):
        """Download a URL audio prompt to a reusable local cache file."""
        try:
            import hashlib
            import urllib.parse
            import urllib.request
            from app_paths import ensure_dir, user_data_dir

            parsed = urllib.parse.urlparse(audio_url)
            suffix = os.path.splitext(parsed.path)[1] or '.audio'
            cache_name = hashlib.sha256(audio_url.encode('utf-8')).hexdigest()[:24] + suffix
            cache_path = ensure_dir(user_data_dir() / 'temp' / 'audio_url_cache') / cache_name

            if cache_path.exists() and cache_path.stat().st_size > 0:
                return str(cache_path)

            print(f"Downloading audio prompt: {audio_url}")
            request = urllib.request.Request(audio_url, headers={'User-Agent': 'AutoScript/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response:
                with open(cache_path, 'wb') as f:
                    f.write(response.read())

            return str(cache_path)
        except Exception as e:
            print(f"Error downloading audio URL: {e}")
            return None

    def _prepare_audio_file(self, audio_file):
        """Resolve local/URL audio references to an existing local playback path."""
        if not audio_file:
            print("Audio file not specified")
            return None

        if self._is_url(audio_file):
            audio_file = self._download_audio_url(audio_file)
            if not audio_file:
                return None

        # Try to use wav version if available (better compatibility)
        if audio_file.lower().endswith('.m4a'):
            wav_file = audio_file[:-4] + '.wav'
            if os.path.exists(wav_file):
                audio_file = wav_file

        if not os.path.exists(audio_file):
            print(f"Audio file not found: {audio_file}")
            return None

        return os.path.abspath(audio_file)

    def _start_word_logic(self):
        # Reset timing data for new word
        self.current_word_data = {
            'reading_start': time.time(),
            'reading_end': None,
            'writing_start': None,
            'writing_end': None,
            'video_file': None
        }
        
        word_data = self.words[self.current_cell]
        if self._is_time_mode():
            self._ensure_time_mode_page_records()
            word_record = self._current_time_mode_word_record()
            self.pen_recorder.set_audio_start_for_word(word_record)
        else:
            # Start pen data recording for this word
            self.pen_recorder.start_word({
                'word': word_data['word'],
                'cell': self._absolute_cell_index(),
                'group': word_data['group']
            })
            
            # Mark audio start time
            self.pen_recorder.set_audio_start()
        
        prepared_audio_file = word_data.get('prepared_audio_file')
        if prepared_audio_file and not os.path.exists(prepared_audio_file):
            prepared_audio_file = None

        audio_file = prepared_audio_file or self._prepare_audio_file(word_data.get('file'))
        if not audio_file:
            return
        abs_audio_file = audio_file
        
        # Try pygame mixer with wav files
        try:
            import pygame
            if not pygame.mixer.get_init():
                # Initialize with higher frequency for better quality
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            
            # --- START SEGMENT LOGIC ---
            start_ms = word_data.get('start_ms')
            end_ms = word_data.get('end_ms')
            
            if prepared_audio_file:
                pygame.mixer.music.load(prepared_audio_file)
                pygame.mixer.music.play()
                if start_ms is not None and end_ms is not None:
                    print(f"♪ Playing segment {self.current_cell + 1}: '{word_data['word']}' ({start_ms}-{end_ms}ms)")
                else:
                    print(f"♪ Playing full file {self.current_cell + 1}: '{word_data['word']}'")
            elif start_ms is not None and end_ms is not None and self.processor:
                segment_context = f"exp_playback_{self.current_cell}_{int(start_ms)}_{int(end_ms)}"
                playback_file = self.processor.get_playback_file(
                    abs_audio_file, start_ms, end_ms, context=segment_context
                )
                if playback_file:
                    pygame.mixer.music.load(playback_file)
                    pygame.mixer.music.play()
                    print(f"♪ Playing segment {self.current_cell + 1}: '{word_data['word']}' ({start_ms}-{end_ms}ms)")
                else:
                    print(f"⚠ Failed to create segment, falling back to full file: {abs_audio_file}")
                    pygame.mixer.music.load(abs_audio_file)
                    pygame.mixer.music.play()
                    print(f"♪ Playing full file {self.current_cell + 1}: '{word_data['word']}'")
            else:
                # Fallback: Play full file
                pygame.mixer.music.load(abs_audio_file)
                pygame.mixer.music.play()
                print(f"♪ Playing full file {self.current_cell + 1}: '{word_data['word']}'")
            # --- END SEGMENT LOGIC ---
            
            # Start monitoring for audio completion
            self.audio_monitor_timer.start()
            
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
        if self._is_time_mode():
            if self.current_word_data['reading_end'] is None:
                self.current_word_data['reading_end'] = time.time()
            
            self.current_cell += 1
            
            if self.current_cell >= len(self.words):
                if self.is_drawing:
                    self.pending_time_mode_finish = True
                    return
                self.wait_for_save_spacebar()
                return
            
            if self._is_page_boundary_after_index(self.current_cell):
                if self.is_drawing:
                    self.pending_time_mode_page_refresh = True
                    return
                print("⚠ Page full - pausing for refresh")
                self._finalize_time_mode_page()
                self.is_paused_for_refresh = True
                self.update()
                return
            
            self.play_current_word()
            self.update()
            return
        
        if self.current_cell >= len(self.words) - 1:
            self.wait_for_save_spacebar()
            return
        
        # End pen data recording for current word
        self.pen_recorder.end_word()
        
        # Mark reading end time (when user advances)
        if self.current_word_data['reading_end'] is None:
            self.current_word_data['reading_end'] = time.time()
        
        # Save current cell data with timing information
        if self.current_strokes or self.current_cell < len(self.words):
            cell_data = {
                'cell': self._absolute_cell_index(),
                'word': self.words[self.current_cell]['word'] if self.current_cell < len(self.words) else '',
                'strokes': self.current_strokes,
                'reading_start_time': self.current_word_data['reading_start'],
                'reading_end_time': self.current_word_data['reading_end'],
                'writing_start_time': self.current_word_data['writing_start'],
                'writing_end_time': self.current_word_data['writing_end'],
                'video_file': self.current_word_data['video_file']
            }
            # Currently unused in final export; retained while we decide whether the
            # detailed stroke/timing structure is needed by the analyzer.
            self.all_data.append(cell_data)
        
        # Move to next cell
        self.current_cell += 1
        self.current_strokes = []
        
        # Check if experiment is complete
        if self.current_cell >= len(self.words):
            self.wait_for_save_spacebar()
            return
            
        # Check for page refresh (if grid is full)
        if self._is_page_boundary_after_index(self.current_cell):
            print("⚠ Page full - pausing for refresh")
            self.is_paused_for_refresh = True
            self.update()
            return
        
        # Play next word
        self.play_current_word()
        self.update()
    
    def _result_file_stem(self, config=None, exp_name=None):
        """Return a filesystem-friendly experiment stem for result filenames."""
        config = config if config is not None else self.config
        exp_name = exp_name or self.exp_name or 'experiment'
        stem = str(exp_name)
        
        if config and '__file_path__' in config:
            try:
                config_path = Path(config['__file_path__'])
                if not stem or stem == 'experiment':
                    stem = config_path.stem
            except Exception:
                pass
        
        safe = ''.join(c if c.isalnum() or c in ('-', '_') else '_' for c in stem)
        return safe or 'experiment'
    
    def collect_experiment_data(self):
        """Finalize this block and return the JSON-ready result data."""
        if self.completed_data is not None:
            return self.completed_data
        
        if self._is_time_mode():
            self._finalize_time_mode_page()
        elif self.pen_recorder.current_word_data:
            self.pen_recorder.end_word()
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        identity = get_block_run_identity(
            self.config,
            session_index=self.session_index,
            session_total=self.session_total,
        )
        self.completed_data = {
            'schema_version': '1.3',
            'app_version': RUNNER_VERSION,
            'experiment_name': identity['experiment_name'],
            'experiment_id': identity['experiment_id'],
            'experiment_version': self.config.get('experiment_version', 1),
            'experiment_revision_id': self.config.get('experiment_revision_id'),
            'experiment_revision_number': self.config.get('experiment_revision_number'),
            'server_run_id': self.config.get('__server_run_id__'),
            'block_name': identity['block_name'],
            'block_id': identity['block_id'],
            'block_index': identity['block_index'],
            'block_count': identity['block_count'],
            'block_completed': len(self.pen_recorder.all_word_data) >= len(self.words),
            'experiment_completed': (
                len(self.pen_recorder.all_word_data) >= len(self.words)
                and identity['block_count'] == 1
            ),
            'completed_word_count': len(self.pen_recorder.all_word_data),
            'expected_word_count': len(self.words),
            'session_experiment_index': identity['block_index'],
            'session_experiment_count': identity['block_count'],
            'participant_number': self.participant_number,
            'participant_age': self.participant_age,
            'participant_gender': self.participant_gender,
            'session_id': self.config.get('__run_session_id__') or (
                f"{self.participant_number}_{timestamp}_{uuid.uuid4().hex[:6]}"
            ),
            'timestamp': timestamp,
            'calibration': self.calibration_data,
            'config': self.config,
            'words': self.pen_recorder.all_word_data
        }
        return self.completed_data
    
    def _confirm_discard(self, parent, text):
        """Ask whether unsaved data should be discarded."""
        discard_msg = QMessageBox(parent)
        discard_msg.setIcon(QMessageBox.Warning)
        discard_msg.setWindowTitle("Discard Data?")
        discard_msg.setText(text)
        discard_msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        discard_msg.setDefaultButton(QMessageBox.No)
        discard_msg.setWindowModality(Qt.ApplicationModal)
        discard_msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        return discard_msg.exec_() == QMessageBox.Yes
    
    def _cleanup_and_quit(self):
        """Release audio resources and close the app."""
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
                pygame.mixer.quit()
        except Exception:
            pass
        app = QApplication.instance()
        if app:
            app.closeAllWindows()
            app.quit()
        else:
            QApplication.quit()
    
    def _stop_runtime_activity(self):
        """Stop experiment timers and any currently playing prompt."""
        self.audio_monitor_timer.stop()
        self.auto_proceed_timer.stop()
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:
            pass

    def _upload_saved_results(self, saved_results, transition="finalize"):
        """Durably queue cloud-run results, then make a best-effort upload."""
        if self.test_mode:
            return 0, [], len(saved_results)

        cloud_results = []
        skipped = 0
        for data_file, result in saved_results:
            experiment_id = result.get('experiment_id')
            try:
                uuid.UUID(str(experiment_id))
            except (ValueError, TypeError, AttributeError):
                skipped += 1
                continue
            cloud_results.append((data_file, result, str(experiment_id)))

        if not cloud_results:
            return 0, [], skipped

        from result_upload_queue import drain_upload_queue, enqueue_result, enqueue_transition
        run_ids = set()
        for data_file, result, experiment_id in cloud_results:
            run_id = result.get('server_run_id') or result.get('config', {}).get('__server_run_id__')
            enqueue_result(data_file, experiment_id, run_id=run_id)
            if run_id:
                run_ids.add(str(run_id))
        if transition:
            for run_id in run_ids:
                enqueue_transition(run_id, transition)
        from autoscript_api import AutoScriptAPI
        try:
            api = AutoScriptAPI()
        except Exception as exc:
            return 0, [f"Cloud connection: {exc}"], skipped
        uploaded, queue_errors = drain_upload_queue(api)
        return uploaded, list(queue_errors), skipped

    def _upload_results_before_export(self, results):
        """Persist cloud results independently of the optional user export dialog."""
        cached = getattr(self, '_cloud_upload_outcome', None)
        if cached is not None:
            return cached

        from app_paths import ensure_dir, user_data_dir

        staging = ensure_dir(user_data_dir() / 'results' / 'cloud_staging')
        staged_results = []
        try:
            for result in results:
                target = staging / f"{uuid.uuid4().hex}.json"
                temporary = target.with_suffix('.tmp')
                temporary.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding='utf-8',
                )
                temporary.replace(target)
                staged_results.append((target, result))
            outcome = self._upload_saved_results(staged_results, transition=None)
            self._cloud_upload_outcome = outcome
            return outcome
        finally:
            for staged_path, _result in staged_results:
                staged_path.unlink(missing_ok=True)

    def _transition_cloud_results(self, results, transition):
        """Queue a terminal Run transition after its result payloads."""
        if self.test_mode:
            return 0, []

        from autoscript_api import AutoScriptAPI
        from result_upload_queue import drain_upload_queue, enqueue_transition

        run_ids = {
            str(
                result.get('server_run_id')
                or result.get('config', {}).get('__server_run_id__')
            )
            for result in results
            if (
                result.get('server_run_id')
                or result.get('config', {}).get('__server_run_id__')
            )
        }
        if not run_ids:
            return 0, []
        for run_id in run_ids:
            enqueue_transition(run_id, transition)
        try:
            return drain_upload_queue(AutoScriptAPI())
        except Exception as exc:
            return 0, [f"Cloud connection: {exc}"]

    @staticmethod
    def _cloud_save_status(uploaded, errors, skipped):
        if errors:
            detail = "\n".join(errors[:3])
            if len(errors) > 3:
                detail += f"\n...and {len(errors) - 3} more"
            return (
                f"\n\nCloud: uploaded {uploaded}; {len(errors)} failed. "
                f"The local files are safe.\n{detail}"
            )
        if uploaded:
            return f"\n\nCloud: uploaded {uploaded} result file(s)."
        if skipped:
            return "\n\nLocal/legacy run: results were kept locally."
        return ""
    
    def _save_single_result(self, combined_data, dialog_parent):
        """Save one experiment result using the existing file-save flow."""
        from pathlib import Path
        from PyQt5.QtWidgets import QFileDialog
        from app_paths import ensure_dir, user_data_dir
        
        results_dir = ensure_dir(user_data_dir() / 'results')
        default_filename = f"{self._result_file_stem()}_p{self.participant_number}_{combined_data['timestamp']}.json"
        default_path = results_dir / default_filename
        combined_data['experiment_completed'] = bool(
            combined_data.get('block_completed', False)
        )
        uploaded, errors, skipped = self._upload_results_before_export([combined_data])
        
        save_dialog = QFileDialog(
            dialog_parent,
            "Save Experiment Data",
            str(default_path),
            "JSON Files (*.json);;All Files (*.*)"
        )
        save_dialog.setAcceptMode(QFileDialog.AcceptSave)
        save_dialog.setDefaultSuffix("json")
        save_dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        save_dialog.setWindowModality(Qt.ApplicationModal)
        save_dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        save_path = save_dialog.selectedFiles()[0] if save_dialog.exec_() == QFileDialog.Accepted else ""
        
        if not save_path:
            if not self._confirm_discard(dialog_parent, "Are you sure you want to exit without saving the experiment data?"):
                self.finish_experiment()
            else:
                self._transition_cloud_results([combined_data], 'cancel')
                print("⚠ Experiment data discarded by user")
                self._cleanup_and_quit()
            return
        
        try:
            data_file = Path(save_path)
            with open(str(data_file), 'w', encoding='utf-8') as f:
                json.dump(combined_data, f, ensure_ascii=False, indent=2)
            transition_uploaded, transition_errors = self._transition_cloud_results(
                [combined_data], 'finalize'
            )
            uploaded += transition_uploaded
            if transition_errors:
                errors.extend(transition_errors)
            elif transition_uploaded:
                errors = [error for error in errors if "Quarantined" in error]
            
            msg = QMessageBox(dialog_parent)
            msg.setIcon(QMessageBox.Information)
            msg.setWindowTitle("Experiment Complete")
            msg.setText(
                f"Experiment finished!\n\nData saved to:\n{str(data_file)}"
                + self._cloud_save_status(uploaded, errors, skipped)
            )
            msg.setWindowModality(Qt.ApplicationModal)
            msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            msg.exec_()
        except Exception as e:
            error_msg = QMessageBox(dialog_parent)
            error_msg.setIcon(QMessageBox.Critical)
            error_msg.setWindowTitle("Error")
            error_msg.setText(f"Failed to save data:\n{str(e)}")
            error_msg.setWindowModality(Qt.ApplicationModal)
            error_msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            error_msg.exec_()
            return
        
        self._cleanup_and_quit()
    
    def _save_session_results(self, session_results, dialog_parent):
        """Save every block result in a multi-block experiment separately."""
        from pathlib import Path
        from PyQt5.QtWidgets import QFileDialog
        from app_paths import ensure_dir, user_data_dir
        
        results_dir = ensure_dir(user_data_dir() / 'results')
        experiment_completed = (
            len(session_results) == self.session_total
            and all(result.get('block_completed', False) for result in session_results)
        )
        for result in session_results:
            result['experiment_completed'] = experiment_completed
        uploaded, errors, skipped = self._upload_results_before_export(session_results)
        parent_dir = QFileDialog.getExistingDirectory(
            dialog_parent,
            f"Select Parent Folder for Participant {self.participant_number}",
            str(results_dir)
        )
        
        if not parent_dir:
            if not self._confirm_discard(dialog_parent, "Are you sure you want to exit without saving this experiment run?"):
                self.finish_experiment()
            else:
                self._transition_cloud_results(session_results, 'cancel')
                print("⚠ Experiment session data discarded by user")
                self._cleanup_and_quit()
            return
        
        session_dir = Path(parent_dir) / str(self.participant_number)
        session_dir.mkdir(parents=True, exist_ok=True)
        
        saved_files = []
        try:
            for index, result in enumerate(session_results, start=1):
                stem = self._result_file_stem(result.get('config'), result.get('block_name'))
                filename = f"{stem}_p{self.participant_number}_{result.get('timestamp')}.json"
                data_file = session_dir / filename
                if data_file.exists():
                    data_file = session_dir / f"{stem}_p{self.participant_number}_{result.get('timestamp')}_{index}.json"
                
                with open(str(data_file), 'w', encoding='utf-8') as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                saved_files.append(data_file)
            transition_uploaded, transition_errors = self._transition_cloud_results(
                session_results, 'finalize'
            )
            uploaded += transition_uploaded
            if transition_errors:
                errors.extend(transition_errors)
            elif transition_uploaded:
                errors = [error for error in errors if "Quarantined" in error]
        except Exception as e:
            error_msg = QMessageBox(dialog_parent)
            error_msg.setIcon(QMessageBox.Critical)
            error_msg.setWindowTitle("Error")
            error_msg.setText(f"Failed to save session data:\n{str(e)}")
            error_msg.setWindowModality(Qt.ApplicationModal)
            error_msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            error_msg.exec_()
            return
        
        msg = QMessageBox(dialog_parent)
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("Session Complete")
        msg.setText(
            f"Saved {len(saved_files)} block result files to:\n{str(session_dir)}"
            + self._cloud_save_status(uploaded, errors, skipped)
        )
        msg.setWindowModality(Qt.ApplicationModal)
        msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        msg.exec_()
        self._cleanup_and_quit()
    
    def finish_experiment(self):
        """Finalize and save this block or the full multi-block experiment."""
        dialog_parent = self.window()
        current_data = self.collect_experiment_data()
        self._stop_runtime_activity()
        
        main_window = self.window()
        prior_results = list(getattr(main_window, 'session_results', []))
        if self.session_total > 1:
            self._save_session_results(prior_results + [current_data], dialog_parent)
        else:
            self._save_single_result(current_data, dialog_parent)
    
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
    
    def _handle_time_mode_pointer_event(self, pointer_event, virtual_x, virtual_y, pressure, timestamp, cell):
        """Record strokes by the cell they belong to, independent of the active prompt."""
        if pointer_event == 'press':
            if cell == -1:
                return
            
            word_record = self._time_mode_record_for_cell(cell)
            if not word_record:
                return
            
            self.pen_recorder.record_event_for_word(word_record, 'press', virtual_x, virtual_y, pressure, timestamp)
            self.is_drawing = True
            self.current_stroke_cell = cell
            self.current_stroke = [{
                'x': virtual_x,
                'y': virtual_y,
                'pressure': pressure,
                'time': timestamp
            }]
            
        elif pointer_event == 'move' and self.is_drawing and self.current_stroke_cell is not None:
            word_record = self._time_mode_record_for_cell(self.current_stroke_cell)
            if not word_record:
                return
            
            self.pen_recorder.record_event_for_word(word_record, 'move', virtual_x, virtual_y, pressure, timestamp)
            self.current_stroke.append({
                'x': virtual_x,
                'y': virtual_y,
                'pressure': pressure,
                'time': timestamp
            })
            self.update()
            
        elif pointer_event == 'release':
            if self.is_drawing and self.current_stroke and self.current_stroke_cell is not None:
                word_record = self._time_mode_record_for_cell(self.current_stroke_cell)
                if word_record:
                    self.pen_recorder.record_event_for_word(word_record, 'release', virtual_x, virtual_y, pressure, timestamp)
                
                self.current_stroke.append({
                    'x': virtual_x,
                    'y': virtual_y,
                    'pressure': pressure,
                    'time': timestamp
                })
                self.time_mode_strokes_by_cell.setdefault(self.current_stroke_cell, []).append(self.current_stroke)
            
            self.current_stroke = []
            self.current_stroke_cell = None
            self.is_drawing = False
            self.update()
            self._complete_pending_time_mode_transition()

    def _handle_pointer_event(self, pointer_event, virtual_x, virtual_y, pressure, timestamp, cell):
        if (
            self.waiting_for_experiment_start or
            self.starting_experiment_after_space
        ):
            return

        if getattr(self, 'is_paused_for_refresh', False):
            return

        if self._is_time_mode():
            self._handle_time_mode_pointer_event(pointer_event, virtual_x, virtual_y, pressure, timestamp, cell)
            return

        if cell == -1 or cell != self._current_page_cell_index():
            return

        if pointer_event == 'press':
            if self.current_word_data['writing_start'] is None:
                self.current_word_data['writing_start'] = time.time()
                if self.current_word_data['reading_end'] is None:
                    self.current_word_data['reading_end'] = time.time()
                if self.pen_recorder.current_word_data and self.pen_recorder.current_word_data['audio_end_time'] is None:
                    self.pen_recorder.set_audio_end()

            self.pen_recorder.record_event('press', virtual_x, virtual_y, pressure, timestamp)

            self.is_drawing = True
            self.current_stroke = [{
                'x': virtual_x,
                'y': virtual_y,
                'pressure': pressure,
                'time': timestamp
            }]

        elif pointer_event == 'move' and self.is_drawing:
            self.pen_recorder.record_event('move', virtual_x, virtual_y, pressure, timestamp)

            self.current_stroke.append({
                'x': virtual_x,
                'y': virtual_y,
                'pressure': pressure,
                'time': timestamp
            })
            self.update()

        elif pointer_event == 'release':
            if self.is_drawing and self.current_stroke:
                self.pen_recorder.record_event('release', virtual_x, virtual_y, pressure, timestamp)

                self.current_stroke.append({
                    'x': virtual_x,
                    'y': virtual_y,
                    'pressure': pressure,
                    'time': timestamp
                })
                self.current_strokes.append(self.current_stroke)
                self.current_stroke = []
                self.current_word_data['writing_end'] = time.time()

            self.is_drawing = False
            self.update()
    
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
        pointer_map = {
            QEvent.TabletPress: 'press',
            QEvent.TabletMove: 'move',
            QEvent.TabletRelease: 'release',
        }
        pointer_event = pointer_map.get(event.type())
        if pointer_event:
            self._handle_pointer_event(pointer_event, virtual_x, virtual_y, pressure, timestamp, cell)
        
        event.accept()
    
    def mousePressEvent(self, event):
        if not self.test_mode or event.button() != Qt.LeftButton:
            return

        global_pos = event.globalPos()
        timestamp = int(time.time() * 1000)
        virtual_x, virtual_y, x_ratio, y_ratio = self.transform_point(global_pos.x(), global_pos.y())
        cell = self.get_cell_from_position(x_ratio, y_ratio)
        self._handle_pointer_event('press', virtual_x, virtual_y, 1.0, timestamp, cell)
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.test_mode or not (event.buttons() & Qt.LeftButton):
            return

        global_pos = event.globalPos()
        timestamp = int(time.time() * 1000)
        virtual_x, virtual_y, x_ratio, y_ratio = self.transform_point(global_pos.x(), global_pos.y())
        cell = self.get_cell_from_position(x_ratio, y_ratio)
        self._handle_pointer_event('move', virtual_x, virtual_y, 1.0, timestamp, cell)
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self.test_mode or event.button() != Qt.LeftButton:
            return

        global_pos = event.globalPos()
        timestamp = int(time.time() * 1000)
        virtual_x, virtual_y, x_ratio, y_ratio = self.transform_point(global_pos.x(), global_pos.y())
        cell = self.get_cell_from_position(x_ratio, y_ratio)
        self._handle_pointer_event('release', virtual_x, virtual_y, 1.0, timestamp, cell)
        event.accept()
            
    def keyPressEvent(self, event):
        """Handle keyboard events"""
        if self.waiting_for_experiment_start or self.starting_experiment_after_space:
            if event.key() == Qt.Key_Space and not self.starting_experiment_after_space:
                self.waiting_for_experiment_start = False
                self.starting_experiment_after_space = True
                self.update()
                QTimer.singleShot(0, self.begin_after_start_screen)
            elif event.key() == Qt.Key_Escape:
                self.finish_experiment()
            event.accept()
            return
        
        if self.waiting_for_next_experiment_spacebar:
            if event.key() == Qt.Key_Space:
                if self._audio_is_playing():
                    event.accept()
                    return
                self.waiting_for_next_experiment_spacebar = False
                main_window = self.window()
                if hasattr(main_window, 'finish_current_and_start_next'):
                    main_window.finish_current_and_start_next()
            elif event.key() == Qt.Key_Escape:
                self.finish_experiment()
            event.accept()
            return
        
        if self.waiting_for_save_spacebar:
            if event.key() == Qt.Key_Space:
                if self._audio_is_playing():
                    event.accept()
                    return
                self.waiting_for_save_spacebar = False
                self.finish_experiment()
            elif event.key() == Qt.Key_Escape:
                self.finish_experiment()
            event.accept()
            return
        
        # --- NEW: Handle Proceed Key Definition ---
        if self.waiting_for_proceed_key:
            key_val = event.key()
            # Ignore modifiers alone
            if key_val in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
                return
            
            # Store the key
            self.proceed_key = key_val
            key_text = QKeySequence(key_val).toString()
            print(f"✓ Proceed key defined as: {key_text} (ID: {key_val})")
            
            # Clear state and start
            self.waiting_for_proceed_key = False
            main_window = self.window()
            if isinstance(main_window, QMainWindow):
                main_window.statusBar().showMessage(f"Proceed Key: {key_text}")
            if self.start_gate_after_key_definition:
                self.start_gate_after_key_definition = False
                self.show_experiment_start_screen()
            else:
                self.play_current_word()
            self.update()
            return

        # Handle Ctrl+R for recalibration
        if event.key() == Qt.Key_R and event.modifiers() == Qt.ControlModifier:
            print("\n⚠ Ctrl+R pressed - initiating recalibration...")
            self.request_recalibration()
            return
        
        # Handle resume from pause
        if getattr(self, 'is_paused_for_refresh', False):
            if event.key() == Qt.Key_Space:
                if self._should_recalibrate_for_page_refresh():
                    print("✓ Re-calibrating before the next page")
                    self.request_page_refresh_recalibration()
                else:
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
            msg = QMessageBox(self.window())
            msg.setIcon(QMessageBox.Question)
            msg.setWindowTitle('Exit Experiment')
            msg.setText('Are you sure you want to exit?\nYou can save or discard data next.')
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)
            msg.setWindowModality(Qt.ApplicationModal)
            msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            reply = msg.exec_()
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

    def request_page_refresh_recalibration(self):
        """Request a recalibration that advances to a fresh page before resuming."""
        self.is_paused_for_refresh = False
        parent = self.parent()
        if parent and hasattr(parent, 'start_recalibration'):
            parent.start_recalibration(page_advance=True)
    
    def paintEvent(self, event):
        """Draw the quadrilateral and current strokes"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Fill background
        painter.fillRect(self.rect(), Qt.white)
        
        if self.waiting_for_experiment_start or self.starting_experiment_after_space:
            painter.setPen(QPen(Qt.black))
            painter.setFont(QFont("Arial", 28, QFont.Bold))
            text = "Starting experiment..." if self.starting_experiment_after_space else "Press SPACE to start experiment"
            painter.drawText(self.rect(), Qt.AlignCenter, text)
            return
        
        # Draw "Waiting for Key" Overlay
        if self.waiting_for_proceed_key:
            painter.setPen(QPen(Qt.black))
            painter.setFont(QFont("Arial", 24, QFont.Bold))
            text = "Press ANY KEY to define it separately\nas the 'Next Word' trigger."
            painter.drawText(self.rect(), Qt.AlignCenter, text)
            return

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
        
        strokes_to_draw = self.current_strokes
        if self._is_time_mode():
            strokes_to_draw = [
                stroke
                for cell_strokes in self.time_mode_strokes_by_cell.values()
                for stroke in cell_strokes
            ]
        
        for stroke in strokes_to_draw:
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
            pause_text = "Please refresh paper\nand press SPACE to continue"
            if self._should_recalibrate_for_page_refresh():
                pause_text = "Please refresh paper\nand press SPACE to recalibrate"
            painter.drawText(self.rect(), Qt.AlignCenter, pause_text)
            return
        
        # Draw instructions at top
        painter.setPen(QPen(Qt.black, 1))
        painter.setFont(QFont('Arial', 14, QFont.Bold))
        
        if self.waiting_for_next_experiment_spacebar or self.waiting_for_save_spacebar:
            heading_prefix = self._current_heading_prefix()
            if self._audio_is_playing():
                text = f"{heading_prefix} - Finish writing - waiting for audio to finish"
            elif self.waiting_for_next_experiment_spacebar:
                text = f"{heading_prefix} - Finish writing - press SPACE for next block"
            else:
                text = f"{heading_prefix} - Finish writing - press SPACE to save data"
        elif self.current_cell < len(self.words):
            word = self.words[self.current_cell]['word']
            cell_number = self._current_page_cell_index() + 1
            heading_prefix = self._current_heading_prefix()
            key_name = "SPACE"
            if self.proceed_mode == 'key':
                if self.proceed_key == Qt.Key_Space: key_name = "SPACE"
                elif self.proceed_key == Qt.Key_Return: key_name = "ENTER"
                elif self.proceed_key == Qt.Key_Right: key_name = "RIGHT ARROW"
                elif self.proceed_key == Qt.Key_Down: key_name = "DOWN ARROW"
                text = (
                    f"{heading_prefix} - Cell {cell_number}/{self.total_cells} - "
                    f"Write: '{word}' - Press {key_name} for next"
                )
            else:
                text = (
                    f"{heading_prefix} - Cell {cell_number}/{self.total_cells} - "
                    f"Write: '{word}' - Auto-advance enabled"
                )
        else:
            text = f"{self._current_heading_prefix()} - Block Complete!"
        
        painter.drawText(self.rect(), Qt.AlignTop | Qt.AlignHCenter, text)


class ExperimentWindow(QMainWindow):
    """Main window for the experiment stage"""
    
    def __init__(
        self,
        calibration_data,
        participant_number,
        config=None,
        age=None,
        gender=None,
        auto_start=True,
        show_start_screen=True,
        session_configs=None,
        session_config_index=0,
        session_results=None,
        test_mode=False,
        start_cell_offset=0,
        initial_page_number=1
    ):
        super().__init__()
        self.calibration_data = calibration_data
        self.participant_number = participant_number
        self.participant_age = age
        self.participant_gender = gender
        self.config = config
        self.session_configs = list(session_configs) if session_configs else ([config] if config else [])
        self.session_config_index = session_config_index
        self.session_results = list(session_results) if session_results else []
        self.test_mode = test_mode
        self.next_window = None
        ensure_shared_run_session_id(self.session_configs, self.participant_number)
        if self.session_config_index == 0:
            initialize_cloud_run(
                self.session_configs,
                self.participant_number,
                self.participant_age,
                self.participant_gender,
                test_mode=self.test_mode,
            )
        layout = (self.config or {}).get('__session_layout__', {}) if self.config else {}
        self.start_cell_offset = int(layout.get('start_cell_offset', start_cell_offset) or 0)
        self.initial_page_number = max(1, int(initial_page_number or 1))
        
        title = "Tablet Experiment - Experiment Stage (Test Mode)" if self.test_mode else "Tablet Experiment - Experiment Stage"
        self.setWindowTitle(title)
        
        # Keep window on top of all other windows (including media player)
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        
        self.showFullScreen()
        
        # Create canvas
        self.canvas = ExperimentCanvas(
            calibration_data,
            participant_number,
            config,
            age,
            gender,
            parent=self,
            session_index=session_config_index,
            session_total=max(1, len(self.session_configs)),
            test_mode=self.test_mode,
            start_cell_offset=self.start_cell_offset,
            initial_page_number=self.initial_page_number
        )
        self.setCentralWidget(self.canvas)
        
        print("✓ Experiment stage started")
        print("  Press ESC to exit, Ctrl+R to recalibrate")
        
        if auto_start:
            if show_start_screen:
                self.canvas.request_initial_start_screen()
            elif not self.canvas.waiting_for_proceed_key:
                self.canvas.play_current_word()

    def _session_recalibration_enabled(self):
        """Return True when the active session requests recalibration on every new page."""
        return bool((self.config or {}).get('__session_recalibrate_between_pages__', False))

    def start_recalibration(self, page_advance=False):
        """Start recalibration process"""
        print("⟲ Starting recalibration...")
        self.hide()
        
        # Create new calibration window
        self.recalib_window = CalibrationWindow(
            self.config,
            session_configs=self.session_configs,
            session_config_index=self.session_config_index,
            session_results=self.session_results,
            test_mode=self.test_mode
        )
        # Store current experiment state to resume after recalibration
        self.recalib_window.resume_experiment_data = {
            'participant_number': self.participant_number,
            'age': self.participant_age,
            'gender': self.participant_gender,
            'current_cell': self.canvas.current_cell,
            'start_cell_offset': self.canvas.start_cell_offset,
            'pen_recorder': self.canvas.pen_recorder,
            'all_data': self.canvas.all_data,
            'page_number': self.canvas.page_number + (1 if page_advance else 0),
            'time_mode_page_start': self.canvas.time_mode_page_start,
            'time_mode_word_records': self.canvas.time_mode_word_records,
            'time_mode_strokes_by_cell': self.canvas.time_mode_strokes_by_cell,
            'current_stroke_cell': self.canvas.current_stroke_cell,
            'current_stroke': self.canvas.current_stroke,
            'is_drawing': self.canvas.is_drawing,
            'pending_time_mode_page_refresh': self.canvas.pending_time_mode_page_refresh,
            'pending_time_mode_finish': self.canvas.pending_time_mode_finish
        }
        self.recalib_window.show()
    
    def finish_current_and_start_next(self):
        """Finalize the current block and continue to the next block config."""
        next_index = self.session_config_index + 1
        if next_index >= len(self.session_configs):
            self.canvas.finish_experiment()
            return
        
        current_data = self.canvas.collect_experiment_data()
        next_results = list(self.session_results)
        next_results.append(current_data)
        
        next_config = self.session_configs[next_index]
        next_layout = next_config.get('__session_layout__', {}) if next_config else {}
        same_page_as_previous = bool(next_layout.get('same_page_as_previous', False))
        recalibrate_before_start = bool(next_layout.get('recalibrate_before_start', False))
        if not recalibrate_before_start and not same_page_as_previous:
            recalibrate_before_start = bool(next_config.get('__session_recalibrate_between_pages__', False))
        next_page_number = self.canvas.page_number if same_page_as_previous else self.canvas.page_number + 1

        if recalibrate_before_start:
            self.next_window = CalibrationWindow(
                next_config,
                session_configs=self.session_configs,
                session_config_index=next_index,
                session_results=next_results,
                test_mode=self.test_mode,
                show_start_screen_after_calibration=False
            )
            self.next_window.pending_participant_number = self.participant_number
            self.next_window.pending_participant_age = self.participant_age
            self.next_window.pending_participant_gender = self.participant_gender
            self.next_window.show()
            self.hide()
            return

        self.next_window = ExperimentWindow(
            self.calibration_data,
            self.participant_number,
            next_config,
            self.participant_age,
            self.participant_gender,
            show_start_screen=False,
            session_configs=self.session_configs,
            session_config_index=next_index,
            session_results=next_results,
            test_mode=self.test_mode,
            start_cell_offset=next_layout.get('start_cell_offset', 0),
            initial_page_number=next_page_number
        )
        self.next_window.show()
        self.hide()
    
    def keyPressEvent(self, event):
        """Handle keyboard events at window level"""
        if event.key() == Qt.Key_Escape:
            # Confirm exit
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Question)
            msg.setWindowTitle('Exit Experiment')
            msg.setText('Are you sure you want to exit?\nYou can save or discard data next.')
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)
            msg.setWindowModality(Qt.ApplicationModal)
            msg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            reply = msg.exec_()
            if reply == QMessageBox.Yes:
                self.canvas.finish_experiment()
        else:
            # Pass other keys to canvas
            self.canvas.keyPressEvent(event)
        event.accept()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help="Use mouse input instead of pen/tablet input for calibration and drawing."
    )
    parser.add_argument(
        "--session-plan",
        help="Path to the launcher-generated session page layout plan."
    )
    parser.add_argument(
        "config_paths",
        nargs="+",
        help="Path(s) to experiment configuration JSON files from exported experiment ZIPs"
    )
    args = parser.parse_args()
    
    try:
        configs = [load_experiment_config(path) for path in args.config_paths]
    except Exception as e:
        print(f"✗ Failed to load config: {e}")
        sys.exit(2)

    try:
        session_plan = load_session_plan(args.session_plan) if args.session_plan else None
        apply_session_plan(configs, session_plan)
    except Exception as e:
        print(f"⚠ Failed to load session plan: {e}")
        apply_session_plan(configs, None)

    original_excepthook = sys.excepthook

    def report_runner_failure(exc_type, exc_value, traceback):
        queue_cloud_run_failure(configs)
        original_excepthook(exc_type, exc_value, traceback)

    sys.excepthook = report_runner_failure
    
    from qt_bootstrap import ensure_qt_platform_plugin_path
    ensure_qt_platform_plugin_path()
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    window = CalibrationWindow(
        configs[0],
        session_configs=configs,
        session_config_index=0,
        session_results=[],
        test_mode=args.test_mode
    )
    window.show()
    
    print("Tablet Experiment - Calibration Stage")
    if args.test_mode:
        print("Mouse test mode enabled")
    print("Touch and hold (0.5s) any 4 corners of your paper")
    print("Corners will be automatically identified by position")
    print("Tolerance: 15mm deviation allowed")
    
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
