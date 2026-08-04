"""One-time deterministic source migration for ZIP and temp-file safety."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_exact(relative_path: str, old: str, new: str, expected: int = 1) -> None:
    path = ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(
            f"Expected {expected} occurrence(s) in {relative_path}, found {count}: {old!r}"
        )
    path.write_text(text.replace(old, new), encoding="utf-8")


def main() -> None:
    replace_exact(
        "gui_menu.py",
        "from app_paths import ensure_dir, user_data_dir, asset_path, source_script_path\n",
        "from app_paths import ensure_dir, user_data_dir, asset_path, source_script_path\n"
        "from archive_utils import safe_extract_zip\n",
    )
    replace_exact(
        "gui_menu.py",
        "                    zip_ref.extractall(extract_dir)\n",
        "                    safe_extract_zip(zip_ref, extract_dir)\n",
    )

    replace_exact(
        "exp_initializer.py",
        "from app_paths import ensure_dir, user_data_dir\n",
        "from app_paths import ensure_dir, user_data_dir\n"
        "from archive_utils import safe_extract_zip\n",
    )
    replace_exact(
        "exp_initializer.py",
        "            zip_ref.extractall(extract_dir)\n",
        "            safe_extract_zip(zip_ref, extract_dir)\n",
    )

    replace_exact(
        "audio_processor.py",
        "from app_paths import asset_path, ensure_dir, user_data_dir\n",
        "from app_paths import asset_path, ensure_dir, user_data_dir\n"
        "from archive_utils import unique_temp_path\n",
    )
    replace_exact(
        "audio_processor.py",
        "            temp_full_wav = str(temp_dir / f\"temp_full_source_{context}.wav\")\n",
        "            temp_full_wav = str(unique_temp_path(temp_dir, prefix=f\"full_source_{context}_\"))\n",
    )
    replace_exact(
        "audio_processor.py",
        "            temp_playback = str(temp_dir / f\"temp_playback_{context}.wav\")\n",
        "            temp_playback = str(unique_temp_path(temp_dir, prefix=f\"playback_{context}_\"))\n",
    )
    replace_exact(
        "audio_processor.py",
        "        temp_wav = str(temp_dir / f\"temp_analysis_{Path(input_file).stem}.wav\")\n",
        "        temp_wav = str(unique_temp_path(temp_dir, prefix=f\"analysis_{Path(input_file).stem}_\"))\n",
    )
    replace_exact(
        "audio_processor.py",
        "        temp_wav = str(temp_dir / \"temp_audio.wav\")\n",
        "        temp_wav = str(unique_temp_path(temp_dir, prefix=f\"slice_{Path(input_file).stem}_\"))\n",
    )

    replace_exact(
        "TouchpadExperiment.spec",
        "    ('app_paths.py', '.'),\n    ('project_version.py', '.'),\n",
        "    ('app_paths.py', '.'),\n    ('archive_utils.py', '.'),\n    ('project_version.py', '.'),\n",
    )


if __name__ == "__main__":
    main()
