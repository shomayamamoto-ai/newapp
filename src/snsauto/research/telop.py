"""Telop analysis: read the text the video actually puts on screen.

Until now ``telop_profile()`` measured the *caption* - the words under the
post, not the words burned into the frames. The numbers it produced were real
but they described the wrong thing. This module reads the frames.

Pipeline: sample frames -> drop near-duplicates -> OCR each survivor ->
collapse consecutive identical reads into telop *events* with a start, an end
and a screen position. Everything downstream (hold time, coverage, position
mix) is derived from those events, so the metrics move together and cannot
contradict each other.

Two reader tiers, matching the rest of the codebase:

* ``TesseractReader`` - offline, free, needs the ``tesseract`` binary with the
  ``jpn`` language data. Gives text and bounding boxes.
* ``VisionReader``    - needs ANTHROPIC_API_KEY. Gives what OCR cannot: colour,
  weight, decoration, and whether a line is the headline or a subtitle.

Neither is required. With no reader at all the caller still gets frame-level
timing, and says so in ``reader``.
"""

from __future__ import annotations

import logging
import re
import shutil
import statistics
import subprocess
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from ..media.ffmpeg import FFmpegError, detect_scenes, ffmpeg_path, probe

log = logging.getLogger(__name__)

# Sampling resolution. 0.8s is a deliberate compromise: short-form telop cards
# rarely hold for less than a second, and a finer grid multiplies OCR cost
# without finding new cards.
DEFAULT_INTERVAL = 0.8
DEFAULT_MAX_FRAMES = 45

# Tesseract word confidence floor. Below ~55 the reader mostly returns
# fragments of background texture.
MIN_CONFIDENCE = 55.0
# Text shorter than this fraction of the frame height is a watermark, a
# username or a platform UI label - not telop.
MIN_TEXT_HEIGHT_RATIO = 0.018
# One character on its own is transition noise, never a card.
MIN_EVENT_CHARS = 2


@dataclass(slots=True)
class TelopRead:
    """What a reader saw in one frame."""

    text: str
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # normalised
    confidence: float = 0.0
    style: dict = field(default_factory=dict)


@dataclass(slots=True)
class TelopEvent:
    """One telop card, held across consecutive frames."""

    start: float
    end: float
    text: str
    position: str  # top | middle | bottom
    area_ratio: float
    confidence: float
    style: dict = field(default_factory=dict)

    @property
    def hold_sec(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "hold_sec": round(self.hold_sec, 2),
            "text": self.text,
            "chars": len(self.text.replace(" ", "")),
            "position": self.position,
            "area_ratio": round(self.area_ratio, 4),
            "confidence": round(self.confidence, 1),
            **({"style": self.style} if self.style else {}),
        }


# ---------------------------------------------------------------- sampling


