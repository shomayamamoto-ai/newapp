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
from ..platforms import Capability, PlatformError, get_adapter

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
        adapter = get_adapter(publication.platform, settings=self.settings)
        if Capability.INSIGHTS not in adapter.capabilities():
            log.info("insights unavailable for %s - skipping", publication.platform.value)
            return None
        try:
            record = adapter.fetch_metrics(publication.external_id)
        except PlatformError as exc:
            log.warning("metrics fetch failed for publication %s: %s", publication.id, exc)
            return None

        snapshot = MetricSnapshot(
            publication_id=publication.id,
            views=record.views, likes=record.likes, comments=record.comments,
            shares=record.shares, saves=record.saves,
            watch_time_sec=record.watch_time_sec, raw=record.raw,
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
        "age_hours": round(age_h, 1),
        "first_24h": growth_between(snapshots, 24.0),
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
