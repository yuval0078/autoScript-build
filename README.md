<<<<<<< HEAD
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
  # TouchpadExperimentManager

  Tablet/touchpad writing experiment manager + analyzer (PyQt5). Lets you:

  - Create a new experiment (slice audio, label words, configure, export ZIP)
  - Load an exported experiment ZIP and run it
  - Run the tablet experiment and save participant results
  - Analyze results and export CSV

  This repository intentionally does NOT include large datasets (audio recordings, training PDFs, model files, debug folders). Those are kept local and ignored via `.gitignore`.

  ## Run from source

  1) Create a venv and install dependencies:

  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  ```

  2) Start the GUI:

  ```powershell
  python main_interface.py
  ```

  ## Audio input folders (local)

  - For **Create a New Experiment**, you select audio files from **any folder** on the PC.
  - For **Run Experiment**, the app uses the per-word WAV files packaged inside the experiment ZIP under `audio/`.
  - The word database template is `src/word_labels.json` (committed).

  ## Releases / auto-update metadata

  See `update_config.json`, `version.json`, and `releases/<channel>/version.json`.
  For the release workflow, see QUICK_START.md.

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
=======
# autoScript-build
>>>>>>> origin/main
