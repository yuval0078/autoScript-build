"""
Convert all m4a audio files to wav format for pygame compatibility
"""
import os
import subprocess
from pathlib import Path

def convert_m4a_to_wav(m4a_file, wav_file):
    """Convert a single m4a file to wav using ffmpeg"""
    # Use full path to ffmpeg
    ffmpeg_path = r"C:\Program Files\PsychoPy\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe"
    
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
    # Directory containing sliced words
    words_dir = Path('src/sliced_words')
    
    if not words_dir.exists():
        print(f"✗ Directory not found: {words_dir}")
        return
    
    # Find all m4a files
    m4a_files = list(words_dir.glob('*.m4a'))
    
    if not m4a_files:
        print("✗ No m4a files found")
        return
    
    print(f"Found {len(m4a_files)} m4a files to convert...")
    
    converted = 0
    for m4a_file in m4a_files:
        wav_file = m4a_file.with_suffix('.wav')
        
        # Skip if wav already exists
        if wav_file.exists():
            print(f"⊙ Skipping {m4a_file.name} (wav already exists)")
            converted += 1
            continue
        
        print(f"Converting {m4a_file.name}...", end=' ')
        if convert_m4a_to_wav(m4a_file, wav_file):
            print("✓")
            converted += 1
        else:
            print("✗")
    
    print(f"\n✓ Conversion complete: {converted}/{len(m4a_files)} files")

if __name__ == '__main__':
    main()
