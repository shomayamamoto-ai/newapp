"""Background worker: publishes scheduled posts and collects metrics.

Two jobs run on a loop:

* **publish** anything whose scheduled time has passed;
* **snapshot** metrics on a decaying cadence - 1h, 3h, 6h, 12h, 24h, 48h, 72h,
  1 week after publication. Short-form performance is decided in the first day,
  so polling every post hourly forever burns quota for nothing.

Publishing is claimed with a compare-and-set on the row before the API call, so
two workers on the same database cannot post the same video twice. That matters
more than it sounds: a duplicate post is not something you can quietly undo.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from ..analytics.collect import MetricsCollector
from ..notify import AlertService
from ..config import get_settings
from ..models import (
    MetricSnapshot,
    Publication,
    PublicationStatus,
    utcnow,
)
from ..platforms import Capability, PlatformError, PublishRequest, get_adapter

log = logging.getLogger(__name__)

# A claim older than this is treated as a crashed worker and may be retaken.
CLAIM_TIMEOUT = timedelta(minutes=30)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def next_metric_due(
    published_at: datetime, snapshot_count: int, schedule: tuple[float, ...]
) -> datetime | None:
    """When the next snapshot is due, or None once the schedule is exhausted."""
    if snapshot_count >= len(schedule):
        return None
    return _aware(published_at) + timedelta(hours=schedule[snapshot_count])


def due_metric_targets(
    publications: list[Publication],
    schedule: tuple[float, ...],
    now: datetime | None = None,
) -> list[Publication]:
    """Publications whose next scheduled snapshot has come due."""
    now = now or datetime.now(timezone.utc)
    due = []
    for pub in publications:
        published = _aware(pub.published_at) or _aware(pub.created_at)
        if published is None:
            continue
        when = next_metric_due(published, len(pub.snapshots), schedule)
        if when is not None and when <= now:
            due.append(pub)
    return due


class Worker:
    def __init__(self, session_factory, settings=None, identity: str | None = None):
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.identity = identity or worker_id()

    # ---------- publishing ----------

    def _claim(self, session, publication_id: int) -> bool:
        """Take exclusive ownership of a publication. False if someone else has it."""
        cutoff = utcnow() - CLAIM_TIMEOUT
        result = session.execute(
            update(Publication)
            .where(
                Publication.id == publication_id,
                Publication.status == PublicationStatus.SCHEDULED,
                # Free, or held by a worker that has clearly died.
                (Publication.claimed_by.is_(None)) | (Publication.claimed_at < cutoff),
            )
            .values(claimed_by=self.identity, claimed_at=utcnow())
        )
        return result.rowcount == 1

    def due_publications(self, session, now: datetime | None = None) -> list[Publication]:
        now = now or datetime.now(timezone.utc)
        rows = session.scalars(
            select(Publication).where(
                Publication.status == PublicationStatus.SCHEDULED,
                Publication.scheduled_for.is_not(None),
            )
        )
        return [p for p in rows if (_aware(p.scheduled_for) or now) <= now]

    def publish_due(self, session, now: datetime | None = None) -> list[dict]:
        outcomes = []
        for publication in self.due_publications(session, now):
            if not self._claim(session, publication.id):
                continue
            session.flush()
            outcomes.append(self._publish_one(session, publication))
        return outcomes

    def _publish_one(self, session, publication: Publication) -> dict:
        from ..models import Render, Script

        render = session.get(Render, publication.render_id) if publication.render_id else None
        script = session.get(Script, publication.script_id) if publication.script_id else None

        if render is None or not render.path:
            return self._fail(session, publication, "no render attached")

        adapter = get_adapter(publication.platform, settings=self.settings)
        if Capability.PUBLISH not in adapter.capabilities():
            return self._fail(
                session, publication,
                f"{publication.platform.value}: publish credentials not configured",
            )

        request = PublishRequest(
            video_path=render.path,
            caption=publication.caption or (script.hook if script else "") or "",
            title=script.title if script else None,
            hashtags=publication.hashtags or (script.hashtags if script else []) or [],
            extra=(publication.__dict__.get("extra") or {}),
        )
        try:
            result = adapter.publish(request)
        except PlatformError as exc:
            return self._fail(session, publication, str(exc))

        publication.external_id = result.external_id
        publication.external_url = result.url
        publication.status = PublicationStatus.PUBLISHED
        publication.published_at = utcnow()
        publication.error = None
        publication.claimed_by = None
        session.flush()
        log.info("published %s -> %s", publication.platform.value, result.url)
        return {
            "publication_id": publication.id, "platform": publication.platform.value,
            "status": "published", "url": result.url,
        }

    def _fail(self, session, publication: Publication, message: str) -> dict:
        publication.status = PublicationStatus.FAILED
        publication.error = message[:2000]
        publication.claimed_by = None
        session.flush()
        log.error("publish failed for %s: %s", publication.id, message)
        AlertService(session, self.settings).raise_alert(
            source="worker.publish",
            title=f"{publication.platform.value} への投稿に失敗しました",
            detail=message,
            project_id=publication.project_id,
            context={"publication_id": publication.id},
        )
        return {
            "publication_id": publication.id, "platform": publication.platform.value,
            "status": "failed", "error": message,
        }

    # ---------- metrics ----------

    def collect_due(self, session, now: datetime | None = None) -> list[MetricSnapshot]:
        published = list(
            session.scalars(
                select(Publication).where(
                    Publication.status == PublicationStatus.PUBLISHED,
                    Publication.external_id.is_not(None),
                )
            )
        )
        targets = due_metric_targets(published, self.settings.metric_schedule_hours, now)
        collector = MetricsCollector(session, self.settings)
        return [s for s in (collector.collect(p) for p in targets) if s]

    # ---------- loop ----------

    def tick(self, now: datetime | None = None) -> dict:
        with self.session_factory() as session:
            published = self.publish_due(session, now)
            snapshots = self.collect_due(session, now)
            session.commit()
        return {
            "at": (now or datetime.now(timezone.utc)).isoformat(),
            "published": published,
            "snapshots": len(snapshots),
        }

    def run(self, interval: float | None = None, max_ticks: int | None = None) -> None:
        interval = interval or self.settings.worker_interval_sec
        ticks = 0
        log.info("worker %s started, interval %.0fs", self.identity, interval)
        while max_ticks is None or ticks < max_ticks:
            try:
                summary = self.tick()
                if summary["published"] or summary["snapshots"]:
                    log.info("tick: %s", summary)
            except Exception:
                # A worker that dies on one bad row stops publishing everything.
                log.exception("worker tick failed; continuing")
            ticks += 1
            if max_ticks is None or ticks < max_ticks:
                time.sleep(interval)
