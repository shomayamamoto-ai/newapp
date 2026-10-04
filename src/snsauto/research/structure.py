"""Composition and telop analysis of competing posts.

Three tiers, each usable on its own:

* **Text** (no credentials, no network): hook classification, CTA detection
  and hashtag extraction from the title and caption.
* **Frames** (needs the video file): the telop actually burned into the
  picture, read by OCR, plus cut detection and an audio-bed measurement. This
  is the tier that makes "テロップ分析" mean what it says - see ``telop.py``.
* **LLM** (needs ANTHROPIC_API_KEY): beat-by-beat breakdown, transferable
  takeaways, and optional vision reads that add telop styling OCR cannot see.

Each tier degrades independently and records that it did. A run with no video
still produces text analysis; it just reports ``onscreen.reader == "none"``
rather than passing caption statistics off as telop.
"""

from __future__ import annotations

import logging
import re
import statistics
from pathlib import Path

from ..models import CompetitorPost, StructureAnalysis
from ..media.ffmpeg import FFmpegError, detect_scenes, probe
from .audio import analyze_audio
from .fetch import FetchedVideo, VideoFetcher, provenance_note
from .telop import (
    TesseractReader,
    VisionReader,
    analyze_telop,
    confidence_band,
    merge_into_profile,
)

log = logging.getLogger(__name__)

HASHTAG_RE = re.compile(r"#([\w぀-ヿ一-鿿]+)")

# Hook archetypes, most specific pattern first - order matters because a
# question containing a number should classify as a question, not a listicle.
HOOK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("question", re.compile(r"[?？]|なぜ|どうやって|知ってる|why|how do|what if", re.I)),
    ("negative", re.compile(r"やめて|ダメ|失敗|損する|間違|注意|危険|stop|never|mistake|worst", re.I)),
    # A bare leading digit is not enough: "3ヶ月で10kg痩せた結果" starts with a
    # number but is a result hook, so require an actual enumeration marker.
    ("listicle", re.compile(
        r"[0-9１-９]+\s*(つ|個|選|パターン|ステップ|"
        r"の(方法|コツ|理由|ポイント|習慣|ルール|特徴))"
        r"|\b(top\s*)?[0-9]+\s*(things|ways|tips|reasons|steps|rules)\b",
        re.I)),
    ("curiosity", re.compile(r"実は|意外と|知られて|裏技|秘密|secret|nobody tells|truth about", re.I)),
    ("result", re.compile(r"結果|できた|変わった|万円|kg|日で|before.?after|i tried|results", re.I)),
    ("authority", re.compile(r"プロが|医師|専門家|現役|年やって|as a|expert|dermatologist", re.I)),
    ("urgency", re.compile(r"今すぐ|期限|終了|急いで|残り|hurry|last chance|before it", re.I)),
]

CTA_PATTERNS = re.compile(
    r"保存|フォロー|コメント|プロフ|リンク|チェック|いいね|シェア|登録|"
    r"follow|comment|save this|link in bio|subscribe|share|check out",
    re.I,
)


def extract_hashtags(text: str | None) -> list[str]:
    return HASHTAG_RE.findall(text or "")


def classify_hook(text: str | None) -> tuple[str, str | None]:
    """Return (archetype, the opening line that carries it)."""
    if not text or not text.strip():
        return "unknown", None
    first_line = next(
        (part.strip() for part in re.split(r"[\n。.!！?？]", text) if part.strip()), ""
    )
    probe_text = first_line or text[:120]
    for name, pattern in HOOK_PATTERNS:
        if pattern.search(probe_text):
            return name, first_line
    return "statement", first_line


def detect_cta(text: str | None) -> str | None:
    if not text:
        return None
    for line in reversed([row.strip() for row in text.splitlines() if row.strip()]):
        if CTA_PATTERNS.search(line):
            return line[:200]
    return None


def analyze_pacing(video_path: str | Path, threshold: float = 0.30) -> dict:
    """Real cut detection: the editing rhythm of a competitor's video."""
    info = probe(video_path)
    duration = info["duration"] or 0.0
    cuts = detect_scenes(video_path, threshold)

    boundaries = [0.0, *cuts, duration] if duration else [0.0, *cuts]
    shots = [b - a for a, b in zip(boundaries, boundaries[1:]) if b > a]

    return {
        "duration_sec": round(duration, 2),
        "cut_count": len(cuts),
        "cuts_per_min": round(len(cuts) / duration * 60, 2) if duration else 0.0,
        "avg_shot_sec": round(statistics.fmean(shots), 2) if shots else None,
        "median_shot_sec": round(statistics.median(shots), 2) if shots else None,
        "shortest_shot_sec": round(min(shots), 2) if shots else None,
        "cut_times": [round(c, 2) for c in cuts],
        "resolution": f"{info['width']}x{info['height']}",
        "has_audio": info["has_audio"],
    }


