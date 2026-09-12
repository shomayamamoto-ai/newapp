"""Keyword research: collect the top N competing posts and rank them.

Ranking deliberately does not sort on raw view count. A 2M-view post from a
10M-follower account tells you little about whether the *format* works; a
40k-view post with an 11% engagement rate and steep velocity tells you a lot.
The composite score blends normalised engagement, velocity and reach so the
posts that surface are the ones whose structure is worth copying.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone

from ..models import CompetitorPost, Platform, Project, ResearchRun
from ..platforms import PostRecord, SearchOptions, get_adapter
from ..analytics.stats import percentile, reliability
from .timing import describe_best, posting_time_analysis
from .text import tokenize

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


# What an engagement number is divided by. Instagram's hashtag endpoints
# return no view count, so there is nothing to divide by and the resulting
# figure is interactions per post - a different quantity wearing the same name.
BASIS_VIEWS = "views"
BASIS_INTERACTIONS = "interactions"
BASIS_MIXED = "mixed"

BASIS_JA = {
    BASIS_VIEWS: "エンゲージ率（インタラクション÷再生数）",
    BASIS_INTERACTIONS: "インタラクション数（再生数が取得できないため率ではありません）",
    BASIS_MIXED: "基準が混在（比較不可）",
}


def engagement_basis(record: PostRecord) -> str:
    return BASIS_VIEWS if record.views > 0 else BASIS_INTERACTIONS


def corpus_basis(records: list[PostRecord]) -> str:
    """The basis a whole corpus shares, or `mixed` when it does not.

    A mixed corpus must never be aggregated into one engagement figure: the
    result is an average of a ratio and a count, which is not a quantity.
    """
    bases = {engagement_basis(r) for r in records}
    if not bases:
        return BASIS_VIEWS
    return bases.pop() if len(bases) == 1 else BASIS_MIXED


def engagement_rate(record: PostRecord) -> float:
    """Interactions per view.

    When the platform hides views there is no rate to compute. The value
    returned is then the raw interaction count, compressed only so that
    scoring still works within the corpus - and ``engagement_basis`` says so,
    so nothing downstream prints it as a percentage or compares it against a
    real rate from another platform.
    """
    interactions = record.likes + record.comments + record.shares
    if record.views > 0:
        return interactions / record.views
    return float(interactions)


def velocity(record: PostRecord, now: datetime | None = None) -> float:
    """Lifetime views per day.

    Not acceleration, despite what a single snapshot might suggest. Views are
    cumulative, so this is an average over the whole life of the post: a video
    that took 900k views in its first week and 100k over the next two years
    reports the same figure as one that trickled the whole million evenly.
    It is still the best signal available from one observation, and it is the
    right one for comparing posts of similar age, which a dated search returns.

    ``observed_velocity`` in ``watch.py`` measures the real thing, because two
    sweeps of the same post give an actual delta over an actual interval.
    """
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


def summarize_corpus(
    records: list[PostRecord], top_n: int = 10, timezone_name: str | None = None
) -> dict:
    """Aggregate patterns across the corpus - the actionable half of research."""
    if not records:
        return {"count": 0, "reliability": "insufficient"}

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

    basis = corpus_basis(records)
    timing = posting_time_analysis(records, engagement_rate, timezone_name)

    return {
        "count": len(records),
        # How much weight these aggregates can carry. A median over four posts
        # is arithmetic, not evidence, and the reader is told which it is.
        "reliability": reliability(len(records)),
        "engagement_basis": basis,
        "engagement_basis_label": BASIS_JA[basis],
        "engagement_is_rate": basis == BASIS_VIEWS,
        "engagement": {
            # Median first: one viral post in the corpus moves the mean and
            # not the median, and the mean is the one people quote.
            "median": statistics.median(engagements),
            "p25": percentile(engagements, 0.25),
            "p75": percentile(engagements, 0.75),
            "p90": percentile(engagements, 0.9),
            "mean": statistics.fmean(engagements),
        },
        "duration_sec": {
            "median_all": statistics.median(durations) if durations else None,
            "median_top": statistics.median(top_durations) if top_durations else None,
            "band_top": _duration_band(top_durations),
            "spread_top": _duration_spread(top_durations),
        },
        "timing": timing,
        "timing_summary": describe_best(timing),
        # Kept for compatibility, but it is the mode of when people post, not
        # of when posting works - `timing` is the one to read.
        "posting_hours_mode_utc": [h for h, _ in Counter(hours).most_common(3)],
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


DURATION_BANDS = (
    (0, 15, "0-15s"), (15, 30, "15-30s"), (30, 60, "30-60s"),
    (60, 180, "1-3min"), (180, 600, "3-10min"), (600, None, "10min+"),
)


def _band_of(duration: float) -> str:
    for lo, hi, label in DURATION_BANDS:
        if lo <= duration and (hi is None or duration < hi):
            return label
    return DURATION_BANDS[-1][2]


def _duration_band(durations: list[float]) -> str | None:
    """The band most of the winners are in - not the band their median lands in.

    Taking the median first and then banding it produces a band no post need
    occupy: winners at 15s and 180s have a median of 97s, which reports
    "1-3min" while half the winners are short-form. Counting posts per band
    keeps the answer to something that actually happened.
    """
    if not durations:
        return None
    counts = Counter(_band_of(d) for d in durations)
    ranked = counts.most_common()
    top, hits = ranked[0]
    # A plurality is not a pattern. A 5/5 split between 20-second and
    # 5-minute winners has no single answer, and naming one of them would
    # send half the production in the wrong direction.
    if len(ranked) > 1:
        if hits <= len(durations) / 2:
            return None
        if ranked[1][1] == hits:
            return None
    return top


def _duration_spread(durations: list[float]) -> dict | None:
    """The range the winners occupy, which a single band cannot express."""
    if not durations:
        return None
    return {
        "p25": round(percentile(durations, 0.25), 1),
        "median": round(statistics.median(durations), 1),
        "p75": round(percentile(durations, 0.75), 1),
        "distribution": Counter(_band_of(d) for d in durations).most_common(),
    }


def extract_tags(text: str) -> list[str]:
    return [t.lower() for t in re.findall(r"#([\w぀-ヿ一-鿿]+)", text)]


def _keywords(text: str) -> list[str]:
    """Content words, script-aware.

    This used to be a single character-class regex, which on Japanese matched
    a whole clause as one token - so `winning_words` counted every title once
    and ranked nothing. See ``text.tokenize``.
    """
    return tokenize(text)


def build_options(settings) -> SearchOptions:
    """Turn configuration into a SearchOptions. Defaults change nothing."""
    return SearchOptions(
        published_within_days=getattr(settings, "research_published_within_days", None),
        video_duration=getattr(settings, "research_video_duration", None),
        order=getattr(settings, "research_order", "relevance") or "relevance",
    )


def _split_csv(value: str | None) -> set[str]:
    return {v.strip().lstrip("@").lower() for v in (value or "").split(",") if v.strip()}


class ExclusionRules:
    """Drop posts that should never have been in the population.

    Your own account's posts are the obvious case: leaving them in means the
    corpus you are benchmarking against contains you, and the aggregate you
    compare yourself to moves every time you post.
    """

    def __init__(self, authors: str | None = None, pattern: str | None = None,
                 min_views: int = 0):
        self.authors = _split_csv(authors)
        self.pattern = re.compile(pattern, re.I) if pattern else None
        self.min_views = max(0, min_views or 0)

    @classmethod
    def from_settings(cls, settings) -> "ExclusionRules":
        return cls(
            authors=getattr(settings, "research_exclude_authors", None),
            pattern=getattr(settings, "research_exclude_pattern", None),
            min_views=getattr(settings, "research_min_views", 0),
        )

    @property
    def active(self) -> bool:
        return bool(self.authors or self.pattern or self.min_views)

    def reason(self, record: PostRecord) -> str | None:
        """Why this post is excluded, or None to keep it."""
        author = (record.author or "").lstrip("@").lower()
        if author and author in self.authors:
            return "excluded_author"
        if self.min_views and record.views < self.min_views:
            return "below_min_views"
        if self.pattern:
            haystack = f"{record.title or ''} {record.caption or ''}"
            if self.pattern.search(haystack):
                return "excluded_pattern"
        return None

    def apply(self, records: list[PostRecord]) -> tuple[list[PostRecord], dict]:
        kept, dropped = [], Counter()
        for record in records:
            reason = self.reason(record)
            if reason:
                dropped[reason] += 1
            else:
                kept.append(record)
        return kept, dict(dropped)


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
        options: SearchOptions | None = None,
        exclusions: "ExclusionRules | None" = None,
        account=None,
    ) -> ResearchRun:
        """Collect (or accept pre-collected) posts, score them, and store the run."""
        options = options or (
            build_options(self.settings) if self.settings else SearchOptions()
        )
        if exclusions is None and self.settings is not None:
            exclusions = ExclusionRules.from_settings(self.settings)

        shaping: dict = {}
        if records is None:
            adapter = get_adapter(platform, settings=self.settings)
            records = adapter.search(keyword, limit=limit, options=options)
            shaping = options.applied(adapter.supported_options())

        collected = len(records)
        dropped: dict = {}
        if exclusions is not None and exclusions.active:
            records, dropped = exclusions.apply(records)

        run = ResearchRun(
            project_id=project.id,
            keyword=keyword,
            platform=platform,
            limit=limit,
            source=source,
            account_id=getattr(account, "id", None),
            filters={
                **shaping,
                "collected": collected,
                "kept": len(records),
                "dropped": dropped,
            },
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
