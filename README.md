# Touchpad Writing Experiment - Complete Guide

## 📋 Overview

This is a complete toolkit for conducting tablet/touchpad writing experiments with Hebrew words. The system records pen movements, pressure, and timing data while participants write words they hear through audio playback.

## 🚀 Quick Installation

1. **Double-click `INSTALL.bat`** to set up everything automatically
2. The installer will:
   - Create a virtual environment
   - Install all required packages (PyQt5, numpy, scipy, Pillow)
   - Create launcher scripts
   - Verify the installation

## 🗂️ Project Structure

```
touchpad exp/
├── src/
│   ├── recordings/          # Place your raw audio files here (m4a, mp3, or wav)
│   ├── sliced_words/        # Auto-generated word segments (wav format)
│   └── word_labels.json     # Auto-generated word database
├── results/
│   └── participant_N/       # Auto-created for each participant
│       └── participant_N_data.json
├── install.py               # 🔧 Project installer
├── audio_processor.py       # 🎵 Unified audio tool (slicer + matcher + converter)
├── tablet_experiment.py     # 📝 Main experiment interface
├── analyzer_refactored.py   # 📊 Data analysis and visualization tool
├── main_interface.py        # 🖥️ GUI experiment manager
└── launcher.py              # 📋 Console menu launcher
```

## 🚀 Complete Workflow

### Option A: GUI Experiment Manager (Recommended)

Use the graphical interface for full experiment configuration:

```powershell
python main_interface.py
# Or double-click: START_GUI.bat
```

**Features:**
1. **New Experiment** - Create and configure experiments:
   - Upload audio files (mp3, wav, m4a) - auto-sliced into words
   - Enter Hebrew word labels for each audio slice
   - Configure experiment properties:
     - Experiment name
     - Grid size (rows × columns)
     - Word presentation order (random/sequential)
     - Proceed condition (keypress/timer)
     - Audio beeps before/after words
   - **Export as ZIP package** for later use or sharing

2. **Load Experiment** - Load a previously saved experiment ZIP:
   - Imports all audio files and configuration
   - Ready to run immediately
   - Perfect for multi-site studies

3. **Analyze Results** - Opens the stroke analyzer

### Option B: Command Line Workflow

### Step 1: Prepare Audio Files

1. **Place your audio files** in `src/recordings/`:
   - Supported formats: **m4a, mp3, wav**
   - Each file contains multiple words separated by silence
   - Example filenames:
     - `true_yod_f.m4a`
     - `false_yod_gramm_f.mp3`
     - `recording_group_1.wav`

### Step 2: Process Audio (Slice + Match + Convert)

Run the unified audio processor:

```powershell
python audio_processor.py
```

**What it does:**
1. ✂️ **Slices** each recording into individual word files
   - Automatically detects words by finding silence gaps
   - Converts everything to WAV format (required for experiment)
2. 🏷️ **Matches** each word file with Hebrew text
   - Interactive prompt for each word
   - Plays audio so you can hear the word
   - Enter Hebrew text for each word
3. 💾 **Saves** everything to `word_labels.json`

**Output:**
- `src/sliced_words/` → `recording_name_word_001.wav`, `recording_name_word_002.wav`, ...
- `src/word_labels.json` → Database mapping files to Hebrew words

### Step 3: Run Experiment

```powershell
python tablet_experiment.py
```

**Experiment Flow:**

1. **Calibration Stage:**
   - Touch and hold (0.5s) each of the 4 paper corners
   - Corners auto-identified by position
   - Validate or reset if needed
   - Press **SPACE** to continue after successful calibration

2. **Participant Setup:**
   - Enter participant number (1-9999)
   - Enter participant age
   - Select participant gender (Male/Female/Other)

3. **Writing Stage:**
   - 25 words (randomized order)
   - For each word:
     - ▶️ Audio plays automatically
     - ✍️ Write the word in the assigned grid cell
     - ⏵ Press SPACE to advance to next word
   - **Press Ctrl+R at any time to recalibrate** (preserves experiment progress)
   - Data recorded:
     - Pen position, pressure, speed
     - Audio timing (start/end)
     - Stroke timing
     - Group/category information
     - Participant demographics (age, gender)

4. **Results:**
   - Saved to: `results/participant_N/participant_N_data.json`

### Step 4: Analyze Data

```powershell
python analyzer_refactored.py
```

**Analyzer Features:**

