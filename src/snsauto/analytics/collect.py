"""Performance collection and time-series accumulation."""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models import (
    MetricSnapshot,
    Publication,
    PublicationStatus,
)
from ..platforms import Capability, PlatformError, adapter_for_account

log = logging.getLogger(__name__)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class MetricsCollector:
    """Polls each platform and appends a snapshot per publication.

    Snapshots are appended, never updated, so the history supports
    velocity and cohort comparisons later.
    """

    def __init__(self, session, settings=None):
        self.session = session
        self.settings = settings

    def collect(self, publication: Publication) -> MetricSnapshot | None:
        if not publication.external_id:
            return None
        # Metrics have to be fetched with the token of the account that posted
        # it; another account's token returns "not found" for the same id.
        adapter, _ = adapter_for_account(
            publication.platform, self.session, self.settings,
            publication.project_id, publication.account_id,
        )
        if Capability.INSIGHTS not in adapter.capabilities():
            log.info("insights unavailable for %s - skipping", publication.platform.value)
            return None
        try:
            record = adapter.fetch_metrics(publication.external_id)
        except PlatformError as exc:
            log.warning("metrics fetch failed for publication %s: %s", publication.id, exc)
            return None

        # Instagram and TikTok report average watch time in seconds but never
        # as a fraction of the video, and the fraction is the only form that
        # compares a 15-second reel to a 60-second one. We hold the duration
        # ourselves, on the render the post was made from.
        if record.retention_rate is None and record.avg_watch_sec:
            duration = getattr(publication.render, "duration_sec", None)
            if duration:
                record.retention_rate = min(1.0, record.avg_watch_sec / duration)

        snapshot = MetricSnapshot(
            publication_id=publication.id,
            views=record.views, likes=record.likes, comments=record.comments,
            shares=record.shares, saves=record.saves,
            watch_time_sec=record.watch_time_sec,
            avg_watch_sec=record.avg_watch_sec,
            retention_rate=record.retention_rate,
            skip_rate=record.skip_rate,
            reach=record.reach,
            impressions=record.impressions,
            click_through_rate=record.click_through_rate,
            raw=record.raw,
        )
        self.session.add(snapshot)
        self.session.flush()
        return snapshot

    def collect_all(self, project_id: int | None = None) -> list[MetricSnapshot]:
        stmt = select(Publication).where(
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.external_id.is_not(None),
        )
        if project_id:
            stmt = stmt.where(Publication.project_id == project_id)
        collected = []
        for publication in self.session.scalars(stmt):
            snapshot = self.collect(publication)
            if snapshot:
                collected.append(snapshot)
        return collected


def growth_between(
    snapshots: list[MetricSnapshot], hours: float = 24.0
) -> dict[str, float]:
    """Change in each metric across the first ``hours`` after the first capture."""
    if len(snapshots) < 2:
        return {}
    ordered = sorted(snapshots, key=lambda s: s.captured_at)
    first = ordered[0]
    cutoff = _aware(first.captured_at) + timedelta(hours=hours)
    window = [s for s in ordered if _aware(s.captured_at) <= cutoff] or ordered[:2]
    last = window[-1]
    return {
        field: getattr(last, field) - getattr(first, field)
        for field in ("views", "likes", "comments", "shares", "saves")
    }


# A post's numbers mean nothing in its first hours: views climb steeply and
# then plateau, so a 2-hour-old post and a 2-month-old post are not the same
# measurement. The worker's snapshot schedule tops out at 24h, which makes 24h
# the first age at which a post can be compared to another one.
MATURITY_HOURS = 24.0