def estimate_beats(duration: float, hook: str | None, cta: str | None) -> list[dict]:
    """A default short-form beat map, scaled to the target duration.

    Proportions follow the pattern that consistently retains on vertical feeds:
    a hook inside the first ~3 seconds, a fast payoff, then a short CTA.
    """
    duration = max(5.0, duration)
    hook_end = min(3.0, duration * 0.12)
    cta_start = duration - min(4.0, duration * 0.15)

    beats = [
        {"label": "hook", "start": 0.0, "end": round(hook_end, 2),
         "purpose": "Stop the scroll; state the payoff or the tension.",
         "text": hook},
        {"label": "context", "start": round(hook_end, 2),
         "end": round(hook_end + (cta_start - hook_end) * 0.25, 2),
         "purpose": "Earn the next 5 seconds: who this is for, why now."},
        {"label": "body", "start": round(hook_end + (cta_start - hook_end) * 0.25, 2),
         "end": round(cta_start, 2),
         "purpose": "Deliver the substance in the promised order."},
        {"label": "cta", "start": round(cta_start, 2), "end": round(duration, 2),
         "purpose": "One action, stated once.", "text": cta},
    ]
    return [b for b in beats if b["end"] > b["start"]]


def telop_profile(text: str | None, duration: float | None) -> dict:
    """Density statistics for on-screen text."""
    lines = [row.strip() for row in (text or "").splitlines() if row.strip()]
    lengths = [len(row) for row in lines]
    return {
        "line_count": len(lines),
        "avg_chars": round(statistics.fmean(lengths), 1) if lengths else 0.0,
        "max_chars": max(lengths) if lengths else 0,
        "chars_per_sec": (
            round(sum(lengths) / duration, 2) if duration and lengths else None
        ),
    }


class StructureService:
    """Analyses a stored CompetitorPost and persists the breakdown."""

    def __init__(self, session, llm=None, settings=None, fetcher=None):
        self.session = session
        self.llm = llm
        self.settings = settings
        self._fetcher = fetcher

    # ---------- video acquisition ----------

    def fetcher(self) -> VideoFetcher | None:
        if self._fetcher is None and self.settings is not None:
            self._fetcher = VideoFetcher(self.settings)
        return self._fetcher

    def _resolve_video(
        self, post: CompetitorPost, video_path
    ) -> FetchedVideo | None:
        if video_path and Path(video_path).exists():
            path = Path(video_path)
            return FetchedVideo(path, "local", bytes=path.stat().st_size)
        fetcher = self.fetcher()
        if fetcher is None:
            return None
        try:
            return fetcher.fetch(post)
        except Exception as exc:
            # Never let acquisition failure take down the run: the text tier
            # still has something to say about this post.
            log.warning("video fetch failed for %s: %s", post.external_id, exc)
            return None

    def _readers(self):
        """Which telop readers to use, honouring configuration."""
        mode = getattr(self.settings, "telop_reader", "auto") if self.settings else "auto"
        if mode == "off":
            return None, None
        vision = None
        if mode in ("auto", "vision") and self.llm is not None and hasattr(
            self.llm, "read_telop_frame"
        ):
            vision = VisionReader(self.llm)

        ocr = None
        if mode in ("auto", "tesseract") and TesseractReader.available():
            ocr = TesseractReader()
        elif mode == "vision" and vision is not None:
            # Vision-only: it does the bulk pass itself. Costs one model call
            # per distinct frame, so it is opt-in rather than the default.
            ocr, vision = vision, None

        return ocr, vision

    # ---------- frame tier ----------

    def analyze_frames(self, video: FetchedVideo) -> dict:
        """Everything measurable from the file itself."""
        settings = self.settings
        ocr, vision = self._readers()
        result: dict = {"provenance": video.source,
                        "provenance_note": provenance_note(video.source)}

        try:
            result["telop"] = analyze_telop(
                video.path,
                reader=ocr,
                interval=getattr(settings, "telop_interval_sec", 0.8),
                max_frames=getattr(settings, "telop_max_frames", 45),
                vision_reader=vision,
                vision_budget=getattr(settings, "telop_vision_budget", 6),
            )
        except (FFmpegError, OSError) as exc:
            result["telop_error"] = str(exc)

        try:
            result["pacing"] = analyze_pacing(video.path)
        except FFmpegError as exc:
            result["pacing_error"] = str(exc)

        try:
            result["audio"] = analyze_audio(video.path)
        except (FFmpegError, OSError) as exc:
            result["audio_error"] = str(exc)

        return result

    # ---------- orchestration ----------

    def analyze(
        self, post: CompetitorPost, video_path: str | Path | None = None
    ) -> StructureAnalysis:
        text = "\n".join(filter(None, [post.title, post.caption]))
        hook_type, hook_text = classify_hook(text)
        cta = detect_cta(text)
        duration = post.duration_sec or 30.0

        # The caption profile stays, but it is no longer called the telop.
        telop = telop_profile(text, post.duration_sec)
        telop["hashtags"] = extract_hashtags(text)

        beats = estimate_beats(duration, hook_text, cta)
        takeaways: list = []

        video = self._resolve_video(post, video_path)
        if video is not None:
            frames = self.analyze_frames(video)
            telop = merge_into_profile(telop, frames.get("telop", {}))
            telop["provenance"] = frames["provenance"]
            telop["provenance_note"] = frames["provenance_note"]
            for key in ("pacing", "pacing_error", "audio", "audio_error",
                        "telop_error"):
                if key in frames:
                    telop[key] = frames[key]
            takeaways.extend(_frame_takeaways(frames, duration))
            # A real beat map beats an estimated one.
            measured = _beats_from_telop(frames.get("telop", {}), duration)
            if measured:
                beats = measured
        else:
            telop = merge_into_profile(telop, {"reader": "none", "source": "none"})
            telop["onscreen_note"] = (
                "動画ファイルが無いため画面内テロップは未測定です。"
                "以下はキャプションの統計です。"
            )

        if self.llm is not None:
            enriched = self.llm.analyze_structure(
                title=post.title, caption=post.caption, duration=duration
            )
            if enriched:
                # Measured beats outrank inferred ones.
                if not _beats_are_measured(beats):
                    beats = enriched.get("beats") or beats
                takeaways.extend(enriched.get("takeaways") or [])
                hook_type = enriched.get("hook_type") or hook_type
                cta = enriched.get("cta") or cta

        # An on-screen hook is the real hook: it is what a scrolling viewer
        # reads, and it usually differs from the caption's first line.
        onscreen = telop.get("onscreen") or {}
        events = onscreen.get("events") or []
        if events and confidence_band(onscreen) in ("high", "medium"):
            first = events[0]["text"].replace("\n", "")
            hook_text = first
            hook_type, _ = classify_hook(first)

        # Assign through the relationship, not post_id. Setting the FK alone
        # leaves the already-loaded post.structure cached as None for the rest
        # of the session, so later readers (reports) see no analysis at all.
        analysis = post.structure or StructureAnalysis()
        analysis.hook_text = hook_text
        analysis.hook_type = hook_type
        analysis.beats = beats
        analysis.telop = telop
        analysis.cta = cta
        analysis.takeaways = takeaways
        post.structure = analysis

        self.session.add(analysis)
        self.session.flush()
        return analysis


