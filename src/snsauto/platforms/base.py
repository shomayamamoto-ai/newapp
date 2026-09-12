"""Common contract every platform adapter implements.

Each platform exposes three capabilities, and they are gated independently
because the real APIs gate them independently: you can read YouTube search
results with a plain API key but uploading needs OAuth; TikTok will not let
you read search results at all without a Research API grant.
`Capability` lets the pipeline degrade honestly instead of pretending.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..models import Platform


class Capability(str, enum.Enum):
    SEARCH = "search"
    PUBLISH = "publish"
    INSIGHTS = "insights"


class PlatformError(RuntimeError):
    """Adapter could not complete a call."""


class CredentialsMissing(PlatformError):
    """The adapter has no usable credentials for the requested capability."""


class CapabilityUnavailable(PlatformError):
    """The platform's API does not grant this capability to this app tier."""


@dataclass(slots=True)
class PostRecord:
    """A competing post, normalised across platforms."""

    external_id: str
    platform: Platform
    url: str | None = None
    title: str | None = None
    caption: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    duration_sec: float | None = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class SearchOptions:
    """How to shape the population a research run collects.

    The default population - "the N most relevant results" - is the wrong one
    surprisingly often: on YouTube it skews towards old, well-established
    videos, which is exactly the corpus you do *not* want when the question is
    what is working now. Every field here narrows it deliberately.

    Adapters ignore what their platform cannot express and report that through
    ``supported_options()``, so a run can say which filters actually applied
    instead of implying all of them did.
    """

    published_within_days: int | None = None
    video_duration: str | None = None        # short | medium | long
    order: str = "relevance"                 # relevance | date | views
    region: str | None = None

    def published_after(self) -> datetime | None:
        if not self.published_within_days:
            return None
        return datetime.now(timezone.utc) - timedelta(days=self.published_within_days)

    def applied(self, supported: set[str]) -> dict:
        """What was actually sent, and what the platform dropped."""
        requested = {
            "published_within_days": self.published_within_days,
            "video_duration": self.video_duration,
            "order": self.order if self.order != "relevance" else None,
            "region": self.region,
        }
        requested = {k: v for k, v in requested.items() if v}
        return {
            "applied": {k: v for k, v in requested.items() if k in supported},
            "ignored": {k: v for k, v in requested.items() if k not in supported},
        }


@dataclass(slots=True)
class PublishRequest:
    video_path: str
    caption: str = ""
    title: str | None = None
    hashtags: list[str] = field(default_factory=list)
    privacy: str = "public"
    scheduled_for: datetime | None = None
    extra: dict = field(default_factory=dict)

    def full_caption(self, limit: int | None = None) -> str:
        tags = " ".join(
            t if t.startswith("#") else f"#{t}" for t in self.hashtags if t.strip()
        )
        text = f"{self.caption}\n\n{tags}".strip() if tags else self.caption.strip()
        return text[:limit] if limit else text


@dataclass(slots=True)
class PublishResult:
    external_id: str | None
    url: str | None = None
    status: str = "published"
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class AccountProfile:
    """A competitor account, as the platform describes it.

    ``followers`` is the field keyword search never gives us. It is what lets
    an engagement rate be read as "this format worked" rather than "this
    account is big", so watching accounts is also how follower normalisation
    becomes possible at all.
    """

    handle: str
    platform: Platform
    external_id: str | None = None
    name: str | None = None
    followers: int | None = None
    post_count: int | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class CommentRecord:
    """One viewer comment. The text is the point - counts we already had."""

    external_id: str
    text: str
    author: str | None = None
    likes: int = 0
    published_at: datetime | None = None
    reply_count: int = 0


@dataclass(slots=True)
class MetricRecord:
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_sec: float = 0.0
    raw: dict = field(default_factory=dict)


@runtime_checkable
class PlatformAdapter(Protocol):
    platform: Platform

    def capabilities(self) -> set[Capability]: ...

    def search(
        self, keyword: str, limit: int = 50, options: SearchOptions | None = None
    ) -> list[PostRecord]: ...

    def publish(self, request: PublishRequest) -> PublishResult: ...

    def fetch_metrics(self, external_id: str) -> MetricRecord: ...


class BaseAdapter:
    """Shared defaults; subclasses override what the platform supports."""

    platform: Platform
    # Set when the caller resolved a connected account; adapters prefer it over
    # environment variables so a refreshed token takes effect immediately.
    credentials = None

    def token(self) -> str | None:
        return getattr(self.credentials, "access_token", None)

    def account_external_id(self) -> str | None:
        return getattr(self.credentials, "external_id", None)

    def capabilities(self) -> set[Capability]:
        return set()

    def _require(self, cap: Capability) -> None:
        if cap not in self.capabilities():
            raise CredentialsMissing(
                f"{self.platform.value}: '{cap.value}' unavailable - "
                f"missing credentials or unsupported. See .env.example."
            )

    def supported_options(self) -> set[str]:
        """Which SearchOptions fields this platform's API can express."""
        return set()

    def search(
        self, keyword: str, limit: int = 50, options: "SearchOptions | None" = None
    ) -> list[PostRecord]:
        raise CapabilityUnavailable(f"{self.platform.value} search not supported")

    def fetch_comments(self, external_id: str, limit: int = 50) -> list["CommentRecord"]:
        raise CapabilityUnavailable(f"{self.platform.value} comments not supported")

    def fetch_account(
        self, handle: str, limit: int = 25
    ) -> tuple["AccountProfile", list[PostRecord]]:
        """A named competitor's profile and recent posts."""
        raise CapabilityUnavailable(
            f"{self.platform.value} account lookup not supported"
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        raise CapabilityUnavailable(f"{self.platform.value} publish not supported")

    def fetch_metrics(self, external_id: str) -> MetricRecord:
        raise CapabilityUnavailable(f"{self.platform.value} insights not supported")
