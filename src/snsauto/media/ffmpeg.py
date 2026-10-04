"""Thin, dependency-light wrapper around the ffmpeg/ffprobe binaries.

Resolution order: an explicit ``FFMPEG_BINARY`` env var, then whatever is on
PATH, then the binary bundled with ``imageio-ffmpeg``. The last fallback means
the toolchain works on a machine with no system ffmpeg installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


@lru_cache
def ffmpeg_path() -> str:
    explicit = os.environ.get("FFMPEG_BINARY")
    if explicit and Path(explicit).exists():
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise FFmpegError(
            "ffmpeg not found. Install it, set FFMPEG_BINARY, or "
            "`pip install imageio-ffmpeg`."
        ) from exc


@lru_cache
def ffprobe_path() -> str | None:
    explicit = os.environ.get("FFPROBE_BINARY")
    if explicit and Path(explicit).exists():
        return explicit
    return shutil.which("ffprobe")


def run_ffmpeg(args: list[str], *, quiet: bool = True) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_path(), "-hide_banner", "-nostdin"]
    if quiet:
        cmd += ["-loglevel", "error"]
    cmd += args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(
            f"ffmpeg failed ({proc.returncode}):\n"
            f"  cmd: {' '.join(cmd[:14])}...\n  {proc.stderr[-1500:]}"
        )
    return proc


def probe(path: str | Path) -> dict:
    """Return {duration, width, height, fps, has_audio}. Works without ffprobe."""
    path = Path(path)
    if not path.exists():
        raise FFmpegError(f"file not found: {path}")

    probe_bin = ffprobe_path()
    if probe_bin:
        proc = subprocess.run(
            [probe_bin, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return _from_ffprobe(json.loads(proc.stdout))

    return _from_ffmpeg_stderr(path)


def _from_ffprobe(data: dict) -> dict:
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": _ratio(video.get("avg_frame_rate")),
        "has_audio": audio is not None,
    }


def _ratio(value: str | None) -> float:
    if not value or "/" not in str(value):
        return float(value or 0)
    num, den = str(value).split("/", 1)
    try:
        return float(num) / float(den) if float(den) else 0.0
    except ValueError:
        return 0.0


_DUR = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)")
_RES = re.compile(r"Stream .*Video:.*?(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def _from_ffmpeg_stderr(path: Path) -> dict:
    """ffprobe is a separate binary that imageio-ffmpeg does not ship, so parse
    ffmpeg's own stderr banner instead of requiring it."""
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True,
    )
    err = proc.stderr
    duration = 0.0
    if m := _DUR.search(err):
        h, mi, s = m.groups()
        duration = int(h) * 3600 + int(mi) * 60 + float(s)
    width = height = 0
    if m := _RES.search(err):
        width, height = int(m.group(1)), int(m.group(2))
    fps = float(m.group(1)) if (m := _FPS.search(err)) else 0.0
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": "Audio:" in err,
    }


_SCENE_PTS = re.compile(r"pts_time:(\d+\.?\d*)")


def detect_scenes(path: str | Path, threshold: float = 0.30) -> list[float]:
    """Timestamps (seconds) where a hard cut occurs.

    Used to measure a competitor's editing rhythm: cut count and average shot
    length are the two numbers that most reliably separate a scroll-stopping
    short from a flat one.
    """
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", "-i", str(path),
         "-filter:v", f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return sorted({float(t) for t in _SCENE_PTS.findall(proc.stderr)})
