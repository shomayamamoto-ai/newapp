"""Alerts and email delivery for unattended failures."""

import pytest

from snsauto.config import Settings
from snsauto.models import AlertLevel
from snsauto.notify import AlertService, build_email_sender, fingerprint_for
from snsauto.notify.email import EmailError, EmailSender


class Mailbox:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, to, subject, body):
        if self.fail:
            raise EmailError("relay refused")
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture
def settings():
    return Settings(_env_file=None, ALERT_EMAIL_TO="ops@example.com",
                    SMTP_HOST="smtp.example.com")


class TestFingerprint:
    def test_is_stable(self):
        assert fingerprint_for("a", "b", 1) == fingerprint_for("a", "b", 1)

    def test_separates_projects(self):
        assert fingerprint_for("a", "b", 1) != fingerprint_for("a", "b", 2)

    def test_separates_sources(self):
        assert fingerprint_for("a", "b") != fingerprint_for("c", "b")


class TestDeduplication:
    def test_repeats_bump_one_row(self, session, settings):
        """A worker retrying a dead token every minute must not create 60 rows."""
        service = AlertService(session, settings, sender=Mailbox())
        for _ in range(5):
            service.raise_alert("worker.publish", "トークンが失効しました")
        alerts = service.open_alerts()
        assert len(alerts) == 1 and alerts[0].count == 5

    def test_only_the_first_occurrence_emails(self, session, settings):
        mailbox = Mailbox()
        service = AlertService(session, settings, sender=mailbox)
        for _ in range(5):
            service.raise_alert("worker.publish", "同じ失敗")
        assert len(mailbox.sent) == 1

    def test_distinct_failures_are_separate(self, session, settings):
        mailbox = Mailbox()
        service = AlertService(session, settings, sender=mailbox)
        service.raise_alert("worker.publish", "失敗A")
        service.raise_alert("worker.publish", "失敗B")
        assert len(service.open_alerts()) == 2
        assert len(mailbox.sent) == 2

    def test_the_same_failure_after_acknowledging_is_new(self, session, settings):
        """Recurrence after a fix is genuinely new information."""
        mailbox = Mailbox()
        service = AlertService(session, settings, sender=mailbox)
        first = service.raise_alert("worker.publish", "失敗")
        service.acknowledge(first, "shoma")
        service.raise_alert("worker.publish", "失敗")
        assert len(mailbox.sent) == 2
        assert len(service.open_alerts()) == 1

    def test_detail_is_refreshed_on_repeat(self, session, settings):
        service = AlertService(session, settings, sender=Mailbox())
        service.raise_alert("x", "t", "first detail")
        service.raise_alert("x", "t", "second detail")
        assert service.open_alerts()[0].detail == "second detail"


class TestAcknowledge:
    def test_acknowledged_alerts_leave_the_open_list(self, session, settings):
        service = AlertService(session, settings, sender=Mailbox())
        alert = service.raise_alert("x", "t")
        assert service.open_alerts()
        service.acknowledge(alert, "shoma")
        assert service.open_alerts() == []
        assert alert.acknowledged_by == "shoma" and not alert.is_open

    def test_open_alerts_filter_by_project(self, session, project, settings):
        service = AlertService(session, settings, sender=Mailbox())
        service.raise_alert("x", "project one", project_id=project.id)
        service.raise_alert("x", "global")
        assert len(service.open_alerts(project_id=project.id)) == 1
        assert len(service.open_alerts()) == 2


class TestDelivery:
    def test_email_carries_the_context(self, session, settings):
        mailbox = Mailbox()
        AlertService(session, settings, sender=mailbox).raise_alert(
            "worker.publish", "投稿に失敗", "credentials missing",
            level=AlertLevel.ERROR,
        )
        message = mailbox.sent[0]
        assert message["to"] == "ops@example.com"
        assert "投稿に失敗" in message["subject"]
        assert "credentials missing" in message["body"]
        assert "worker.publish" in message["body"]

    def test_a_failing_mailer_does_not_break_the_caller(self, session, settings):
        """The work that raised the alert must still complete."""
        service = AlertService(session, settings, sender=Mailbox(fail=True))
        alert = service.raise_alert("x", "t")
        assert alert.id is not None
        assert alert.notified_at is None

    def test_no_mailer_still_records_the_alert(self, session):
        service = AlertService(session, Settings(_env_file=None), sender=None)
        assert service.raise_alert("x", "t").id is not None

    def test_notify_false_skips_mail(self, session, settings):
        mailbox = Mailbox()
        AlertService(session, settings, sender=mailbox).raise_alert("x", "t", notify=False)
        assert mailbox.sent == []


class TestSenderConstruction:
    def test_none_when_unconfigured(self):
        assert build_email_sender(Settings(_env_file=None)) is None

    def test_none_without_a_recipient(self):
        assert build_email_sender(
            Settings(_env_file=None, SMTP_HOST="smtp.example.com")
        ) is None

    def test_built_when_both_are_present(self):
        sender = build_email_sender(Settings(
            _env_file=None, SMTP_HOST="smtp.example.com", ALERT_EMAIL_TO="a@b.com",
        ))
        assert isinstance(sender, EmailSender) and sender.host == "smtp.example.com"

    def test_sender_falls_back_to_the_smtp_user(self):
        sender = EmailSender("h", user="bot@example.com")
        assert sender.sender == "bot@example.com"
