"""Composition and telop analysis of competing posts.

Two tiers, both usable:

* **Heuristic** (no credentials, no network): hook classification, CTA
  detection, hashtag extraction, and - when the video file is local - real cut
  detection via ffmpeg, giving cut count, average shot length and pacing.
* **LLM-assisted** (needs ANTHROPIC_API_KEY): beat-by-beat breakdown and
  transferable takeaways.

The heuristic tier is what runs by default, so analysis never silently
degrades to nothing when an API key is absent.
"""

from __future__ import annotations

import re
import statistics
from pathlib import Path

from ..models import CompetitorPost, StructureAnalysis
from ..media.ffmpeg import FFmpegError, detect_scenes, probe

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
        (l.strip() for l in re.split(r"[\n。.!！?？]", text) if l.strip()), ""
    )
    probe_text = first_line or text[:120]
    for name, pattern in HOOK_PATTERNS:
        if pattern.search(probe_text):
            return name, first_line
    return "statement", first_line


def detect_cta(text: str | None) -> str | None:
    if not text:
        return None
    for line in reversed([l.strip() for l in text.splitlines() if l.strip()]):
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
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    lengths = [len(l) for l in lines]
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

    def __init__(self, session, llm=None):
        self.session = session
        self.llm = llm

    def analyze(
        self, post: CompetitorPost, video_path: str | Path | None = None
    ) -> StructureAnalysis:
        text = "\n".join(filter(None, [post.title, post.caption]))
        hook_type, hook_text = classify_hook(text)
        cta = detect_cta(text)
        duration = post.duration_sec or 30.0

        telop = telop_profile(text, post.duration_sec)
        telop["hashtags"] = extract_hashtags(text)

        beats = estimate_beats(duration, hook_text, cta)
        takeaways: list = []

        if video_path and Path(video_path).exists():
            try:
                pacing = analyze_pacing(video_path)
                telop["pacing"] = pacing
                takeaways.append(
                    f"Cuts every {pacing['avg_shot_sec']}s on average "
                    f"({pacing['cut_count']} cuts in {pacing['duration_sec']}s)."
                )
            except FFmpegError as exc:
                telop["pacing_error"] = str(exc)

        if self.llm is not None:
            enriched = self.llm.analyze_structure(
                title=post.title, caption=post.caption, duration=duration
            )
            if enriched:
                beats = enriched.get("beats") or beats
                takeaways.extend(enriched.get("takeaways") or [])
                hook_type = enriched.get("hook_type") or hook_type
                cta = enriched.get("cta") or cta

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