def sample_times(
    duration: float,
    cuts: list[float] | None = None,
    interval: float = DEFAULT_INTERVAL,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> list[float]:
    """Timestamps to grab. A regular grid, plus a frame just after every cut.

    The post-cut frames matter because a telop card usually appears with the
    cut; a pure grid can land either side of it and miss short cards.
    """
    if duration <= 0:
        return []
    times = {round(t, 2) for t in _frange(interval / 2, duration, interval)}
    for cut in cuts or []:
        if 0 < cut < duration:
            times.add(round(min(cut + 0.15, duration - 0.05), 2))

    ordered = sorted(t for t in times if 0 <= t < duration)
    if len(ordered) <= max_frames:
        return ordered
    # Thin evenly rather than truncating, so late telop is not lost.
    step = len(ordered) / max_frames
    return [ordered[int(i * step)] for i in range(max_frames)]


def _frange(start: float, stop: float, step: float):
    t = start
    while t < stop:
        yield t
        t += step


def extract_frames(
    video_path: str | Path, times: list[float], out_dir: Path, width: int = 720
) -> list[tuple[float, Path]]:
    """Grab one frame per timestamp, scaled down - OCR does not need 1080p.

    PNG, not JPEG, and the difference is not cosmetic. Measured on four known
    telop cards, mean character accuracy was:

        PNG          1.00
        JPEG -q:v 2  0.98
        JPEG -q:v 3  0.91

    JPEG ringing around a glyph's outline both erases characters (10kg read as
    10ko, then dropped for low confidence) and invents them (せた read as
    せだた). Telop is high-contrast type with a hard outline - exactly the
    content a DCT codec handles worst. The frames live in a temp dir for a few
    seconds, so the extra bytes cost nothing that matters.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[float, Path]] = []
    for i, t in enumerate(times):
        target = out_dir / f"f{i:03d}.png"
        proc = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-nostdin", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(video_path), "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-y", str(target)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and target.exists() and target.stat().st_size > 0:
            frames.append((t, target))
    return frames


def average_hash(path: Path, size: int = 12) -> int | None:
    """64-bit-ish perceptual hash, PIL only. Used to skip unchanged frames."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dep in practice
        return None
    with Image.open(path) as img:
        small = img.convert("L").resize((size, size), Image.BILINEAR)
        pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    bits = 0
    for i, px in enumerate(pixels):
        if px > mean:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ---------------------------------------------------------------- readers


class TesseractReader:
    """OCR via the tesseract binary. Text and boxes; no styling.

    Page segmentation mode is the single setting that decides whether this
    works on Japanese telop. Measured against four known cards burned into a
    1080x1920 test render:

        psm 6 (uniform block) : 4/4 cards read exactly
        psm 11 (sparse text)  : 2/4 correct, one read as 2 of its 8 characters

    so psm 6 leads and psm 11 is only a fallback for scattered layouts that
    psm 6 cannot block together.

    **Never pick between the two on confidence.** In that same run the badly
    truncated psm 11 read of the card reading 朝食を抜くのはNG returned just
    のは at confidence 97, against the correct psm 6 read at confidence 96.
    Tesseract is confident about the characters it *did* read and reports
    nothing about the ones it dropped, and dropping characters is exactly this
    failure mode, so length is the honest tie-break and confidence is not.

    Binarising the frame first also measurably *hurts* (1.00 -> 0.92): telop
    carries an outline and a drop shadow, and thresholding flattens the very
    edge that separates the glyph from the footage behind it.
    """

    name = "tesseract"

    def __init__(self, langs: str = "jpn+eng", psm: int = 6,
                 fallback_psm: int | None = 11,
                 min_confidence: float = MIN_CONFIDENCE):
        self.langs = langs
        self.psm = psm
        self.fallback_psm = fallback_psm
        self.min_confidence = min_confidence

    @staticmethod
    def available() -> bool:
        return shutil.which("tesseract") is not None

    def read(self, path: Path) -> TelopRead | None:
        primary = self._read_with(path, self.psm)
        if self.fallback_psm is None:
            return primary
        if primary is not None and _char_count(primary.text) >= 3:
            return primary
        fallback = self._read_with(path, self.fallback_psm)
        if fallback is None:
            return primary
        if primary is None:
            return fallback
        return max((primary, fallback), key=lambda r: _char_count(r.text))

    def _read_with(self, path: Path, psm: int) -> TelopRead | None:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", self.langs,
             "--psm", str(psm), "tsv"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            log.debug("tesseract failed on %s: %s", path.name, proc.stderr[-200:])
            return None
        return self._parse_tsv(proc.stdout, path)

    def _parse_tsv(self, tsv: str, path: Path) -> TelopRead | None:
        try:
            from PIL import Image

            with Image.open(path) as img:
                fw, fh = img.size
        except Exception:  # pragma: no cover
            return None
        if not fw or not fh:
            return None

        lines: dict[tuple, list[dict]] = {}
        for row in tsv.splitlines()[1:]:
            cols = row.split("\t")
            if len(cols) < 12:
                continue
            try:
                conf = float(cols[10])
                left, top, w, h = (int(cols[6]), int(cols[7]),
                                   int(cols[8]), int(cols[9]))
            except ValueError:
                continue
            text = cols[11].strip()
            if not text or conf < self.min_confidence:
                continue
            if h / fh < MIN_TEXT_HEIGHT_RATIO:
                continue  # watermark / handle / UI chrome
            if not _meaningful(text):
                continue
            lines.setdefault((cols[2], cols[3], cols[4]), []).append(
                {"text": text, "left": left, "top": top, "w": w, "h": h, "conf": conf}
            )

        if not lines:
            return None

        parts, boxes, confs = [], [], []
        for key in sorted(lines, key=lambda k: min(w["top"] for w in lines[k])):
            words = sorted(lines[key], key=lambda w: w["left"])
            parts.append("".join(w["text"] for w in words)
                         if _is_cjk("".join(w["text"] for w in words))
                         else " ".join(w["text"] for w in words))
            boxes.extend(words)
            confs.extend(w["conf"] for w in words)

        x0 = min(b["left"] for b in boxes) / fw
        y0 = min(b["top"] for b in boxes) / fh
        x1 = max(b["left"] + b["w"] for b in boxes) / fw
        y1 = max(b["top"] + b["h"] for b in boxes) / fh

        text = "\n".join(parts)
        # A lone character is a frame-transition artefact, not a telop card;
        # letting it through creates a one-frame event between two real cards.
        if _char_count(text) < MIN_EVENT_CHARS:
            return None

        return TelopRead(
            text=text,
            bbox=(x0, y0, x1, y1),
            confidence=statistics.fmean(confs),
        )


def _char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


_CJK = re.compile(r"[぀-ヿ一-鿿]")
_JUNK = re.compile(r"^[^\w぀-ヿ一-鿿]+$")


def _is_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _meaningful(text: str) -> bool:
    """Reject punctuation soup and stray single latin characters."""
    if _JUNK.match(text):
        return False
    if len(text) == 1 and not _is_cjk(text) and not text.isdigit():
        return False
    return True


class VisionReader:
    """Claude reads the frame. Gives styling that OCR structurally cannot.

    Used on the deduplicated frames only - one call per distinct telop card,
    not per sampled frame.
    """

    name = "vision"

    def __init__(self, llm):
        self.llm = llm

    def read(self, path: Path) -> TelopRead | None:
        try:
            result = self.llm.read_telop_frame(path)
        except Exception as exc:
            log.warning("vision telop read failed for %s: %s", path.name, exc)
            return None
        if not result or not (result.get("text") or "").strip():
            return None
        box = result.get("bbox") or {}
        return TelopRead(
            text=result["text"].strip(),
            bbox=(
                float(box.get("x0", 0.05)), float(box.get("y0", 0.4)),
                float(box.get("x1", 0.95)), float(box.get("y1", 0.6)),
            ),
            confidence=float(result.get("confidence", 90.0)),
            style={k: v for k, v in result.items()
                   if k in ("color", "weight", "decoration", "role", "emphasis")
                   and v},
        )


# ---------------------------------------------------------------- analysis


def _position(bbox: tuple[float, float, float, float]) -> str:
    centre_y = (bbox[1] + bbox[3]) / 2
    if centre_y < 0.34:
        return "top"
    if centre_y < 0.67:
        return "middle"
    return "bottom"


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


# How alike two reads must be to count as the same telop card. A held card is
# re-read every sampled frame and OCR rarely returns the same string twice -
# it drops a trailing line, or picks up one more character as a fade finishes -
# so the comparison has to tolerate partial reads. 0.6 separates cleanly here:
# partial reads of one card score 0.75-0.95, while two different cards in the
# same video score under 0.2.
SAME_CARD_RATIO = 0.6


def _similar(a: str, b: str) -> bool:
    """Are two OCR reads the same card? OCR jitters by a character or two."""
    a_s, b_s = re.sub(r"\s+", "", a), re.sub(r"\s+", "", b)
    if not a_s or not b_s:
        return a_s == b_s
    if a_s == b_s:
        return True
    shorter, longer = sorted((a_s, b_s), key=len)
    # A clean prefix/substring read of a longer card: the reader saw part of
    # the same text, not a different card.
    if shorter in longer and len(shorter) / len(longer) >= 0.5:
        return True
    return SequenceMatcher(None, a_s, b_s).ratio() >= SAME_CARD_RATIO


def build_events(
    reads: list[tuple[float, TelopRead | None]], duration: float, interval: float
) -> list[TelopEvent]:
    """Collapse consecutive frames showing the same card into one event."""
    events: list[TelopEvent] = []
    for time, read in reads:
        if read is None:
            continue
        last = events[-1] if events else None
        # Only merge into the previous event if it is still the current one.
        if last is not None and _similar(last.text, read.text) and (
            time - last.end <= interval * 2.2
        ):
            last.end = time + interval
            last.confidence = (last.confidence + read.confidence) / 2
            if len(read.text) > len(last.text):
                last.text = read.text  # keep the fullest read of the card
            continue
        events.append(
            TelopEvent(
                start=time,
                end=min(time + interval, duration) if duration else time + interval,
                text=read.text,
                position=_position(read.bbox),
                area_ratio=_area(read.bbox),
                confidence=read.confidence,
                style=read.style,
            )
        )
    # One frame the reader failed on should not split a held card into two
    # events - that would halve avg_hold_sec and double events_per_min.
    stitched: list[TelopEvent] = []
    for event in events:
        previous = stitched[-1] if stitched else None
        if (
            previous is not None
            and _similar(previous.text, event.text)
            and event.start - previous.end <= interval * 1.5
        ):
            previous.end = event.end
            previous.confidence = max(previous.confidence, event.confidence)
            if len(event.text) > len(previous.text):
                previous.text = event.text
            continue
        stitched.append(event)

    for event in stitched:
        if duration:
            event.end = min(event.end, duration)
    return stitched


def analyze_telop(
    video_path: str | Path,
    reader=None,
    interval: float = DEFAULT_INTERVAL,
    max_frames: int = DEFAULT_MAX_FRAMES,
    vision_reader=None,
    vision_budget: int = 6,
) -> dict:
    """Read the burned-in text of a video and summarise how it is used.

    ``reader`` does the bulk pass. ``vision_reader``, when given, re-reads up
    to ``vision_budget`` distinct cards to add styling the OCR pass cannot see.
    """
    video_path = Path(video_path)
    info = probe(video_path)
    duration = float(info.get("duration") or 0.0)
    if duration <= 0:
        raise FFmpegError(f"could not determine duration: {video_path}")

    try:
        cuts = detect_scenes(video_path)
    except FFmpegError:
        cuts = []

    times = sample_times(duration, cuts, interval, max_frames)
    if reader is None and TesseractReader.available():
        reader = TesseractReader()

    with tempfile.TemporaryDirectory(prefix="snsauto-telop-") as tmp:
        frames = extract_frames(video_path, times, Path(tmp))
        if not frames:
            raise FFmpegError(f"no frames extracted from {video_path}")

        # OCR is by far the expensive step, and a telop card holds still for
        # many frames. Hash each frame against the one before it and, when the
        # picture has not changed, reuse the previous read instead of running
        # the reader again. The frame still contributes its timestamp, so hold
        # durations stay correct.
        reads: list[tuple[float, TelopRead | None]] = []
        ocr_calls = 0
        previous_hash: int | None = None
        previous_read: TelopRead | None = None
        for time, path in frames:
            digest = average_hash(path)
            unchanged = (
                digest is not None
                and previous_hash is not None
                and _hamming(digest, previous_hash) <= 2
            )
            previous_hash = digest
            if unchanged:
                reads.append((time, previous_read))
                continue
            if reader is None:
                reads.append((time, None))
                continue
            previous_read = reader.read(path)
            ocr_calls += 1
            reads.append((time, previous_read))

        events = build_events(reads, duration, interval)

        if vision_reader is not None and events:
            _enrich_with_vision(events, frames, vision_reader, vision_budget)

    return summarize_events(
        events,
        duration=duration,
        reader=getattr(reader, "name", None),
        vision=getattr(vision_reader, "name", None) if vision_reader else None,
        frames_sampled=len(frames),
        ocr_calls=ocr_calls,
        interval=interval,
        cut_count=len(cuts),
    )


def _enrich_with_vision(events, frames, vision_reader, budget: int) -> None:
    """Re-read the longest-held cards with the vision tier, for styling."""
    ranked = sorted(events, key=lambda e: e.hold_sec, reverse=True)[:budget]
    for event in ranked:
        midpoint = (event.start + event.end) / 2
        frame = min(frames, key=lambda f: abs(f[0] - midpoint), default=None)
        if frame is None:
            continue
        read = vision_reader.read(frame[1])
        if read is None:
            continue
        event.style = {**event.style, **read.style}
        # Vision reads decorative Japanese type far better than OCR does; take
        # its text when OCR's confidence was shaky.
        if event.confidence < 75 and read.text:
            event.text = read.text
            event.confidence = read.confidence


def summarize_events(
    events: list[TelopEvent],
    *,
    duration: float,
    reader: str | None = None,
    vision: str | None = None,
    frames_sampled: int = 0,
    ocr_calls: int = 0,
    interval: float = DEFAULT_INTERVAL,
    cut_count: int = 0,
) -> dict:
    """Turn telop events into the numbers a script writer can act on."""
    base = {
        "source": "frames",
        "reader": reader or "none",
        "vision": vision,
        "duration_sec": round(duration, 2),
        "frames_sampled": frames_sampled,
        "reader_calls": ocr_calls,
        "resolution_sec": interval,
        "cut_count": cut_count,
        "event_count": len(events),
        "events": [e.as_dict() for e in events],
    }
    if not events:
        base.update({
            "coverage_ratio": 0.0, "first_telop_sec": None,
            "avg_chars": 0.0, "chars_per_sec": 0.0,
        })
        return base

    char_counts = [len(e.text.replace(" ", "").replace("\n", "")) for e in events]
    holds = [e.hold_sec for e in events if e.hold_sec > 0]
    covered = sum(e.hold_sec for e in events)
    positions = {"top": 0.0, "middle": 0.0, "bottom": 0.0}
    for e in events:
        positions[e.position] += e.hold_sec

    base.update({
        # Fraction of the video with any telop on screen. Short-form winners
        # sit high here; a low number with a high view count usually means the
        # post carried on voice or face instead.
        "coverage_ratio": round(min(1.0, covered / duration), 3) if duration else 0.0,
        "first_telop_sec": round(min(e.start for e in events), 2),
        "events_per_min": round(len(events) / duration * 60, 2) if duration else 0.0,
        "avg_chars": round(statistics.fmean(char_counts), 1),
        "median_chars": round(statistics.median(char_counts), 1),
        "max_chars": max(char_counts),
        "avg_hold_sec": round(statistics.fmean(holds), 2) if holds else None,
        "min_hold_sec": round(min(holds), 2) if holds else None,
        # Measured, not inferred from the caption.
        "chars_per_sec": round(sum(char_counts) / duration, 2) if duration else 0.0,
        "position_mix": {
            k: round(v / covered, 3) if covered else 0.0 for k, v in positions.items()
        },
        "dominant_position": max(positions, key=positions.get),
        "area_ratio_mean": round(statistics.fmean([e.area_ratio for e in events]), 4),
        "mean_confidence": round(statistics.fmean([e.confidence for e in events]), 1),
        "styles": _style_summary(events),
    })
    return base


def _style_summary(events: list[TelopEvent]) -> dict:
    from collections import Counter

    counts: dict[str, Counter] = {}
    for event in events:
        for key, value in (event.style or {}).items():
            counts.setdefault(key, Counter())[str(value)] += 1
    return {k: v.most_common(4) for k, v in counts.items()}


def confidence_band(summary: dict) -> str:
    """How much to trust these numbers. Reported alongside them, always."""
    if summary.get("reader") in (None, "none"):
        return "unavailable"
    mean = summary.get("mean_confidence") or 0
    events = summary.get("event_count") or 0
    if events == 0:
        return "no-telop-detected"
    if mean >= 85:
        return "high"
    if mean >= 70:
        return "medium"
    return "low"


def merge_into_profile(caption_profile: dict, frame_summary: dict) -> dict:
    """Keep both readings side by side rather than overwriting.

    The caption profile is still worth having - it describes the copy under
    the post - but it must never again be presented as the telop.
    """
    merged = dict(caption_profile)
    merged["caption"] = {
        k: caption_profile.get(k)
        for k in ("line_count", "avg_chars", "max_chars", "chars_per_sec")
    }
    for key in ("line_count", "avg_chars", "max_chars", "chars_per_sec"):
        merged.pop(key, None)
    merged["onscreen"] = frame_summary
    merged["onscreen_confidence"] = confidence_band(frame_summary)
    return merged
