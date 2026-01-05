<<<<<<< HEAD
# Touchpad Writing Experiment - Complete Guide

## 📋 Overview

This is a complete toolkit for conducting tablet/touchpad writing experiments with Hebrew words. The system records pen movements, pressure, and timing data while participants write words they hear through audio playback.

# TouchpadExperimentManager

Tablet/touchpad writing experiment manager + analyzer (PyQt5).

## Core workflow

- **Create a new experiment** in the GUI: select recordings from anywhere on the PC, label words, configure settings, export an experiment ZIP.
- **Run an experiment** by selecting the exported ZIP: the app extracts it and runs the experiment using the ZIP’s `audio/*.wav` and the config JSON.
- **Analyze results** using the built-in analyzer.

This repository intentionally does **not** include large datasets (recordings, generated slices, training data, debug assets, results, etc.).

## Run from source (developer)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main_interface.py
```

## Install on another PC (end user)

- Download the latest `TouchpadExperimentManager-portable.zip` from GitHub Releases.
- Unzip anywhere (avoid very long paths).
- Run `TouchpadExperimentManager.exe`.

## Updates (stable/beta channels)

The packaged app’s **Check for Updates** button uses:

- `update_config.json` (where to check)
- `version.json` (current version/channel inside the app)
- `releases/<channel>/version.json` and `releases/<channel>/version.sha256` (remote metadata)

Release/publishing instructions are in QUICK_START.md.

