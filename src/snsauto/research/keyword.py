"""Keyword research: collect the top N competing posts and rank them.

Ranking deliberately does not sort on raw view count. A 2M-view post from a
10M-follower account tells you little about whether the *format* works; a
40k-view post with an 11% engagement rate and steep velocity tells you a lot.
The composite score blends normalised engagement, velocity and reach so the
posts that surface are the ones whose structure is worth copying.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime, timezone

from ..models import CompetitorPost, Platform, Project, ResearchRun
from ..platforms import PostRecord, get_adapter

# Composite weights. Engagement must be strictly dominant - greater than
# velocity and reach combined - because it is the only signal here that is
# independent of follower count. At 0.5/0.3/0.2 a huge-account post with weak
# engagement ties a small-account post with excellent engagement, which defeats
# the purpose of scoring at all.
W_ENGAGEMENT = 0.55
W_VELOCITY = 0.27
W_REACH = 0.18
assert W_ENGAGEMENT > W_VELOCITY + W_REACH


def _age_days(published_at: datetime | None, now: datetime | None = None) -> float:
    if not published_at:
        return 1.0
    now = now or datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return max((now - published_at).total_seconds() / 86400.0, 0.5)


def engagement_rate(record: PostRecord) -> float:
    """Interactions per view. Falls back to raw interactions when views are hidden."""
    interactions = record.likes + record.comments + record.shares
    if record.views > 0:
        return interactions / record.views
    return 0.0 if interactions == 0 else min(1.0, interactions / 1000.0)


def velocity(record: PostRecord, now: datetime | None = None) -> float:
    """Views per day since publication - how fast the post accelerated."""
    return record.views / _age_days(record.published_at, now)


def _normalize(values: list[float]) -> list[float]:
    """Log-compress then min-max. Social metrics are heavily long-tailed, so a
    plain min-max lets one viral outlier flatten everything else to ~0."""
    if not values:
        return []
    logged = [math.log1p(max(0.0, v)) for v in values]
    lo, hi = min(logged), max(logged)
    if hi - lo < 1e-9:
        return [0.5] * len(logged)
    return [(v - lo) / (hi - lo) for v in logged]


def score_posts(
    records: list[PostRecord], now: datetime | None = None
) -> list[tuple[PostRecord, dict]]:
    """Return records paired with their derived metrics, best first."""
    if not records:
        return []

    engagements = [engagement_rate(r) for r in records]
    velocities = [velocity(r, now) for r in records]
    reaches = [float(r.views) for r in records]

    n_eng = _normalize(engagements)
    n_vel = _normalize(velocities)
    n_reach = _normalize(reaches)

    scored = []
    for i, record in enumerate(records):
        composite = (
            W_ENGAGEMENT * n_eng[i] + W_VELOCITY * n_vel[i] + W_REACH * n_reach[i]
        )
        scored.append(
            (
                record,
                {
                    "engagement_rate": engagements[i],
                    "velocity": velocities[i],
                    "score": composite,
                },
            )
        )
    scored.sort(key=lambda pair: pair[1]["score"], reverse=True)
    return scored


def summarize_corpus(records: list[PostRecord], top_n: int = 10) -> dict:
    """Aggregate patterns across the corpus - the actionable half of research."""
    if not records:
        return {"count": 0}

    scored = score_posts(records)
    top = [r for r, _ in scored[:top_n]]

    durations = [r.duration_sec for r in records if r.duration_sec]
    top_durations = [r.duration_sec for r in top if r.duration_sec]
    hours = [
        r.published_at.astimezone(timezone.utc).hour
        for r in records
        if r.published_at
    ]

    hashtags: Counter[str] = Counter()
    for r in records:
        hashtags.update(extract_tags(r.caption or ""))

    top_words: Counter[str] = Counter()
    for r in top:
        top_words.update(_keywords(f"{r.title or ''} {r.caption or ''}"))

    engagements = [engagement_rate(r) for r in records]

    return {
        "count": len(records),
        "engagement": {
            "mean": statistics.fmean(engagements),
            "median": statistics.median(engagements),
            "p90": _percentile(engagements, 0.9),
        },
        "duration_sec": {
            "median_all": statistics.median(durations) if durations else None,
            "median_top": statistics.median(top_durations) if top_durations else None,
            "band_top": _duration_band(top_durations),
        },
        "best_posting_hours_utc": [h for h, _ in Counter(hours).most_common(3)],
        "top_hashtags": hashtags.most_common(15),
        "winning_words": top_words.most_common(20),
        "top_posts": [
            {
                "external_id": r.external_id,
                "url": r.url,
                "title": r.title,
                "views": r.views,
                "engagement_rate": round(m["engagement_rate"], 4),
                "score": round(m["score"], 4),
            }
            for r, m in scored[:top_n]
        ],
    }


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _duration_band(durations: list[float]) -> str | None:
    if not durations:
        return None
    med = statistics.median(durations)
    for lo, hi, label in [
        (0, 15, "0-15s"),
        (15, 30, "15-30s"),
        (30, 60, "30-60s"),
        (60, 180, "1-3min"),
        (180, 600, "3-10min"),
    ]:
        if lo <= med < hi:
            return label
    return "10min+"


_STOP = {
    "the", "and", "for", "you", "your", "with", "this", "that", "are", "was",
    "how", "what", "why", "から", "こと", "する", "です", "ます", "して", "ました",
    "この", "その", "ため", "よう", "など", "だけ", "でも", "ない",
}


def extract_tags(text: str) -> list[str]:
    import re

    return [t.lower() for t in re.findall(r"#([\w぀-ヿ一-鿿]+)", text)]


def _keywords(text: str) -> list[str]:
    import re

    tokens = re.findall(r"[\w぀-ヿ一-鿿]{2,}", text.lower())
    return [t for t in tokens if t not in _STOP and not t.isdigit()]


class ResearchService:
    """Runs a keyword sweep and persists it."""

    def __init__(self, session, settings=None):
        self.session = session
        self.settings = settings

    def run(
        self,
        project: Project,
        keyword: str,
        platform: Platform,
        limit: int = 50,
        records: list[PostRecord] | None = None,
        source: str = "api",
    ) -> ResearchRun:
        """Collect (or accept pre-collected) posts, score them, and store the run."""
        if records is None:
            adapter = get_adapter(platform, settings=self.settings)
            records = adapter.search(keyword, limit=limit)

        run = ResearchRun(
            project_id=project.id,
            keyword=keyword,
            platform=platform,
            limit=limit,
            source=source,
        )
        self.session.add(run)
        self.session.flush()

        seen: set[str] = set()
        for rank, (record, metrics) in enumerate(score_posts(records), start=1):
            if record.external_id in seen:
                continue  # paging can repeat items across pages
            seen.add(record.external_id)
            self.session.add(
                CompetitorPost(
                    run_id=run.id,
                    external_id=record.external_id,
                    platform=record.platform,
                    rank=rank,
                    url=record.url,
                    title=record.title,
                    caption=record.caption,
                    author=record.author,
                    published_at=record.published_at,
                    duration_sec=record.duration_sec,
                    views=record.views,
                    likes=record.likes,
                    comments=record.comments,
                    shares=record.shares,
                    engagement_rate=metrics["engagement_rate"],
                    velocity=metrics["velocity"],
                    score=metrics["score"],
                    raw=record.raw,
                )
            )
        self.session.flush()
        return run
