"""Script (台本) generation.

With an API key the script is written by Claude against the research summary.
Without one, a structural fallback builds a beat-tiled script from the research
findings - it will not write good copy, but it produces the correct *shape*
(hook timing, beat count, duration band, hashtags) so the rest of the pipeline
stays exercisable and the writer only has to fill in words.
"""

from __future__ import annotations

from ..models import Platform, ResearchRun, Script
from ..research.keyword import summarize_corpus
from ..platforms import PostRecord

# Telop the viewer can read in one glance, per beat.
DEFAULT_BEAT_SEC = 3.0


def _records_from_run(run: ResearchRun) -> list[PostRecord]:
    return [
        PostRecord(
            external_id=p.external_id, platform=p.platform, url=p.url, title=p.title,
            caption=p.caption, author=p.author, published_at=p.published_at,
            duration_sec=p.duration_sec, views=p.views, likes=p.likes,
            comments=p.comments, shares=p.shares,
        )
        for p in run.posts
    ]


def _fallback_script(keyword: str, duration: float, research: dict) -> dict:
    """A structurally correct skeleton, derived from the research."""
    beat_count = max(4, int(round(duration / DEFAULT_BEAT_SEC)))
    step = duration / beat_count

    tags = [t for t, _ in research.get("top_hashtags", [])[:8]] or [keyword]
    words = [w for w, _ in research.get("winning_words", [])[:5]]

    hook = f"{keyword}、まだ知らないの？"
    cta = "保存して後で見返してね"

    lines = []
    for i in range(beat_count):
        start = round(i * step, 2)
        end = round(min(duration, (i + 1) * step), 2)
        if i == 0:
            telop, narration = hook, hook
        elif i == beat_count - 1:
            telop, narration = cta, cta
        else:
            topic = words[(i - 1) % len(words)] if words else keyword
            telop = f"ポイント{i}：{topic}"
            narration = f"{topic}について、ここを押さえてください。"
        lines.append({
            "index": i, "start": start, "end": end,
            "narration": narration, "telop": telop,
            "visual": f"Vertical shot illustrating: {keyword}",
        })

    return {
        "title": f"{keyword}｜{beat_count}ビート構成",
        "hook": hook,
        "body": f"{keyword}の要点を{beat_count - 2}つに分けて解説します。",
        "cta": cta,
        "lines": lines,
        "hashtags": tags,
        "rationale": (
            "LLM unavailable - skeleton generated from research: "
            f"duration band {research.get('duration_sec', {}).get('band_top')}, "
            f"{beat_count} beats at {step:.1f}s. Copy needs a human pass."
        ),
    }


class ScriptService:
    def __init__(self, session, llm=None):
        self.session = session
        self.llm = llm

    def generate(
        self,
        project,
        keyword: str,
        platform: Platform,
        duration: float = 30.0,
        run: ResearchRun | None = None,
        research: dict | None = None,
    ) -> Script:
        if research is None:
            research = summarize_corpus(_records_from_run(run)) if run else {"count": 0}

        if self.llm is not None:
            data = self.llm.write_script(
                keyword=keyword,
                platform=platform.value,
                duration=duration,
                brand_profile=project.brand_profile or {},
                research=research,
            )
        else:
            data = _fallback_script(keyword, duration, research)

        data["lines"] = self._repair_timing(data.get("lines", []), duration)

        script = Script(
            project_id=project.id,
            run_id=run.id if run else None,
            title=data.get("title") or keyword,
            platform=platform,
            target_duration_sec=duration,
            hook=data.get("hook"),
            body=data.get("body"),
            cta=data.get("cta"),
            lines=data["lines"],
            hashtags=data.get("hashtags", []),
            rationale=data.get("rationale"),
        )
        self.session.add(script)
        self.session.flush()
        return script

    @staticmethod
    def _repair_timing(lines: list[dict], duration: float) -> list[dict]:
        """Force beats to tile [0, duration] with no gaps or overlaps.

        A model asked for exact timings will occasionally leave a 0.2s seam.
        Left alone that seam becomes a black frame in the render, so close it
        here rather than trusting the generation.
        """
        if not lines:
            return lines
        ordered = sorted(lines, key=lambda l: (l.get("start", 0), l.get("index", 0)))
        cursor = 0.0
        repaired = []
        for i, line in enumerate(ordered):
            end = float(line.get("end", cursor + DEFAULT_BEAT_SEC))
            if i == len(ordered) - 1:
                end = duration
            end = max(end, cursor + 0.5)
            end = min(end, duration)
            repaired.append({**line, "index": i, "start": round(cursor, 2), "end": round(end, 2)})
            cursor = end
            if cursor >= duration:
                break
        if repaired:
            repaired[-1]["end"] = duration
        return repaired
