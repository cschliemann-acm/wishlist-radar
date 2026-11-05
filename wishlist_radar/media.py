"""ffmpeg helpers: still frames (and audio) from a video. Shared with reel-tunes."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *args],
        check=True,
        capture_output=True,
    )


def probe_duration(video: Path) -> float | None:
    """Length in seconds, read from the file itself (Instagram doesn't always report it)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return None


def has_audio(video: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    return bool(out.stdout.strip())


def extract_audio(video: Path, out: Path, start: float = 0, length: float | None = None) -> Path:
    """Mono 16 kHz mp3 (fine for both fingerprinting and Whisper)."""
    args = ["-ss", f"{start:.2f}", "-i", str(video)]
    if length:
        args += ["-t", f"{length:.2f}"]
    args += ["-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(out)]
    _ffmpeg(*args)
    return out


def frame_at(video: Path, t: float, out: Path) -> Path | None:
    try:
        _ffmpeg("-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                "-vf", "scale=768:-2", "-q:v", "4", str(out))
    except subprocess.CalledProcessError:
        return None
    return out if out.exists() else None


def extract_frames(video: Path, outdir: Path, duration: float | None, count: int) -> list[Path]:
    """Grab `count` evenly spaced frames, scaled to 768px wide JPEGs."""
    if count <= 0:
        return []
    duration = duration or 10.0
    times = [duration * (i + 0.5) / count for i in range(count)]
    return [f for i, t in enumerate(times) if (f := frame_at(video, t, outdir / f"frame_{i:02d}.jpg"))]


def scene_change_times(video: Path, threshold: float = 0.25) -> list[float]:
    """Timestamps where the picture changes a lot (cuts, a new CD held up, etc.)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video), "-an",
         "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return [float(t) for t in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def pick_frame_times(duration: float, scene_times: list[float], interval: float,
                     max_frames: int, min_gap: float = 1.0) -> list[float]:
    """Merge scene-change moments with a regular grid, drop near-duplicates, cap the total.

    Scene-change frames are taken a moment after the cut so the new shot has settled.
    If over the cap, keep an evenly spread subset (scene changes take priority).
    """
    scenes = sorted({min(duration - 0.1, t + 0.5) for t in scene_times if 0 <= t < duration})
    grid = [t for t in (interval * (i + 0.5) for i in range(int(duration / interval) + 1))
            if t < duration]
    times: list[float] = []
    for t in scenes + grid:  # scenes first, so they win when two are close together
        if all(abs(t - u) >= min_gap for u in times):
            times.append(t)
    times.sort()
    if len(times) > max_frames:
        scene_set = set(scenes)
        keep = [t for t in times if t in scene_set]
        if len(keep) > max_frames:
            step = len(keep) / max_frames
            keep = [keep[int(i * step)] for i in range(max_frames)]
        else:
            rest = [t for t in times if t not in scene_set]
            room = max_frames - len(keep)
            if room and rest:
                step = len(rest) / room
                keep += [rest[int(i * step)] for i in range(room)]
        times = sorted(keep)
    return times


def extract_smart_frames(video: Path, outdir: Path, duration: float | None,
                         interval: float, max_frames: int) -> list[Path]:
    duration = duration or 10.0
    times = pick_frame_times(duration, scene_change_times(video), interval, max_frames)
    return [f for i, t in enumerate(times) if (f := frame_at(video, t, outdir / f"frame_{i:02d}.jpg"))]
