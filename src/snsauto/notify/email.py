"""SMTP notifications.

stdlib smtplib rather than a provider SDK: every mail service speaks SMTP, so
this works with Gmail, SES, Postmark or a company relay without another
dependency or another API key to rotate.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from ..config import get_settings

log = logging.getLogger(__name__)


class EmailError(RuntimeError):
    pass


class EmailSender:
    def __init__(
        self,
        host: str,
        port: int = 587,
        user: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        starttls: bool = True,
        timeout: float = 20.0,
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender or user or "snsauto@localhost"
        self.starttls = starttls
        self.timeout = timeout

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)

        try:
            if self.port == 465:
                with smtplib.SMTP_SSL(
                    self.host, self.port, timeout=self.timeout,
                    context=ssl.create_default_context(),
                ) as server:
                    self._authenticate(server)
                    server.send_message(message)
                return

            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                server.ehlo()
                if self.starttls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                self._authenticate(server)
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise EmailError(f"sending mail to {to} failed: {exc}") from exc

    def _authenticate(self, server: smtplib.SMTP) -> None:
        if self.user and self.password:
            server.login(self.user, self.password)


def build_email_sender(settings=None) -> EmailSender | None:
    """Return a sender, or None when mail is not configured."""
    settings = settings or get_settings()
    if not settings.smtp_host or not settings.alert_email_to:
        return None
    return EmailSender(
        settings.smtp_host, settings.smtp_port, settings.smtp_user,
        settings.smtp_password, settings.smtp_from, settings.smtp_starttls,
    )
