"""
Convert all m4a audio files to wav format for pygame compatibility
"""
import os
import sys
import subprocess
from pathlib import Path

def find_ffmpeg():
    """Find ffmpeg executable in common locations"""
    # Try PsychoPy installation first
    psychopy_ffmpeg = Path(r"C:\Program Files\PsychoPy\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe")
    if psychopy_ffmpeg.exists():
        return str(psychopy_ffmpeg)
    
    # Try system PATH
    try:
        subprocess.run(['ffmpeg', '-version'], 
                      capture_output=True, 
                      text=True, 
                      check=True)
        return 'ffmpeg'
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    
    print("⚠ Warning: ffmpeg not found. Please install ffmpeg:")
    print("  Download from: https://ffmpeg.org/download.html")
    print("  Or use: winget install ffmpeg")
    return None

def convert_m4a_to_wav(m4a_file, wav_file, ffmpeg_path=None):
    """Convert a single m4a file to wav using ffmpeg"""
    if ffmpeg_path is None:
        ffmpeg_path = find_ffmpeg()
    
    if ffmpeg_path is None:
        print(f"✗ Error: ffmpeg not available")
        return False
    
    try:
        subprocess.run(
            [ffmpeg_path, '-y', '-i', str(m4a_file), '-acodec', 'pcm_s16le', '-ar', '44100', str(wav_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"✗ Error converting {m4a_file}: {e}")
        return False

def main():
    # Get directory from command line or use current directory
    if len(sys.argv) > 1:
        words_dir = Path(sys.argv[1])
    else:
        words_dir = Path.cwd()
    
    if not words_dir.exists():
        print(f"✗ Directory not found: {words_dir}")
        print(f"  Usage: python convert_audio.py [directory_path]")
        return
    
    # Find all m4a files
    m4a_files = list(words_dir.glob('*.m4a'))
    
    if not m4a_files:
        print(f"✗ No m4a files found in {words_dir}")
        return
    
    print(f"Found {len(m4a_files)} m4a files to convert...")
    print(f"Directory: {words_dir.absolute()}")
    
    # Find ffmpeg once
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return
    
    converted = 0
    for m4a_file in m4a_files:
        wav_file = m4a_file.with_suffix('.wav')
        
        # Skip if wav already exists
        if wav_file.exists():
            print(f"⊙ Skipping {m4a_file.name} (wav already exists)")
            converted += 1
            continue
        
        print(f"Converting {m4a_file.name}...", end=' ')
        if convert_m4a_to_wav(m4a_file, wav_file, ffmpeg_path):
            print("✓")
            converted += 1
        else:
            print("✗")
    
    print(f"\n✓ Conversion complete: {converted}/{len(m4a_files)} files")

if __name__ == '__main__':
    main()