- 📁 **Open participant data** (`participant_N_data.json`)
- 🗂️ **Grouped word tree** - words organized by category
- 🎬 **Animation playback** - watch writing in real-time (RTL slider for Hebrew)
- ⏯️ **Playback controls (RTL optimized):**
  - Play/Pause (SPACE)
  - Previous Event (RIGHT arrow) / Next Event (LEFT arrow) - RTL navigation
  - Previous/Next Stroke (Ctrl+RIGHT/LEFT)
  - Previous/Next Word (UP/DOWN arrows)
  - **Enter** - Assign letter when on first event of a stroke
- ✂️ **Stroke slicing:**
  - Click "✂ Slice Stroke Here" to split a stroke at the current event
  - Useful for correcting stroke boundaries
- 🚧 **Blocker support:**
  - Leave letter field empty to insert a blocker
  - Blockers display as '|' in the Letters column
  - Automatically marks word as "Low-Quality Trainable"
- 🎯 **Letter assignment:**
  - Assign letters to stroke segments
  - Auto-detects if word should be low-quality (blockers or stroke count mismatch)
  - Up to 3 target markers per word
- 📊 **Timing display:**
  - All times relative to Reading Start (audio_start)
  - Audio listening duration
  - Event timestamps
  - Pen pressure and speed
- 📤 **CSV/JSON Export:**
  - **Single file mode:** Click "📊 Output Data (CSV)" when done analyzing
  - **Multiple files mode:** Click "📁 Load Multiple Files" to batch process
  - Exports include: age, gender, word timings, letter segments, train mode
  - **Single file:** Saves to `participant_N_analysis.csv`
  - **Multiple files:** Saves all participants to `combined_analysis.csv`

### Batch Processing Multiple Participants

**Method 1: Load Multiple Files at Once**
1. **Click "📁 Load Multiple Files"** in analyzer
2. **Select multiple participant JSON files** (Ctrl+Click or Shift+Click)
3. **All participants displayed in tree** - organized by participant number
4. **Navigate through all words** - tree shows: Participant → Group → Words

**Method 2: Load Files One by One**
1. **Click "Open Participant Data"** to load first file
2. **Click "Open Participant Data"** again to load another file
3. **Choose "YES"** to add to collection (or "NO" to replace)
4. **Repeat** to add more files
5. **All participants displayed together** in the tree

**Both methods result in:**
- All participants displayed in one tree view
- Can view animations from any participant
- Can navigate between all words seamlessly
- **Click "📊 Output Data (CSV)"** to export all data to one file
- Single CSV with all participants' data combined

**Tree Structure in Multi-File Mode:**
```
└─ Participant 5 (10 words)
   ├─ false_yod (5 words)
   │  ├─ Cell 0: 'מזרקה' (234 events)
   │  └─ Cell 1: 'מכלאה' (189 events)
   └─ true_yod (5 words)
      ├─ Cell 2: 'איפוס' (156 events)
      └─ Cell 3: 'כישוף' (201 events)
└─ Participant 6 (10 words)
   └─ ...
```

**Benefits:**
- View and compare animations across multiple participants
- Process entire experiment dataset at once
- Consistent formatting across all participants
- Ready for statistical analysis (SPSS, R, Python)

**Note:** Target letter marking is available in both single and multi-file modes, but marks are session-specific and not saved to CSV in multi-file batch exports.

## 📦 Required Dependencies

**Automatic installation (recommended):**
```powershell
# Double-click INSTALL.bat or run:
python install.py
```

**Manual installation:**
```powershell
pip install PyQt5 numpy scipy Pillow pyautogui
```

**External dependency:**
- **ffmpeg** (for audio processing)
  - Included with PsychoPy: `C:\Program Files\PsychoPy\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe`
  - Or install: `winget install ffmpeg`

## 🎨 Customization

### Audio Processing Parameters

Edit `audio_processor.py`:

```python
segments = self.detect_nonsilent(
    audio_array, 
    sample_rate,
    silence_thresh=0.01,    # Lower = more sensitive (0-1)
    min_silence_len=300,    # Minimum silence between words (ms)
    min_word_len=100        # Minimum word duration (ms)
)
```

### Experiment Grid Size

Edit `tablet_experiment.py`:

```python
self.grid_size = 5  # 5x5 = 25 cells
```

### Number of Words

In `tablet_experiment.py`, words are automatically limited to grid size:

