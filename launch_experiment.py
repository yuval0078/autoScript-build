"""
Launcher script for the experiment runner - used by PyInstaller to create a separate executable
"""
import sys
import os

# Ensure the script directory is in the path
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    script_dir = os.path.dirname(sys.executable)
else:
    # Running as script
    script_dir = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, script_dir)

# Now import and run
import tablet_experiment

if __name__ == "__main__":
    tablet_experiment.main()
