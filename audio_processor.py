"""
Audio Processor - Unified Tool
Combines audio slicing, word matching, and format conversion
Supports: m4a, mp3, wav input -> wav output
"""

import os
import sys
import subprocess
import numpy as np
import json
import glob
import shutil
from pathlib import Path

from app_paths import asset_path, ensure_dir, user_data_dir


class AudioProcessor:
    """Unified audio processing tool"""
    
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.ffmpeg_path = self.find_ffmpeg()
        self.ffprobe_path = self.find_ffprobe()
        self._configure_pydub()
        self.data_dir = user_data_dir()
        # Keep a small user-writable workspace for labels/temp.
        # Note: GUI flows select audio from arbitrary locations and export/run from ZIPs,
        # so we do not require persistent recordings/sliced_words folders.
        self.src_dir = ensure_dir(self.data_dir / 'src')
        self.recordings_dir = self.src_dir / 'recordings'
        self.sliced_words_dir = self.src_dir / 'sliced_words'
        self.word_labels_file = self.src_dir / 'word_labels.json'

        # Seed labels DB from shipped assets on first run (if present)
        if not self.word_labels_file.exists():
            default_labels = asset_path('src/word_labels.json')
            if default_labels.exists():
                try:
                    shutil.copy(default_labels, self.word_labels_file)
                except Exception:
                    pass
        
    def log(self, message):
        """Print message only if verbose is True"""
        if self.verbose:
            print(message)

    def find_ffmpeg(self):
        """Find ffmpeg executable"""
        # Prefer a bundled ffmpeg (works on other PCs with no installs)
        bundled = asset_path('assets/bin/ffmpeg.exe')
        if bundled.exists():
            return str(bundled)

        # Try PsychoPy installation first
        psychopy_ffmpeg = Path(r"C:\Program Files\PsychoPy\share\ffpyplayer\ffmpeg\bin\ffmpeg.exe")
        if psychopy_ffmpeg.exists():
            return str(psychopy_ffmpeg)
        
        # Try system PATH
        try:
            result = subprocess.run(['ffmpeg', '-version'], 
                                  capture_output=True, 
                                  text=True, 
                                  check=True)
            return 'ffmpeg'
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        
        if self.verbose:
            print("⚠ Warning: ffmpeg not found. Please install ffmpeg:")
            print("  Download from: https://ffmpeg.org/download.html")
            print("  Or use: winget install ffmpeg")
        return None

    def find_ffprobe(self):
        """Find ffprobe executable (optional but improves metadata/duration handling)."""
        bundled = asset_path('assets/bin/ffprobe.exe')
        if bundled.exists():
            return str(bundled)

        if self.ffmpeg_path and self.ffmpeg_path.lower().endswith('ffmpeg.exe'):
            candidate = Path(self.ffmpeg_path).with_name('ffprobe.exe')
            if candidate.exists():
                return str(candidate)

        # If ffmpeg is coming from PATH, ffprobe is typically alongside it.
        try:
            subprocess.run(['ffprobe', '-version'], capture_output=True, text=True, check=True)
            return 'ffprobe'
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None

    def _configure_pydub(self):
        """Point pydub at our ffmpeg/ffprobe paths (bundled or system)."""
        try:
            from pydub import AudioSegment

            if self.ffmpeg_path:
                AudioSegment.converter = self.ffmpeg_path
            if getattr(self, 'ffprobe_path', None):
                AudioSegment.ffprobe = self.ffprobe_path
        except Exception:
            return
    
    def convert_to_wav(self, input_file, output_wav):
        """Convert audio file (m4a/mp3/wav) to wav format"""
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg not available")
        
        input_path = Path(input_file)
        
        # If already wav, just copy
        if input_path.suffix.lower() == '.wav':
            self.log(f"  ℹ Already WAV format: {input_path.name}")
            if str(input_path) != str(output_wav):
                import shutil
                shutil.copy(input_path, output_wav)
            return output_wav
        
        self.log(f"  Converting {input_path.name} to WAV...")
        cmd = [
            self.ffmpeg_path, "-i", str(input_file),
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            "-ac", "1",
            "-y",  # Overwrite without asking
            str(output_wav)
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
            self.log(f"  ✓ Converted to WAV")
            
            # Debug: check file size
            if os.path.exists(output_wav):
                size = os.path.getsize(output_wav)
                self.log(f"  ℹ WAV size: {size} bytes")
                if size < 100:
                    self.log(f"  ⚠ Warning: WAV file is too small!")
                    self.log(f"  ffmpeg stderr:\n{result.stderr}")
                
            return output_wav
        except subprocess.CalledProcessError as e:
            self.log(f"  ✗ Error converting: {e.stderr}")
            raise
    
    def get_temp_segment_file(self, input_file, start_ms, end_ms, context="default"):
        """
        Extract a segment to a temporary WAV file for playback.
        
        Args:
            input_file (str): Path to source audio
            start_ms (int): Start time in ms
            end_ms (int): End time in ms
            context (str): Context name to avoid file conflicts between different players
            
        Returns:
            str: Path to temporary WAV file for playback
        """
        try:
            from pydub import AudioSegment

            temp_dir = ensure_dir(self.data_dir / 'temp')

            # Ensure we have a WAV to work with
            temp_full_wav = str(temp_dir / f"temp_full_source_{context}.wav")
            self.convert_to_wav(input_file, temp_full_wav)
            
            sound = AudioSegment.from_wav(temp_full_wav)
            
            # Handle bounds
            start_ms = max(0, int(start_ms))
            end_ms = min(len(sound), int(end_ms))
            
            segment = sound[start_ms:end_ms]
            
            # Export to context-specific temp file for playback
            temp_playback = str(temp_dir / f"temp_playback_{context}.wav")
            segment.export(temp_playback, format="wav")
            
            # Cleanup full temp
            if os.path.exists(temp_full_wav):
                try:
                    os.remove(temp_full_wav)
                except:
                    pass
                    
            return temp_playback
            
        except Exception as e:
            self.log(f"Error extracting segment: {e}")
            return None

    def detect_nonsilent(self, audio_data, sample_rate, silence_thresh=0.01, min_silence_len=300, min_word_len=100):
        """
        Detect non-silent regions in audio data.
        
        Args:
            audio_data: numpy array of audio samples
            sample_rate: sample rate in Hz
            silence_thresh: amplitude threshold (0-1, lower = more sensitive)
            min_silence_len: minimum silence length in ms
            min_word_len: minimum word length in ms
        
        Returns:
            List of (start_sample, end_sample) tuples
        """
        self.log("Detecting non-silent regions...")
        self.log(f"Sample rate: {sample_rate}, Silence threshold: {silence_thresh}")

        # Convert to absolute amplitude
        abs_audio = np.abs(audio_data)
        
        # Detect where audio is above threshold (non-silent)
        is_speech = abs_audio > silence_thresh
        
        # Convert ms to samples
        min_silence_samples = int(min_silence_len * sample_rate / 1000)
        min_word_samples = int(min_word_len * sample_rate / 1000)
        
        # Find speech segments
        segments = []
        in_speech = False
        start = 0
        silence_count = 0

        for i, sample in enumerate(is_speech):
            if sample:
                if not in_speech:
                    in_speech = True
                    start = i
                silence_count = 0
            else:
                if in_speech:
                    silence_count += 1
                    if silence_count > min_silence_samples:
                        end = i - silence_count
                        if end - start >= min_word_samples:
                            segments.append((start, end))
                        in_speech = False

        # Handle last segment
        if in_speech:
            end = len(is_speech) - 1
            if end - start >= min_word_samples:
                segments.append((start, end))

        self.log(f"Detected segments: {segments}")
        return segments
    
    def slice_audio_file(self, input_file, output_dir, base_name):
        """
        Slice audio file into separate word segments using pydub
        
        Returns:
            List of dicts with start/end times
        """
        self.log(f"\n📂 Processing: {input_file}")

        temp_dir = ensure_dir(self.data_dir / 'temp')
        
        # Convert to WAV first (pydub works best with wav)
        temp_wav = str(temp_dir / "temp_audio.wav")
        self.convert_to_wav(input_file, temp_wav)
        
        try:
            # Fix for Python 3.13 where audioop is removed
            import sys
            if sys.version_info >= (3, 13):
                try:
                    import audioop  # noqa: F401
                except ImportError:
                    self.log("  ⚠ Warning: audioop not found (required for pydub on Python 3.13+)")
                    self.log("  Please run: pip install audioop-lts")

            from pydub import AudioSegment
            from pydub.silence import detect_nonsilent

            # Configure pydub to use our ffmpeg/ffprobe if we found one
            if self.ffmpeg_path:
                probe_candidate = Path(self.ffmpeg_path).with_name("ffprobe.exe")
                # pydub uses ffprobe for duration/metadata lookups; set it if available
                if probe_candidate.exists():
                    AudioSegment.ffprobe = str(probe_candidate)
            
            # Load audio
            self.log(f"  Loading audio...")
            sound = AudioSegment.from_wav(temp_wav)
            
            # Detect non-silent chunks
            self.log(f"  🔍 Detecting words (pydub smart split)...")
            self.log(f"  Audio dBFS: {sound.dBFS:.2f}")
            
            # Use detect_nonsilent to get start/end times
            ranges = detect_nonsilent(
                sound, 
                min_silence_len=200,
                silence_thresh=-30
            )
            
            # Apply keep_silence (expand ranges)
            keep_silence = 100
            expanded_ranges = []
            for start, end in ranges:
                start = max(0, start - keep_silence)
                end = min(len(sound), end + keep_silence)
                expanded_ranges.append((start, end))
            
            ranges = expanded_ranges
            
            # Filter out short chunks (likely noise/clicks)
            min_word_len = 300  # ms
            ranges = [r for r in ranges if (r[1] - r[0]) >= min_word_len]
            
            if not ranges:
                self.log(f"  ⚠ No words detected")
                if os.path.exists(temp_wav):
                    os.remove(temp_wav)
                return []
            
            self.log(f"  ✓ Found {len(ranges)} words")
            
            # Create segments list
            segments = []
            for i, (start, end) in enumerate(ranges, 1):
                duration_ms = end - start
                self.log(f"    {i:2d}. {start}ms - {end}ms ({duration_ms}ms)")
                segments.append({
                    'index': i,
                    'start': start,
                    'end': end,
                    'duration': duration_ms
                })
                
        except ImportError:
            self.log("  ✗ pydub not installed. Please run: pip install pydub")
            return []
        except Exception as e:
            self.log(f"  ✗ Error processing audio: {e}")
            if os.path.exists(temp_wav):
                os.remove(temp_wav)
            raise
        
        # Clean up temp file
        if os.path.exists(temp_wav):
            os.remove(temp_wav)
        
        return segments
    
    def process_single_file(self, file_path):
        """
        Process a single audio file and return its segments.
        This is the preferred API method for external scripts.
        
        Args:
            file_path (str): Path to the audio file
            
        Returns:
            list: List of dicts {'index', 'start', 'end', 'duration'}
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
            
        # Detect word segments; does not require writing slices to disk.
        return self.slice_audio_file(str(file_path), output_dir=None, base_name=file_path.stem)

    def load_or_create_labels(self):
        """Load existing word labels or create new database"""
        if self.word_labels_file.exists():
            with open(self.word_labels_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            return {}
    
    def save_labels(self, labels):
        """Save word labels to JSON"""
        with open(self.word_labels_file, 'w', encoding='utf-8') as f:
            json.dump(labels, f, ensure_ascii=False, indent=2)
    
    def update_labels_database(self, recording_name, segments, labels):
        """Update labels database with new word segments"""
        # Create group if it doesn't exist
        if recording_name not in labels:
            labels[recording_name] = []
        
        # Get existing start times to avoid duplicates
        # We use start time as a unique identifier for the segment
        existing_starts = {item.get('start') for item in labels[recording_name]}
        
        # Add new word segments
        new_count = 0
        for seg in segments:
            if seg['start'] not in existing_starts:
                labels[recording_name].append({
                    'id': f"{recording_name}_word_{seg['index']:03d}",
                    'start': seg['start'],
                    'end': seg['end'],
                    'word': ''
                })
                new_count += 1
        
        return new_count
    
    def get_unlabeled_words(self, labels):
        """Get all words without labels"""
        unlabeled = []
        for recording_name, words in labels.items():
            for word_item in words:
                if word_item['word'] == '':
                    unlabeled.append((recording_name, word_item))
        return unlabeled
    
    def interactive_labeling(self):
        """Interactive labeling of words"""
        labels = self.load_or_create_labels()
        unlabeled = self.get_unlabeled_words(labels)
        
        if not unlabeled:
            print("\n✓ All words are already labeled!")
            return
        
        print(f"\n📝 Labeling {len(unlabeled)} words...")
        print("  (Enter Hebrew text for each word, or 'q' to quit)")
        print()
        
        # Cache for loaded audio files to avoid reloading
        loaded_audio = {}
        
        for recording_name, word_item in unlabeled:
            word_id = word_item.get('id', 'unknown')
            start_ms = word_item.get('start', 0)
            end_ms = word_item.get('end', 0)
            
            print(f"Group: {recording_name}")
            print(f"ID:    {word_id}")
            print(f"Time:  {start_ms}ms - {end_ms}ms")
            
            # Find source file
            source_file = None
            # Try to find the source file in recordings dir
            # We assume the recording name matches the file stem
            candidates = list(self.recordings_dir.glob(f"{recording_name}.*"))
            if candidates:
                source_file = candidates[0]
            
            if source_file:
                print(f"  ▶ Playing audio segment...")
                try:
                    # Load audio if not in cache
                    if str(source_file) not in loaded_audio:
                        # We need pydub here
                        from pydub import AudioSegment
                        # Fix for Python 3.13
                        import sys
                        if sys.version_info >= (3, 13):
                            try:
                                import audioop
                            except ImportError:
                                pass
                        
                        # Convert source to wav for pydub
                        temp_play_wav = "temp_play.wav"
                        self.convert_to_wav(source_file, temp_play_wav)
                        loaded_audio[str(source_file)] = AudioSegment.from_wav(temp_play_wav)
                        if os.path.exists(temp_play_wav):
                            os.remove(temp_play_wav)
                            
                    sound = loaded_audio[str(source_file)]
                    
                    # Extract segment
                    # Add a bit of padding if possible? No, exact is better for checking.
                    segment = sound[start_ms:end_ms]
                    
                    # Export to temp file for pygame
                    temp_segment_wav = "temp_segment.wav"
                    segment.export(temp_segment_wav, format="wav")
                    
                    import pygame
                    if not pygame.mixer.get_init():
                        pygame.mixer.init(frequency=44100)
                    pygame.mixer.music.load(temp_segment_wav)
                    pygame.mixer.music.play()
                    
                    # Wait for playback to finish
                    while pygame.mixer.music.get_busy():
                        pygame.time.Clock().tick(10)
                        
                    # Cleanup
                    pygame.mixer.music.unload()
                    if os.path.exists(temp_segment_wav):
                        try:
                            os.remove(temp_segment_wav)
                        except PermissionError:
                            pass # Sometimes file is locked
                            
                except Exception as e:
                    print(f"  ⚠ Could not play audio: {e}")
            else:
                print(f"  ⚠ Source file not found for playback")
            
            # Get label
            label = input("  Word: ").strip()
            
            if label.lower() == 'q':
                print("\n  Saving and exiting...")
                break
            
            if label:
                word_item['word'] = label
                self.save_labels(labels)
                print(f"  ✓ Saved: '{label}'")
            else:
                print(f"  ⊙ Skipped (empty)")
            
            print()
        
        # Summary
        remaining = len(self.get_unlabeled_words(labels))
        if remaining == 0:
            print("✓ All words labeled!")
        else:
            print(f"ℹ {remaining} words remaining")
    
    def process_all_recordings(self):
        """
        Process all audio files in recordings directory
        
        Returns:
            dict: The complete labels database
        """
        if not self.recordings_dir.exists():
            self.log(f"✗ Recordings directory not found: {self.recordings_dir}")
            self.log(f"  Please create it and add your audio files (m4a, mp3, or wav)")
            return {}
        
        # Find all audio files
        audio_files = set()
        for ext in ['*.m4a', '*.mp3', '*.wav', '*.M4A', '*.MP3', '*.WAV']:
            audio_files.update(self.recordings_dir.glob(ext))
        
        audio_files = sorted(list(audio_files))
        
        if not audio_files:
            self.log(f"✗ No audio files found in {self.recordings_dir}")
            self.log(f"  Supported formats: m4a, mp3, wav")
            return {}
        
        self.log(f"╔══════════════════════════════════════════╗")
        self.log(f"║  Audio Processor - Slicer                ║")
        self.log(f"╚══════════════════════════════════════════╝")
        self.log(f"\nFound {len(audio_files)} audio files to process")
        
        # Load existing labels
        labels = self.load_or_create_labels()
        
        total_words = 0
        
        # Process each file
        for audio_file in audio_files:
            base_name = audio_file.stem
            
            # Slice audio
            segments = self.slice_audio_file(
                str(audio_file), 
                str(self.sliced_words_dir),
                base_name
            )
            
            # Special check for "Dec 31 at 10-14"
            if base_name == "Dec 31 at 10-14":
                if len(segments) != 6:
                    self.log(f"\n❌ Error: Expected 6 words for '{base_name}', but found {len(segments)}.")
                    self.log("  Terminating program as requested.")
                    sys.exit(1)
            
            if segments:
                # Update labels database
                new_count = self.update_labels_database(base_name, segments, labels)
                total_words += len(segments)
                
                if new_count > 0:
                    self.log(f"  ✓ Added {new_count} new words to database")
        
        # Save labels
        self.save_labels(labels)
        
        self.log(f"\n✓ Processing complete!")
        self.log(f"  Total words: {total_words}")
        self.log(f"  Output: {self.sliced_words_dir} (No files created, only instructions)")
        self.log(f"  Database: {self.word_labels_file}")
        
        return labels
    
    def run(self):
        """Main workflow"""
        self.log(f"╔══════════════════════════════════════════╗")
        self.log(f"║  Audio Processor                         ║")
        self.log(f"║  Unified Slicer + Matcher + Converter    ║")
        self.log(f"╚══════════════════════════════════════════╝")
        print()
        
        if not self.ffmpeg_path:
            self.log("✗ Cannot proceed without ffmpeg")
            return
        
        # Create directories
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.sliced_words_dir.mkdir(parents=True, exist_ok=True)
        
        # Step 1: Process recordings (slice + convert to wav)
        self.process_all_recordings()
        
        # Step 2: Interactive labeling
        print()
        self.log(f"╔══════════════════════════════════════════╗")
        self.log(f"║  Audio Processor - Word Matching         ║")
        self.log(f"╚══════════════════════════════════════════╝")
        self.interactive_labeling()
        
        print()
        self.log("✓ Audio processing complete!")
        self.log(f"  Sliced words: {self.sliced_words_dir}")
        self.log(f"  Word labels: {self.word_labels_file}")


def main():
    processor = AudioProcessor()
    processor.run()


if __name__ == "__main__":
    main()
