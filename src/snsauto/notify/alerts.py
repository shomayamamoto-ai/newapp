"""Raising, deduplicating and delivering alerts.

Unattended work fails quietly by nature: a worker whose Instagram token expired
keeps retrying every minute and nobody notices until the week's posts are
missing. Alerts make that visible in two places - the dashboard, and an email.

Two rules keep them useful rather than noisy:

* **Deduplication.** Repeats of the same failure bump a counter on one row
  instead of creating new ones. Sixty identical emails is the same as none.
* **One email per open alert.** Mail is sent when an alert first opens, not on
  every repeat. Acknowledging it and hitting the same failure again re-notifies,
  because that is genuinely new information.
"""

from __future__ import annotations

import hashlib
import logging

from ..config import get_settings
from ..models import Alert, AlertLevel, utcnow
from .email import EmailError, build_email_sender

log = logging.getLogger(__name__)


def fingerprint_for(source: str, title: str, project_id: int | None = None) -> str:
    raw = f"{source}|{title}|{project_id or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class AlertService:
    def __init__(self, session, settings=None, sender=None):
        self.session = session
        self.settings = settings or get_settings()
        self._sender = sender if sender is not None else build_email_sender(self.settings)

    def raise_alert(
        self,
        source: str,
        title: str,
        detail: str | None = None,
        level: AlertLevel = AlertLevel.ERROR,
        project_id: int | None = None,
        context: dict | None = None,
        notify: bool = True,
    ) -> Alert:
        key = fingerprint_for(source, title, project_id)
        alert = (
            self.session.query(Alert)
            .filter(Alert.fingerprint == key, Alert.acknowledged_at.is_(None))
            .one_or_none()
        )

        if alert is None:
            alert = Alert(
                project_id=project_id, level=level, source=source, title=title[:300],
                detail=detail, fingerprint=key, count=1, context=context or {},
            )
            self.session.add(alert)
            self.session.flush()
            if notify:
                self._notify(alert)
        else:
            alert.count += 1
            alert.last_seen_at = utcnow()
            if detail:
                alert.detail = detail
            self.session.flush()

        return alert

    def acknowledge(self, alert: Alert, by: str | None = None) -> Alert:
        alert.acknowledged_at = utcnow()
        alert.acknowledged_by = by
        self.session.flush()
        return alert

    def open_alerts(self, project_id: int | None = None) -> list[Alert]:
        query = self.session.query(Alert).filter(Alert.acknowledged_at.is_(None))
        if project_id:
            query = query.filter(Alert.project_id == project_id)
        return query.order_by(Alert.last_seen_at.desc()).all()

    def _notify(self, alert: Alert) -> None:
        if self._sender is None or not self.settings.alert_email_to:
            return
        body = (
            f"{alert.title}\n\n"
            f"source : {alert.source}\n"
            f"level  : {alert.level.value}\n"
            f"time   : {alert.last_seen_at:%Y-%m-%d %H:%M} UTC\n\n"
            f"{alert.detail or ''}\n"
        )
        try:
            self._sender.send(
                self.settings.alert_email_to,
                f"[snsauto] {alert.title[:120]}",
                body,
            )
            alert.notified_at = utcnow()
            self.session.flush()
        except EmailError as exc:
            # Failing to send the alert must not break the work that raised it.
            log.error("alert email failed: %s", exc)
