# Touchpad Experiment Manager - Portable Build Guide

This guide explains how to create a portable, self-contained executable that runs on any Windows PC without requiring Python or other dependencies.

## Prerequisites (Build Machine Only)

These are only needed on the machine where you BUILD the package:

- Windows PC
- Python 3.8 or higher
- Internet connection (to download dependencies)

## Quick Start - Build Portable Package

1. **Open PowerShell** in the project folder
   - Right-click the folder → "Open in Terminal" or "Open PowerShell window here"

2. **Run the build script:**
   ```powershell
   .\build_portable.ps1
   ```

3. **Wait for completion** (2-5 minutes)
   - The script will install dependencies
   - Build the executable with PyInstaller
   - Create a portable ZIP file

4. **Find your package:**
   - Look for `TouchpadExperiment_Portable_YYYYMMDD.zip` in the project folder
   - This ZIP contains everything needed to run on any PC!

## What Gets Packaged

The portable package includes:

✅ **Executable** - `TouchpadExperiment.exe` (no Python needed!)
✅ **All Dependencies** - PyQt5, pygame, numpy, pydub (bundled)
✅ **All Python Files** - Your application code
✅ **ffmpeg** (optional) - For audio processing, if available

## Distribution

### To Share With Others:

1. Upload the ZIP file to cloud storage, email, or USB drive
2. Users extract it anywhere on their PC (Desktop, Documents, etc.)
3. Users run `TouchpadExperiment.exe`
4. **No installation required!**

### System Requirements (Target PC):

- Windows 7 or higher
- No Python installation needed
- No pip or external packages needed
- ~100-200 MB disk space when extracted

## Manual Build (Alternative)

If you prefer to build manually:

```powershell
# 1. Install dependencies
pip install -r requirements.txt

# 2. Build with PyInstaller
pyinstaller --clean TouchpadExperiment.spec

# 3. Package the dist folder
Compress-Archive -Path "dist\TouchpadExperiment" -DestinationPath "TouchpadExperiment_Portable.zip"
```

## Troubleshooting

### Build Fails - Missing Modules

**Problem:** PyInstaller can't find a module
**Solution:** Add it to `hiddenimports` in `TouchpadExperiment.spec`

```python
hiddenimports=[
    'PyQt5.QtCore',
    'your_missing_module',  # Add here
],
```

### Application Won't Start on Target PC

**Problem:** Missing Visual C++ Runtime
**Solution:** User needs to install Microsoft Visual C++ Redistributable:
- Download from: https://aka.ms/vs/17/release/vc_redist.x64.exe

### Audio Features Not Working

**Problem:** ffmpeg not bundled
**Solution:** 
1. Download ffmpeg: https://ffmpeg.org/download.html
2. Extract `ffmpeg.exe` and `ffprobe.exe`
3. Place in `dist\TouchpadExperiment\assets\bin\` folder before zipping

## Project Dependencies

The project uses these packages (bundled automatically):

- **PyQt5** - GUI framework
- **pygame** - Audio playback
- **numpy** - Numerical processing
- **pydub** - Audio editing
- **PyInstaller** - Creates the executable

## File Structure After Build

```
TouchpadExperiment_Portable.zip
└── TouchpadExperiment/
    ├── TouchpadExperiment.exe        # Main executable
    ├── _internal/                     # Bundled dependencies
    │   ├── PyQt5/                    # GUI framework
    │   ├── pygame/                   # Audio
    │   ├── numpy/                    # Processing
    │   └── ... (all dependencies)
    └── assets/                       # Optional resources
        └── bin/
            ├── ffmpeg.exe (optional)
            └── ffprobe.exe (optional)
```

## Advanced Options

### Reduce File Size

Edit `TouchpadExperiment.spec`:

```python
exe = EXE(
    # ...
    upx=True,           # Enable compression (already on)
    console=False,      # Keep False for GUI
)
```

### Add Application Icon

1. Create or download an `.ico` file
2. Edit `TouchpadExperiment.spec`:

```python
exe = EXE(
    # ...
    icon='your_icon.ico',
)
```

### Include Additional Files

Edit `TouchpadExperiment.spec`:

```python
datas=[
    ('assets', 'assets'),      # Include assets folder
    ('config.json', '.'),      # Include config file
],
```

## Support

For issues or questions:
- Check `build.log` in the project folder
- Review PyInstaller documentation: https://pyinstaller.org/
- Ensure all Python files are in the project directory

---

**Last Updated:** January 2026