def _beats_are_measured(beats: list) -> bool:
    return bool(beats) and any(b.get("measured") for b in beats)


def _beats_from_telop(summary: dict, duration: float) -> list[dict]:
    """Turn real telop events into a beat map, labelled as measured.

    Only when the read is trustworthy - a low-confidence OCR pass would
    otherwise replace a sensible estimate with noise.
    """
    events = summary.get("events") or []
    if not events or confidence_band(summary) not in ("high", "medium"):
        return []

    beats = []
    for i, event in enumerate(events):
        if i == 0:
            label = "hook"
        elif i == len(events) - 1:
            label = "cta"
        else:
            label = "body"
        beats.append({
            "label": label,
            "start": event["start"],
            "end": event["end"],
            "text": event["text"].replace("\n", " "),
            "position": event["position"],
            "purpose": BEAT_PURPOSE[label],
            "measured": True,
        })
    return beats


BEAT_PURPOSE = {
    "hook": "最初のテロップ。スクロールを止める役割。",
    "body": "本題。約束した順序で中身を出す。",
    "cta": "最後のテロップ。行動を一つだけ言う。",
}


def _frame_takeaways(frames: dict, duration: float) -> list[str]:
    """Transferable rules, stated in numbers taken from the video itself."""
    out: list[str] = []

    pacing = frames.get("pacing")
    if pacing and pacing.get("avg_shot_sec"):
        out.append(
            f"平均 {pacing['avg_shot_sec']}秒ごとにカット"
            f"（{pacing['duration_sec']}秒で{pacing['cut_count']}カット）。"
        )

    telop = frames.get("telop") or {}
    band = confidence_band(telop)
    if telop.get("event_count"):
        note = "" if band == "high" else f"（OCR信頼度: {band}）"
        out.append(
            f"テロップ{telop['event_count']}枚、画面占有率{telop['coverage_ratio']:.0%}、"
            f"1枚平均{telop['avg_chars']}文字を{telop.get('avg_hold_sec')}秒表示{note}。"
        )
        if telop.get("first_telop_sec") is not None:
            out.append(
                f"最初のテロップが {telop['first_telop_sec']}秒 で出る。"
            )
        if telop.get("dominant_position"):
            out.append(
                f"テロップ位置は主に画面{POSITION_JA[telop['dominant_position']]}。"
            )

    audio = frames.get("audio") or {}
    if audio.get("has_audio"):
        from .audio import AUDIO_STYLE_JA

        style = AUDIO_STYLE_JA.get(audio.get("audio_style"), "")
        if style:
            out.append(f"音声は{style}。無音区間は{audio['silence_ratio']:.0%}。")

    return out


POSITION_JA = {"top": "上部", "middle": "中央", "bottom": "下部"}