```python
self.words = self.words[:25]  # One word per cell
```

## 📊 Data Format

### Word Labels (`word_labels.json`)

```json
{
  "true_yod_f": [
    {"file": "true_yod_f_word_001.wav", "word": "איפוס"},
    {"file": "true_yod_f_word_002.wav", "word": "אילוץ"}
  ],
  "false_yod_gramm_f": [
    {"file": "false_yod_gramm_f_word_001.wav", "word": "משכל"},
    {"file": "false_yod_gramm_f_word_002.wav", "word": "מטבל"}
  ]
}
```

### Participant Data (`participant_N_data.json`)

```json
{
  "participant_number": 5,
  "participant_age": 25,
  "participant_gender": "Female",
  "timestamp": "20251126_141849",
  "calibration": {
    "corners": [[x1, y1], [x2, y2], [x3, y3], [x4, y4]]
  },
  "words": [
    {
      "word": "hebrew_word",
      "cell": 0,
      "group": "true_yod_f",
      "audio_start_time": 1764156605.871,
      "audio_end_time": 1764156608.234,
      "pen_events": [
        {
          "type": "press",
          "x": 1234.5,
          "y": 678.9,
          "pressure": 0.75,
          "timestamp": 1234567890,
          "absolute_time": 1764156610.123,
          "speed": 0.0
        },
        ...
      ]
    },
    ...
  ]
}
```

### Analysis Output CSV (`participant_N_analysis.csv`)

**CSV Columns:**

| Column | Description |
|--------|-------------|
| Exp Step (Word Order) | Sequential word number (1-25) |
| Participant Number | Participant ID from experiment |
| Participant Age | Age entered at experiment start |
| Participant Gender | Gender selected at experiment start |
| Reading End Timestamp | When audio finished (MM:SS.mmm from 00:00) |
| Writing Start Timestamp | First stroke start time (MM:SS.mmm from 00:00) |
| Writing End Timestamp | Last stroke end time (MM:SS.mmm from 00:00) |
| Number of Strokes | Total pen strokes for this word |
| Average Interval Between Strokes (ms) | Mean time between consecutive strokes |
| Target N Name | Name of target letter (e.g., "target 1") |
| Target N Start Time | When target letter begins (MM:SS.mmm from 00:00) |
| Target N End Time | When target letter ends (MM:SS.mmm from 00:00) |
| Target N From Last Stroke Start (ms) | Time from previous stroke start |
| Target N From Last Stroke End (ms) | Time from previous stroke end |

**Time Reference:** All timestamps use **audio start time as 00:00** (not clock time)

**Example Row:**
```csv
1,5,00:02.345,00:03.123,00:05.678,4,234.5,target 1,00:03.456,00:03.567,125.3,89.7,target 2,...
```

## 🎯 Research Applications

This toolkit is designed for analyzing:

- **Writing initiation delay** (audio end → first stroke)
- **Stroke timing patterns** (intervals between strokes)
- **Target letter formation** (specific letter timing within words)
- **Pressure patterns** during writing
- **Speed variations** across word writing
- **Group comparisons** (e.g., true vs false yod words)

## 🔧 Troubleshooting

### Audio files not detected
- Check file extensions: `.m4a`, `.mp3`, `.wav`
- Ensure files are in `src/recordings/` directory

### ffmpeg not found
- Install PsychoPy (includes ffmpeg)
- Or: `winget install ffmpeg`
- Or: Download from https://ffmpeg.org/download.html

### Words not slicing correctly
- Adjust silence detection parameters in `audio_processor.py`
- Lower `silence_thresh` for more sensitivity
- Increase `min_silence_len` for more separation

### Calibration issues
- Ensure paper corners are clearly marked
- Touch and hold (0.5s) each corner
- Tolerance is 15mm - corners auto-identified by position

### Tablet not responding
- Ensure tablet drivers are installed
- Check Windows Ink settings
- Try different USB port

## 📝 Notes

- All sliced words are **converted to WAV** format (pygame requirement)
- Original recordings remain unchanged in `src/recordings/`
- Participant data is **never overwritten** (unique timestamps)
- Target letter marks are **session-specific** (not saved to file)
- Hebrew text is fully supported with UTF-8 encoding

## 🎓 Citation

If using this toolkit for research, please cite appropriately and ensure IRB approval for human subjects research.

---

**Version:** 2.0  
**Author:** Research Assistant  
**Last Updated:** December 30, 2025
