import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app_paths import ensure_dir, user_data_dir


@dataclass(frozen=True)
class UpdateCheckResult:
    update_available: bool
    channel: str
    local_version: str
    remote_version: str
    download_url: str | None = None
    checksum_url: str | None = None


def _tokenize_prerelease(text: str) -> list[object]:
    parts: list[object] = []
    for chunk in re.split(r"[._]", text):
        if not chunk:
            continue
        m = re.fullmatch(r"(\d+)([a-zA-Z].*)?", chunk)
        if m:
            parts.append(int(m.group(1)))
            if m.group(2):
                parts.append(m.group(2).lower())
        else:
            parts.append(chunk.lower())
    return parts


def _parse_version(v: str) -> tuple[tuple[int, ...], tuple[int, list[object]]]:
    """Return (release_tuple, (stability_rank, prerelease_tokens)).

    stability_rank: 1 for prerelease, 2 for final release.
    """
    v = (v or "").strip()
    if v.lower().startswith("v"):
        v = v[1:]

    base, sep, suffix = v.partition("-")
    release_parts: list[int] = []
    for part in base.split("."):
        m = re.match(r"^(\d+)", part)
        release_parts.append(int(m.group(1)) if m else 0)

    if not sep:
        return (tuple(release_parts), (2, []))

    return (tuple(release_parts), (1, _tokenize_prerelease(suffix)))


def is_remote_newer(remote_version: str, local_version: str) -> bool:
    r_rel, (r_rank, r_pre) = _parse_version(str(remote_version))
    l_rel, (l_rank, l_pre) = _parse_version(str(local_version))

    # Compare release tuple with padding.
    max_len = max(len(r_rel), len(l_rel))
    r_rel_p = r_rel + (0,) * (max_len - len(r_rel))
    l_rel_p = l_rel + (0,) * (max_len - len(l_rel))
    if r_rel_p != l_rel_p:
        return r_rel_p > l_rel_p

    # Same release numbers: final > prerelease.
    if r_rank != l_rank:
        return r_rank > l_rank

    # Both final.
    if r_rank == 2:
        return False

    # Both prerelease: compare tokens.
    for a, b in zip(r_pre, l_pre):
        if a == b:
            continue
        a_is_int = isinstance(a, int)
        b_is_int = isinstance(b, int)
        if a_is_int and b_is_int:
            return a > b
        if a_is_int and not b_is_int:
            return True
        if not a_is_int and b_is_int:
            return False
        return str(a) > str(b)

    return len(r_pre) > len(l_pre)


def fetch_json(url: str, timeout_s: int = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return __import__("json").loads(resp.read().decode("utf-8"))


def fetch_checksum_sha256(checksum_url: str, timeout_s: int = 10) -> str:
    with urllib.request.urlopen(checksum_url, timeout=timeout_s) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()

    # Typical format: "<hash>  TouchpadExperimentManager-portable.zip"
    token = (text.split() or [""])[0].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError(f"Invalid SHA256 format from checksum URL: {checksum_url}")
    return token


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def download_to(url: str, dest: Path, timeout_s: int = 30) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "TouchpadExperimentManager-Updater"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def find_app_executable(extract_dir: Path) -> Path:
    # Expected layout from build script: <extract_dir>/TouchpadExperimentManager/TouchpadExperimentManager.exe
    expected = extract_dir / "TouchpadExperimentManager" / "TouchpadExperimentManager.exe"
    if expected.exists():
        return expected

    # Fallback: find any .exe in first 2 levels.
    candidates = []
    for p in extract_dir.rglob("*.exe"):
        try:
            rel = p.relative_to(extract_dir)
            if len(rel.parts) <= 2:
                candidates.append(p)
        except Exception:
            candidates.append(p)

    if not candidates:
        raise FileNotFoundError("No executable found in extracted update")
    # Prefer an exe matching the folder name.
    for c in candidates:
        if c.name.lower().startswith("touchpadexperimentmanager"):
            return c
    return candidates[0]


def launch_detached(exe_path: Path) -> None:
    exe_path = exe_path.resolve()
    cwd = exe_path.parent

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    subprocess.Popen(
        [str(exe_path)],
        cwd=str(cwd),
        close_fds=True,
        creationflags=creationflags,
    )


def get_update_paths(remote_version: str) -> tuple[Path, Path]:
    base = ensure_dir(user_data_dir() / "updates")
    safe_ver = re.sub(r"[^0-9A-Za-z._-]+", "_", str(remote_version))
    zip_path = base / f"TouchpadExperimentManager-{safe_ver}.zip"
    extract_dir = base / f"TouchpadExperimentManager-{safe_ver}"
    return zip_path, extract_dir
