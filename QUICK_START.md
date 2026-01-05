# Quick Start Guide

## 🔁 Releasing Updates (Stable/Beta)

1) Build:

`powershell -ExecutionPolicy Bypass -File build_pyinstaller.ps1 -Version v1.0.0 -Channel stable -RepoOwner yuval0078 -RepoName autoScript-build`

2) Commit + push the updated metadata:

`releases/<channel>/version.json` and `releases/<channel>/version.sha256`

3) Create a GitHub Release with tag = the same `-Version` and upload:

`TouchpadExperimentManager-portable.zip`

The app’s “Check for Updates” reads:
`https://raw.githubusercontent.com/yuval0078/autoScript-build/main/releases/<channel>/version.json`

## 🛠️ Installation

Create a venv and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 🖥️ GUI Mode (Recommended)

```
1. Run: python main_interface.py

2. Choose from:
   ┌────────────────────────────────┐
   │  [New Experiment]             │  Create & configure new experiment
   │  [Load Experiment]            │  Load saved ZIP package
   │  [Analyze Results]            │  Open stroke analyzer
   └────────────────────────────────┘

3. New Experiment:
   → Upload audio files (auto-sliced)
   → Enter Hebrew labels for each word
   → Configure grid size, order, beeps
   → Export as ZIP package

4. Load later on any computer!
```

## 🚀 Command Line Mode (3 Steps)

### Step 1: Prepare Audio 🎵

```
1. Prepare your audio files anywhere on the PC:
   ├── true_yod_f.m4a
   ├── false_yod_gramm.mp3
   └── other_recording.wav

2. Use the GUI “Create a New Experiment” to slice + label and export a ZIP.

3. Result: an experiment ZIP (contains `audio/*.wav`) + config JSON at ZIP root
```

### Step 2: Collect Data 📝

```
1. Run: python tablet_experiment.py

2. Calibration:
   ┌─────────────────┐
   │ •           •   │  Touch and hold (0.5s)
   │                 │  each corner of paper
   │                 │
   │ •           •   │
   └─────────────────┘
   → Press SPACE after calibration to continue

3. Participant Info:
   - Enter participant number
   - Enter age
   - Select gender (Male/Female/Other)

4. Experiment:
   - Listen to word → Write in cell
   - Press SPACE to continue
   - Press Ctrl+R to recalibrate (anytime)
   - Auto-saves to: results/participant_N/

5. Complete all 25 words
```

### Step 3: Analyze Results 📊

```
1. Run: python analyzer_refactored.py

2. Open participant_N_data.json

3. Features:
   ┌────────────────────────────────────┐
   │ Groups:              Animation:    │
   │ ├─ true_yod_f       ┌───────────┐ │
   │ │  ├─ Cell 0: איפוס│  Writing  │ │
   │ │  └─ Cell 1: אילוץ│  playback │ │
   │ └─ false_yod_gramm │           │ │
   │    ├─ Cell 2: משכל  └───────────┘ │
   │    └─ Cell 3: מטבל                │
   └────────────────────────────────────┘

4. Assign letters to strokes:
   - Navigate to stroke start event
   - Press Enter or click "🎯 Assign Letter"
   - Enter Hebrew letter (or empty for blocker)
   - Blockers display as '|'

5. Slice strokes if needed:
   - Navigate to desired split point
   - Click "✂ Slice Stroke Here"

6. RTL Keyboard Navigation:
   - LEFT arrow = Next event
   - RIGHT arrow = Previous event
   - Ctrl+arrows = Navigate strokes
```

## 🎯 Key Features

### Installation
- 🔧 One-click installer (`INSTALL.bat`)
- 📦 Auto-creates virtual environment
- ✅ Verifies all dependencies

### Audio Processor
- ✂️ Auto-slices words from recordings
- 🏷️ Interactive word labeling
- 🔄 Converts m4a/mp3/wav → wav
- 💾 Creates word_labels.json database

### Experiment
- 📏 4-point calibration (any order)
- ⏸️ Spacebar wait after calibration
- 👤 Collects age and gender
- 🔄 Ctrl+R recalibration anytime
- 🎲 Randomized word presentation
- ⏱️ Audio timing tracking
- 🖊️ Full pen data recording
- 📦 Single JSON output per participant

### Analyzer
- 🗂️ Hierarchical word grouping
- 🎬 Real-time animation playback
- ➡️ RTL navigation (Hebrew optimized)
- ✂️ Stroke slicing at any point
- 🚧 Blocker support (displayed as '|')
- 🎯 Letter assignment with timing
- 📊 Auto low-quality detection
- 📤 CSV/JSON export with demographics

## ⚡ Quick Commands

```powershell
# Installation (first time only)
python install.py
# Or double-click: INSTALL.bat

# All-in-one launcher (recommended)
python launcher.py
# Or double-click: START_HERE.bat

# GUI manager
python main_interface.py
# Or double-click: START_GUI.bat

# Individual tools:
python audio_processor.py       # Process audio files
python tablet_experiment.py      # Run experiment
python analyzer_refactored.py    # Analyze data
```

## 📂 File Structure After Processing

```
touchpad exp/
├── src/
│   ├── recordings/
│   │   ├── true_yod_f.m4a           # Your original files
│   │   └── false_yod_gramm.mp3
│   ├── sliced_words/
│   │   ├── true_yod_f_word_001.wav  # Auto-generated
│   │   ├── true_yod_f_word_002.wav
│   │   ├── false_yod_gramm_word_001.wav
│   │   └── ...
│   └── word_labels.json              # Auto-generated database
├── results/
│   ├── participant_1/
│   │   └── participant_1_data.json
│   ├── participant_2/
│   │   └── participant_2_data.json
│   └── ...
├── INSTALL.bat                       # Double-click to install!
├── START_HERE.bat                    # Double-click to launch menu!
├── START_GUI.bat                     # Double-click for GUI!
├── RUN_EXPERIMENT.bat                # Direct experiment launch
└── RUN_ANALYZER.bat                  # Direct analyzer launch
```

## 💡 Tips

1. **Installation:**
   - Run INSTALL.bat once on first setup
   - Requires Python 3.8+ and internet connection

2. **Audio Quality:**
   - Clear speech with distinct pauses between words
   - Background noise may affect word detection
   - Adjust `silence_thresh` if needed

3. **Calibration:**
   - Use clearly visible corner marks on paper
   - Touch firmly for 0.5 seconds
   - Corners auto-identified by position
   - Wait for SPACE prompt before continuing

4. **During Experiment:**
   - Wait for audio to finish before writing
   - Write naturally within the cell
   - Press SPACE when done with word
   - Press Ctrl+R if recalibration needed

5. **Analyzing Data:**
   - Use RTL navigation (LEFT = next, RIGHT = prev)
   - Press Enter on stroke start to assign letter
   - Leave empty for blocker (displays as '|')
   - Use slice tool to split strokes

## 🔧 Troubleshooting

| Problem | Solution |
|---------|----------|
| Install fails | Ensure Python 3.8+ in PATH, run as admin |
| No words detected | Lower `silence_thresh` in audio_processor.py |
| ffmpeg not found | Install: `winget install ffmpeg` |
| Calibration fails | Ensure 15mm accuracy, reset if needed |
| Tablet not working | Check drivers, Windows Ink settings |
| Audio won't play | Files auto-converted to WAV format |
| Need to recalibrate | Press Ctrl+R during experiment |

## 📞 Support

- Check README.md for detailed documentation
- Review code comments for implementation details
- Adjust parameters in respective .py files

---

**Happy Researching! 🎓**
