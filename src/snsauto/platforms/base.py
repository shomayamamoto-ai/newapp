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

    def full_caption(self, limit: int | None = None, weighted: bool = False) -> str:
        """Caption plus hashtags, fitted to the platform's limit.

        The hashtags are reserved first and the body absorbs the cut. Doing it
        the other way - truncating the joined string - drops the tags entirely
        whenever the body is long, which is exactly when discovery matters
        most. ``weighted`` selects X's counting, where kana and kanji weigh
        two and a 280-character Japanese post is over the limit.
        """
        from .captions import truncate_caption, weighted_length

        measure = weighted_length if weighted else len
        tags = " ".join(
            t if t.startswith("#") else f"#{t}" for t in self.hashtags if t.strip()
        )
        body = self.caption.strip()

        if limit is None:
            return f"{body}\n\n{tags}".strip() if tags else body

        if not tags:
            return truncate_caption(body, limit, weighted)

        separator = "\n\n"
        reserved = measure(separator) + measure(tags)
        if reserved >= limit:
            # No room for both. The tags alone are more use than a body with
            # half a hashtag stuck to it.
            return truncate_caption(tags, limit, weighted)

        fitted = truncate_caption(body, limit - reserved, weighted)
        return f"{fitted}{separator}{tags}".strip() if fitted else tags


@dataclass(slots=True)
class PublishResult:
    """What the platform did with the upload - which is not always what we asked.

    ``visibility`` is separate from ``status`` because a post can be fully
    published and still invisible: YouTube locks every API upload to private
    until the project passes its compliance audit, and TikTok forces SELF_ONLY
    until the app passes its own. Both return success. Recording what the
    platform actually set, next to what we requested, is the only way the
    difference ever surfaces.

    ``warnings`` carries those in the operator's own language, for the post
    record and the screen.
    """

    external_id: str | None
    url: str | None = None
    status: str = "published"
    visibility: str | None = None
    requested_visibility: str | None = None
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def visibility_downgraded(self) -> bool:
        """The platform published it somewhere nobody can see it."""
        if not self.visibility or not self.requested_visibility:
            return False
        return self.visibility.lower() != self.requested_visibility.lower()


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
    """One reading of a post's performance.

    The retention fields are the ones that actually decide whether a
    short-form video worked, and they are reachable for *your own* posts on
    the platforms that expose an analytics surface - which is not the same
    surface the public counts come from. YouTube needs a second API entirely
    (youtubeanalytics.googleapis.com, `yt-analytics.readonly`); Instagram needs
    the insights edge. None of it is reachable for a competitor's post, on any
    platform, which is a rule about the APIs rather than a gap in this code.
    """

    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_sec: float = 0.0

    # Retention. `retention_rate` is the fraction of the video an average
    # viewer watched (0-1); None means the platform did not report it, which
    # must never be rendered as 0%.
    avg_watch_sec: float | None = None
    retention_rate: float | None = None
    skip_rate: float | None = None
    reach: int | None = None
    impressions: int | None = None
    click_through_rate: float | None = None

    raw: dict = field(default_factory=dict)

    def has_retention(self) -> bool:
        return self.retention_rate is not None or self.avg_watch_sec is not None


# Which metrics each platform will report, and for whom. Written down here
# rather than discovered per-call, because "we cannot get a competitor's saves"
# is a fact about the APIs that the UI has to be able to state.
#
# own:  reachable for a post you published, with the right grant.
# rival: reachable for someone else's post. Almost nothing is.
METRIC_AVAILABILITY: dict[str, dict[str, dict[str, bool]]] = {
    "youtube": {
        "views":          {"own": True,  "rival": True},
        "likes":          {"own": True,  "rival": True},
        "comments":       {"own": True,  "rival": True},
        "shares":         {"own": False, "rival": False},
        "saves":          {"own": False, "rival": False},
        # Analytics API, yt-analytics.readonly, owner only.
        "retention_rate": {"own": True,  "rival": False},
        "avg_watch_sec":  {"own": True,  "rival": False},
    },
    "instagram": {
        "views":          {"own": True,  "rival": False},
        "likes":          {"own": True,  "rival": True},
        "comments":       {"own": True,  "rival": True},
        "shares":         {"own": True,  "rival": False},
        "saves":          {"own": True,  "rival": False},
        "retention_rate": {"own": True,  "rival": False},
        "avg_watch_sec":  {"own": True,  "rival": False},
    },
    "x": {
        "views":          {"own": True,  "rival": True},
        "likes":          {"own": True,  "rival": True},
        "comments":       {"own": True,  "rival": True},
        "shares":         {"own": True,  "rival": True},
        # X is the one platform that exposes a save count publicly.
        "saves":          {"own": True,  "rival": True},
        "retention_rate": {"own": False, "rival": False},
        "avg_watch_sec":  {"own": False, "rival": False},
    },
    "tiktok": {
        "views":          {"own": True,  "rival": False},
        "likes":          {"own": True,  "rival": False},
        "comments":       {"own": True,  "rival": False},
        "shares":         {"own": True,  "rival": False},
        "saves":          {"own": False, "rival": False},
        # The Display API carries no watch-time field. Retention exists only
        # in the Research API, which is granted to approved institutions.
        "retention_rate": {"own": False, "rival": False},
        "avg_watch_sec":  {"own": False, "rival": False},
    },
}

METRIC_UNAVAILABLE_JA = {
    "retention_rate": "視聴維持率は自社投稿のみ（各社のInsights APIは自分の投稿にしか開放されていません）",
    "avg_watch_sec": "平均視聴時間は自社投稿のみ",
    "saves": "保存数は自社投稿のみ（Xのみブックマーク数が公開されています）",
    "shares": "シェア数はこのAPIでは提供されていません",
}


def metric_available(platform: str, metric: str, own: bool = True) -> bool:
    row = METRIC_AVAILABILITY.get(platform, {}).get(metric)
    return bool(row and row["own" if own else "rival"])


def unavailable_reason(platform: str, metric: str, own: bool = True) -> str | None:
    """Why a metric is missing, in a sentence the UI can print."""
    if metric_available(platform, metric, own):
        return None
    if not own:
        return (
            f"{metric} は競合投稿については取得できません。"
            "各プラットフォームのInsights APIは自社アカウントの投稿にのみ"
            "開放されているためで、回避策はありません。"
        )
    return METRIC_UNAVAILABLE_JA.get(
        metric, f"{platform} の公式APIでは {metric} を取得できません。"
    )


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
