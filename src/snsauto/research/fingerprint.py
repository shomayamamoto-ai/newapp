"""Finding the same sound used across several competing posts.

"Which sound is trending" is not answerable from official APIs - TikTok has no
public search, Instagram's hashtag endpoints carry no audio attribution, and
YouTube exposes no music metadata for a third party's video. There is no
honest way to produce a platform ranking.

The question underneath it is answerable, though, and is the more useful half:
**of the top posts we collected, which ones are using the same audio?** A track
that carries seven of a keyword's top twenty is doing something, and that
conclusion comes entirely from files we already fetched for telop analysis.

How it works: each video's audio is reduced to a coarse spectral envelope - the
energy in each of a few frequency bands, once every quarter second. Two clips
of the same track have the same envelope shape even at different volumes, and
matching allows a time offset because two creators rarely start a song at the
same point.

The limits, stated plainly:

* This groups audio that *sounds alike*. It does not identify the track, and
  it cannot give you a title to search for.
* Heavy narration over a quiet bed can mask the bed and split a group.
* Thresholds were calibrated on synthetic audio, not on a library of real
  music, so the numbers below are defaults to tune rather than constants
  anyone has proven on production data.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
from dataclasses import dataclass, field

from ..media.ffmpeg import FFmpegError, ffmpeg_path, probe

log = logging.getLogger(__name__)

# Log-spaced centres under the 4 kHz Nyquist of an 8 kHz mono downmix. Eight
# bands is enough to tell tracks apart and few enough to stay cheap.
BANDS = (100, 200, 400, 700, 1200, 2000, 3000, 3600)

SAMPLE_RATE = 8000
WINDOW_SAMPLES = 2048          # 0.256 s per measurement
WINDOW_SEC = WINDOW_SAMPLES / SAMPLE_RATE

# Only the opening matters: short-form audio rarely changes track mid-video,
# and reading the whole thing multiplies cost for nothing.
MAX_SECONDS = 30.0

# Two clips of one track start at different points, so matching searches over
# offsets. +-8 seconds covers normal usage without letting an unrelated pair
# find a lucky alignment.
MAX_OFFSET_WINDOWS = int(8.0 / WINDOW_SEC)

# Below this many overlapping windows a correlation is not meaningful.
MIN_OVERLAP_WINDOWS = 20

# Measured on generated melodies (tests/test_fingerprint.py rebuilds them):
#
#   same track, offset by 6s        0.997
#   same track, 9 dB quieter        1.000
#   same track under loud speech    0.873   <- the hardest same-track case
#   different tracks                0.417 - 0.680
#
# 0.80 sits in the 0.193-wide gap between those two populations. It is a
# default tuned on synthetic audio, not a constant validated against a real
# music library, and it is exposed as a parameter for that reason.
SAME_SOUND_THRESHOLD = 0.80

_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-?inf|nan)")

# Silence floor. astats reports -inf for a silent window; a finite floor keeps
# the arithmetic well defined.
FLOOR_DB = -90.0


@dataclass(slots=True)
class Fingerprint:
    """A video's audio reduced to a band-energy envelope."""

    key: str
    windows: list[list[float]] = field(default_factory=list)
    duration_sec: float = 0.0

    def __len__(self) -> int:
        return len(self.windows)

    @property
    def usable(self) -> bool:
        return len(self.windows) >= MIN_OVERLAP_WINDOWS


