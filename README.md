<<<<<<< HEAD
# Touchpad Writing Experiment - Complete Guide

## 📋 Overview

This is a complete toolkit for conducting tablet/touchpad writing experiments with Hebrew words. The system records pen movements, pressure, and timing data while participants write words they hear through audio playback.

## 🚀 Quick Installation

1. **Double-click `INSTALL.bat`** to set up everything automatically
2. The installer will:
   - Create a virtual environment
  # TouchpadExperimentManager

  Tablet/touchpad writing experiment manager + analyzer (PyQt5).

  Core workflow:
  - Create a new experiment (slice audio, label words, configure, export ZIP)
  - Run an exported experiment ZIP (uses the ZIP’s `audio/*.wav`)
  - Analyze results and export CSV

  This repository intentionally does NOT include large datasets (recordings, generated slices, training PDFs, results, etc.).

  ## Run from source

  ```powershell
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -r requirements.txt
  python main_interface.py
  ```

  ## Notes about audio and labels

  - When creating a new experiment, you select audio files from **any folder** on the PC.
  - When running an experiment, audio slices and labels are loaded from the exported experiment ZIP:
    - config JSON at ZIP root
    - `audio/` folder containing per-word WAV files

  ## Releases / auto-update metadata

  See `update_config.json`, `version.json`, and `releases/<channel>/version.json`.
  For the release workflow, see QUICK_START.md.

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