def metrics_at_age(publication: Publication, hours: float = MATURITY_HOURS) -> dict | None:
    """What this post's numbers were ``hours`` after it went out.

    This is the comparison that PDCA actually needs. Latest-state numbers are
    cumulative, so a cycle's week-old posts always lose to a baseline of
    year-old posts no matter how much better they are - the baseline simply
    had longer to accumulate. Reading both groups at the same age removes that
    bias entirely, and the snapshot series was already being collected for it.

    Returns None when the post is not yet that old, or when no snapshot was
    captured near that age: a missing measurement must not be substituted with
    a later one, which would reintroduce exactly the bias this removes.
    """
    published = _aware(publication.published_at)
    snapshots = sorted(publication.snapshots, key=lambda s: s.captured_at)
    if not published or not snapshots:
        return None

    aged = [
        (( _aware(s.captured_at) - published).total_seconds() / 3600.0, s)
        for s in snapshots
    ]
    at_or_before = [(age, s) for age, s in aged if 0 <= age <= hours]
    if not at_or_before:
        return None

    age, snapshot = max(at_or_before, key=lambda pair: pair[0])
    # Guard against reading a 2-hour snapshot as if it were the 24-hour one.
    # Half the window is generous enough for the worker's cadence to satisfy
    # and tight enough that the two groups stay comparable.
    if age < hours * 0.5:
        return None

    interactions = (
        snapshot.likes + snapshot.comments + snapshot.shares + snapshot.saves
    )
    return {
        "age_hours": round(age, 1),
        "views": snapshot.views,
        "likes": snapshot.likes,
        "comments": snapshot.comments,
        "shares": snapshot.shares,
        "saves": snapshot.saves,
        "engagement_rate": (
            round(interactions / snapshot.views, 5) if snapshot.views else 0.0
        ),
        "retention_rate": snapshot.retention_rate,
        "avg_watch_sec": snapshot.avg_watch_sec,
    }


def summarize_publication(publication: Publication) -> dict:
    """Latest state plus derived rates for one publication."""
    snapshots = sorted(publication.snapshots, key=lambda s: s.captured_at)
    if not snapshots:
        return {
            "publication_id": publication.id,
            "platform": publication.platform.value,
            "status": publication.status.value,
            "has_data": False,
        }

    latest = snapshots[-1]
    interactions = latest.likes + latest.comments + latest.shares + latest.saves
    published = _aware(publication.published_at) or _aware(snapshots[0].captured_at)
    age_h = max(
        0.5, (datetime.now(timezone.utc) - published).total_seconds() / 3600.0
    )

    return {
        "publication_id": publication.id,
        "platform": publication.platform.value,
        "status": publication.status.value,
        "url": publication.external_url,
        "has_data": True,
        "snapshots": len(snapshots),
        "views": latest.views,
        "likes": latest.likes,
        "comments": latest.comments,
        "shares": latest.shares,
        "saves": latest.saves,
        "engagement_rate": round(interactions / latest.views, 5) if latest.views else 0.0,
        "views_per_hour": round(latest.views / age_h, 2),
        # Retention stays None when unmeasured. Zero would mean "nobody
        # watched", which is a different claim and would poison any average.
        "retention_rate": latest.retention_rate,
        "avg_watch_sec": latest.avg_watch_sec,
        "skip_rate": latest.skip_rate,
        "reach": latest.reach,
        "has_retention": latest.retention_rate is not None,
        "age_hours": round(age_h, 1),
        "first_24h": growth_between(snapshots, 24.0),
        # Age-matched numbers, so two posts of different ages can be compared.
        "at_24h": metrics_at_age(publication, MATURITY_HOURS),
        "mature": age_h >= MATURITY_HOURS,
    }


def platform_breakdown(publications: list[Publication]) -> dict[str, dict]:
    """Aggregate performance per platform - the cross-channel comparison."""
    buckets: dict[str, list[dict]] = {}
    for publication in publications:
        summary = summarize_publication(publication)
        if summary.get("has_data"):
            buckets.setdefault(publication.platform.value, []).append(summary)

    out = {}
    for platform, rows in buckets.items():
        out[platform] = {
            "posts": len(rows),
            "total_views": sum(r["views"] for r in rows),
            "mean_engagement_rate": round(
                statistics.fmean(r["engagement_rate"] for r in rows), 5
            ),
            "median_views": statistics.median(r["views"] for r in rows),
            "best": max(rows, key=lambda r: r["engagement_rate"]),
        }
    return out