def _band_series(path, centre: int, seconds: float) -> list[float]:
    """Per-window RMS (dB) inside one frequency band."""
    chain = (
        f"aresample={SAMPLE_RATE},aformat=channel_layouts=mono,"
        f"asetnsamples=n={WINDOW_SAMPLES}:p=0,"
        f"bandpass=f={centre}:width_type=o:w=1,"
        "astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    proc = subprocess.run(
        [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
         "-t", f"{seconds:.2f}", "-i", str(path), "-af", chain, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    values = []
    for raw in _RMS.findall(proc.stdout):
        try:
            value = float(raw)
        except ValueError:
            value = FLOOR_DB          # -inf / nan: a silent window
        values.append(max(FLOOR_DB, value))
    return values


def fingerprint(path, key: str | None = None) -> Fingerprint:
    """Reduce one video's audio to its band-energy envelope."""
    info = probe(path)
    if not info.get("has_audio"):
        raise FFmpegError(f"no audio track: {path}")
    seconds = min(MAX_SECONDS, float(info.get("duration") or MAX_SECONDS))

    series = [_band_series(path, centre, seconds) for centre in BANDS]
    length = min((len(s) for s in series), default=0)
    if not length:
        raise FFmpegError(f"no audio windows extracted: {path}")

    windows = []
    for i in range(length):
        frame = [series[b][i] for b in range(len(BANDS))]
        # Subtract the frame's own mean so the shape survives a volume
        # difference: the same track mixed 6 dB quieter must still match.
        centre = sum(frame) / len(frame)
        windows.append([v - centre for v in frame])

    return Fingerprint(key=key or str(path), windows=windows,
                       duration_sec=length * WINDOW_SEC)


def _correlation(a: list[list[float]], b: list[list[float]]) -> float:
    """Mean cosine similarity between two aligned window sequences."""
    total, counted = 0.0, 0
    for frame_a, frame_b in zip(a, b):
        dot = sum(x * y for x, y in zip(frame_a, frame_b))
        norm = math.sqrt(sum(x * x for x in frame_a)) * math.sqrt(
            sum(y * y for y in frame_b)
        )
        if norm > 1e-9:
            total += dot / norm
            counted += 1
    return total / counted if counted else 0.0


def similarity(a: Fingerprint, b: Fingerprint) -> dict:
    """How alike two audio tracks are, searching over start offsets."""
    if not a.usable or not b.usable:
        return {"score": 0.0, "offset_sec": 0.0, "comparable": False,
                "reason": "音声が短すぎて比較できません"}

    # None, not -1: cosine similarity is legitimately negative for two
    # unrelated tracks, and treating that as "could not compare" would report
    # a confident mismatch as a failure to measure.
    best_score, best_offset = None, 0
    for offset in range(-MAX_OFFSET_WINDOWS, MAX_OFFSET_WINDOWS + 1):
        if offset >= 0:
            left, right = a.windows[offset:], b.windows
        else:
            left, right = a.windows, b.windows[-offset:]
        overlap = min(len(left), len(right))
        if overlap < MIN_OVERLAP_WINDOWS:
            continue
        score = _correlation(left[:overlap], right[:overlap])
        if best_score is None or score > best_score:
            best_score, best_offset = score, offset

    if best_score is None:
        return {"score": 0.0, "offset_sec": 0.0, "comparable": False,
                "reason": "重なる区間が足りません"}
    return {
        "score": round(best_score, 4),
        "offset_sec": round(best_offset * WINDOW_SEC, 2),
        "comparable": True,
        "same_sound": best_score >= SAME_SOUND_THRESHOLD,
    }


def cluster(
    fingerprints: list[Fingerprint], threshold: float = SAME_SOUND_THRESHOLD
) -> list[list[str]]:
    """Group videos that share audio. Single-link, which suits the data:
    two clips of one track can overlap different sections of it."""
    keys = [f.key for f in fingerprints]
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, first in enumerate(fingerprints):
        for second in fingerprints[i + 1:]:
            result = similarity(first, second)
            if result["comparable"] and result["score"] >= threshold:
                a, b = find(first.key), find(second.key)
                if a != b:
                    parent[a] = b

    groups: dict[str, list[str]] = {}
    for key in keys:
        groups.setdefault(find(key), []).append(key)
    return sorted(groups.values(), key=len, reverse=True)


def recurring_sounds(videos: dict, threshold: float = SAME_SOUND_THRESHOLD) -> dict:
    """Which audio recurs across a set of fetched competitor videos.

    ``videos`` maps a label (an external id, usually) to a local file path.
    """
    prints, failed = [], {}
    for key, path in videos.items():
        try:
            prints.append(fingerprint(path, key=key))
        except (FFmpegError, OSError) as exc:
            failed[key] = str(exc)

    if len(prints) < 2:
        return {
            "usable": False,
            "reason": "比較できる動画が2本未満です。音源の共通性は判定できません。",
            "analysed": len(prints), "failed": failed,
        }

    groups = [g for g in cluster(prints, threshold) if len(g) > 1]
    return {
        "usable": True,
        "analysed": len(prints),
        "failed": failed,
        "shared_groups": groups,
        "posts_sharing_audio": sum(len(g) for g in groups),
        "threshold": threshold,
        # Said at the point of use, not only in the docs: this is similarity,
        # not identification, and it cannot name the track.
        "note": (
            "同じ音源を使っている投稿のグループです。音源名は特定できません"
            "（公式APIが音源情報を返さないため）。ナレーションが大きい動画では"
            "BGMが埋もれ、同じ音源でも別グループになることがあります。"
        ),
    }
