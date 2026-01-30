"""
Launcher script for the analyzer - used by PyInstaller to create a separate executable
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
import analyzer_refactored

if __name__ == "__main__":
    analyzer_refactored.main()
