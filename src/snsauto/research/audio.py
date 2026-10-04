"""What the competitor's audio track is doing.

The ask was "音源・BGMトレンド". Half of that is not obtainable and saying so
is part of the deliverable:

* **Which sound is trending** - not available. TikTok has no public keyword
  search at all; Instagram's hashtag endpoints return no audio attribution;
  YouTube's API exposes no music metadata for a third party's video. There is
  no honest way to produce a trending-sounds chart from official APIs.
* **What the audio of a specific competitor post does** - fully obtainable
  from the file itself, which is what this module measures.

Everything here comes out of ffmpeg filters, so there is no model to ship and
no extra dependency. The speech/music split is an *estimate* from band energy,
labelled as one everywhere it surfaces.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..media.ffmpeg import FFmpegError, ffmpeg_path, probe

# Telephony band. Speech energy concentrates here; a music bed spreads far
# wider, so the ratio separates a narrated video from a music-only one.
SPEECH_BAND = (300, 3400)

SILENCE_THRESHOLD_DB = -35.0
SILENCE_MIN_SEC = 0.35

_SILENCE_DUR = re.compile(r"silence_duration:\s*([0-9.]+)")
_RMS = re.compile(r"RMS level dB:\s*(-?[0-9.]+|-inf)")
_LUFS = re.compile(r"I:\s*(-?[0-9.]+)\s*LUFS")


def _run(args: list[str]) -> str:
    """ffmpeg writes filter measurements to stderr, including on success."""
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", *args],
        capture_output=True, text=True,
    )
    return proc.stderr


def silence_ratio(path: str | Path, duration: float) -> tuple[float, int]:
    """Fraction of the video with no audible audio, and how many gaps."""
    if duration <= 0:
        return 0.0, 0
    stderr = _run([
        "-i", str(path), "-af",
        f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={SILENCE_MIN_SEC}",
        "-f", "null", "-",
    ])
    gaps = [float(d) for d in _SILENCE_DUR.findall(stderr)]
    return min(1.0, sum(gaps) / duration), len(gaps)


def _rms_db(path: str | Path, band: tuple[int, int] | None = None) -> float | None:
    chain = "astats=measure_overall=RMS_level:measure_perchannel=none"
    if band:
        lo, hi = band
        chain = f"highpass=f={lo},lowpass=f={hi},{chain}"
    stderr = _run(["-i", str(path), "-af", chain, "-f", "null", "-"])
    match = _RMS.search(stderr)
    if not match or match.group(1) == "-inf":
        return None
    return float(match.group(1))


def loudness_lufs(path: str | Path) -> float | None:
    """Integrated loudness. Platforms normalise to roughly -14 LUFS."""
    stderr = _run(["-i", str(path), "-af", "ebur128=framelog=verbose",
                   "-f", "null", "-"])
    matches = _LUFS.findall(stderr)
    return float(matches[-1]) if matches else None


def analyze_audio(video_path: str | Path) -> dict:
    """Measure the audio bed of one competitor video."""
    info = probe(video_path)
    duration = float(info.get("duration") or 0.0)
    if not info.get("has_audio"):
        return {"has_audio": False, "audio_style": "silent",
                "duration_sec": round(duration, 2)}
    if duration <= 0:
        raise FFmpegError(f"could not determine duration: {video_path}")

    quiet_ratio, gap_count = silence_ratio(video_path, duration)
    overall = _rms_db(video_path)
    speech = _rms_db(video_path, SPEECH_BAND)

    # Energy inside the speech band relative to the whole track. Converted out
    # of dB first - averaging decibels directly is meaningless.
    band_ratio = None
    if overall is not None and speech is not None:
        band_ratio = min(1.0, 10 ** ((speech - overall) / 20))

    return {
        "has_audio": True,
        "duration_sec": round(duration, 2),
        "loudness_lufs": loudness_lufs(video_path),
        "silence_ratio": round(quiet_ratio, 3),
        "silence_gaps": gap_count,
        "rms_db": round(overall, 2) if overall is not None else None,
        "speech_band_ratio": round(band_ratio, 3) if band_ratio is not None else None,
        "audio_style": classify_audio(band_ratio, quiet_ratio),
        # Stated inline so no reader mistakes the estimate for a detection.
        "method": "ffmpeg band-energy estimate; not a speech/music classifier",
    }


def classify_audio(band_ratio: float | None, quiet_ratio: float) -> str:
    """A label for the audio bed. An estimate, and named as one.

    Thresholds are deliberately wide: this decides which of four sentences a
    report prints, not anything a number is computed from.
    """
    if quiet_ratio > 0.85:
        return "silent"
    if band_ratio is None:
        return "unknown"
    if band_ratio >= 0.72:
        return "narration-led"
    if band_ratio <= 0.45:
        return "music-led"
    return "mixed"


AUDIO_STYLE_JA = {
    "narration-led": "ナレーション主体（音声帯域にエネルギーが集中）",
    "music-led": "BGM主体（音声帯域外の成分が多い）",
    "mixed": "ナレーション＋BGM",
    "silent": "実質無音",
    "unknown": "判定不可",
}
