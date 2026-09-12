"""Connected-account storage, token refresh and posting-rate accounting.

Adapters ask this module for credentials rather than reading settings, so a
connected account wins over an environment variable and a refreshed token
takes effect without a restart.

The rate accounting exists because both TikTok and Instagram cap how often an
account may publish, and hitting the cap is not free: the attempt is rejected
but still counts against the window on some platforms, and a scheduler that
retries blindly will burn the whole day's quota on errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models import Platform, PublishAttempt, SocialAccount, utcnow
from .oauth import ConnectedAccount, OAuthError, get_provider

log = logging.getLogger(__name__)

# Published caps, used only as a local guard. The platform is always the
# authority; Instagram is asked directly (see instagram.py).
DAILY_POST_CAP = {
    Platform.TIKTOK: 25,
    Platform.INSTAGRAM: 25,
    Platform.YOUTUBE: None,   # quota is per-project units, not per-post
    Platform.X: None,
}

# TikTok caps upload initiation at 6/min per user token.
BURST_LIMIT = {Platform.TIKTOK: (6, 60)}


@dataclass(slots=True)
class Credentials:
    """What an adapter needs to call the platform."""

    access_token: str | None = None
    token_secret: str | None = None
    external_id: str | None = None
    account_id: int | None = None
    source: str = "env"
    meta: dict | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class AccountService:
    def __init__(self, session, settings=None, provider_factory=None):
        from ..config import get_settings

        self.session = session
        self.settings = settings or get_settings()
        # Injectable so refresh can be exercised without reaching the network.
        self._provider_factory = provider_factory or (
            lambda platform: get_provider(platform, self.settings)
        )

    # ---------- lookup ----------

    def accounts(self, platform: Platform | None = None, project_id: int | None = None):
        stmt = select(SocialAccount).where(SocialAccount.is_active.is_(True))
        if platform:
            stmt = stmt.where(SocialAccount.platform == platform)
        if project_id:
            stmt = stmt.where(
                (SocialAccount.project_id == project_id)
                | (SocialAccount.project_id.is_(None))
            )
        return list(self.session.scalars(stmt.order_by(SocialAccount.id)))

    @staticmethod
    def _credentials_for(account: SocialAccount) -> Credentials:
        return Credentials(
            access_token=account.access_token,
            token_secret=account.token_secret,
            external_id=account.external_id,
            account_id=account.id,
            source="connected",
            meta=account.meta or {},
        )

    def resolve(
        self,
        platform: Platform,
        project_id: int | None = None,
        account_id: int | None = None,
    ) -> Credentials | None:
        """Credentials to post with.

        ``account_id`` names an exact account - that is how a post reaches one
        of several connected accounts on the same platform. Without it the
        project's own connection wins over a shared one, which keeps
        single-account installs working with no extra configuration.
        """
        if account_id is not None:
            account = self.session.get(SocialAccount, account_id)
            if account is None or not account.is_active:
                return None
            return self._credentials_for(account)

        found = self.accounts(platform, project_id)
        if found:
            found.sort(key=lambda a: (a.project_id is None, a.id))
            return self._credentials_for(found[0])
        return self._from_env(platform)

    def targets(
        self, platform: Platform, project_id: int | None = None
    ) -> list[SocialAccount]:
        """Every account this project could post to on a platform."""
        return self.accounts(platform, project_id)

    def label(self, account: SocialAccount) -> str:
        name = account.display_name or account.external_id
        handle = f"@{account.username}" if account.username else ""
        return f"{name} {handle}".strip()

    def _from_env(self, platform: Platform) -> Credentials | None:
        s = self.settings
        if platform is Platform.INSTAGRAM and s.ig_access_token and s.ig_user_id:
            return Credentials(s.ig_access_token, None, s.ig_user_id, None, "env")
        if platform is Platform.TIKTOK and s.tiktok_access_token:
            return Credentials(s.tiktok_access_token, None, None, None, "env")
        if platform is Platform.X and s.x_access_token and s.x_access_token_secret:
            return Credentials(
                s.x_access_token, s.x_access_token_secret, None, None, "env"
            )
        # YouTube's env path is a refresh token the adapter exchanges itself.
        return None

    # ---------- connect ----------

    def save(
        self, connected: ConnectedAccount, project_id: int | None = None
    ) -> SocialAccount:
        existing = self.session.scalars(
            select(SocialAccount).where(
                SocialAccount.platform == connected.platform,
                SocialAccount.external_id == connected.external_id,
                SocialAccount.project_id == project_id,
            )
        ).first()
        account = existing or SocialAccount(project_id=project_id)
        connected.apply_to(account)
        if existing is None:
            self.session.add(account)
        self.session.flush()
        log.info(
            "connected %s account %s (%s)",
            connected.platform.value, connected.external_id, connected.display_name,
        )
        return account

    def disconnect(self, account: SocialAccount) -> SocialAccount:
        account.is_active = False
        self.session.flush()
        return account

    # ---------- refresh ----------

    def needs_refresh(self, account: SocialAccount, now: datetime | None = None) -> bool:
        if not account.is_active or not account.expires_at:
            return False
        provider = self._provider_factory(account.platform)
        if not provider.supports_refresh:
            return False
        remaining = account.seconds_until_expiry(now or utcnow())
        margin = self.settings.token_refresh_margin_hours * 3600
        return remaining is not None and remaining <= margin

    def due_for_refresh(self, now: datetime | None = None) -> list[SocialAccount]:
        return [a for a in self.accounts() if self.needs_refresh(a, now)]

    def refresh(self, account: SocialAccount) -> SocialAccount:
        provider = self._provider_factory(account.platform)
        try:
            refreshed = provider.refresh(account)
        except OAuthError as exc:
            account.refresh_error = str(exc)[:2000]
            self.session.flush()
            raise
        except Exception as exc:
            # A network blip must not abort the worker tick that is also
            # publishing scheduled posts. Record it and move on.
            message = f"{type(exc).__name__}: {exc}"
            account.refresh_error = message[:2000]
            self.session.flush()
            raise OAuthError(message) from exc
        refreshed.apply_to(account)
        self.session.flush()
        log.info("refreshed %s token for %s", account.platform.value, account.external_id)
        return account

    def refresh_due(self, now: datetime | None = None) -> list[dict]:
        results = []
        for account in self.due_for_refresh(now):
            try:
                self.refresh(account)
                results.append({
                    "account_id": account.id, "platform": account.platform.value,
                    "status": "refreshed", "expires_at": account.expires_at,
                })
            except OAuthError as exc:
                results.append({
                    "account_id": account.id, "platform": account.platform.value,
                    "status": "failed", "error": str(exc),
                })
        return results

    # ---------- rate accounting ----------

    def record_attempt(
        self, platform: Platform, account_id: int | None,
        publication_id: int | None, succeeded: bool, error: str | None = None,
    ) -> PublishAttempt:
        attempt = PublishAttempt(
            platform=platform, account_id=account_id, publication_id=publication_id,
            succeeded=succeeded, error=(error or None) and error[:2000],
        )
        self.session.add(attempt)
        self.session.flush()
        return attempt

    def posts_in_window(
        self, platform: Platform, account_id: int | None = None,
        hours: float = 24.0, now: datetime | None = None,
    ) -> int:
        """Successful posts inside a *rolling* window - these caps do not reset
        at midnight, so a calendar-day count would be wrong."""
        now = now or utcnow()
        since = now - timedelta(hours=hours)
        stmt = select(PublishAttempt).where(
            PublishAttempt.platform == platform,
            PublishAttempt.succeeded.is_(True),
            PublishAttempt.attempted_at >= since,
        )
        if account_id:
            stmt = stmt.where(PublishAttempt.account_id == account_id)
        return len(list(self.session.scalars(stmt)))

    def check_rate(
        self, platform: Platform, account_id: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Whether another post is allowed right now, per our own accounting."""
        now = now or utcnow()
        cap = DAILY_POST_CAP.get(platform)
        used = self.posts_in_window(platform, account_id, 24.0, now)
        result = {"platform": platform.value, "used_24h": used, "cap": cap,
                  "allowed": True, "reason": None}

        if cap is not None and used >= cap:
            oldest = self.session.scalars(
                select(PublishAttempt)
                .where(
                    PublishAttempt.platform == platform,
                    PublishAttempt.succeeded.is_(True),
                    PublishAttempt.attempted_at >= now - timedelta(hours=24),
                )
                .order_by(PublishAttempt.attempted_at)
            ).first()
            frees_at = (
                _aware(oldest.attempted_at) + timedelta(hours=24) if oldest else None
            )
            result.update(
                allowed=False,
                reason=(
                    f"{platform.value} の24時間あたりの投稿上限（{cap}件）に達しています。"
                    + (f"次に空くのは {frees_at:%Y-%m-%d %H:%M} UTC です。" if frees_at else "")
                ),
                frees_at=frees_at,
            )
            return result

        burst = BURST_LIMIT.get(platform)
        if burst:
            limit, seconds = burst
            recent = self.posts_in_window(platform, account_id, seconds / 3600.0, now)
            if recent >= limit:
                result.update(
                    allowed=False,
                    reason=f"{platform.value} は{seconds}秒あたり{limit}件までです。少し待ってから再試行してください。",
                )
        return result
