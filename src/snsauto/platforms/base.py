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
from datetime import datetime
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

    def search(self, keyword: str, limit: int = 50) -> list[PostRecord]: ...

    def publish(self, request: PublishRequest) -> PublishResult: ...

    def fetch_metrics(self, external_id: str) -> MetricRecord: ...


class BaseAdapter:
    """Shared defaults; subclasses override what the platform supports."""

    platform: Platform

    def capabilities(self) -> set[Capability]:
        return set()

    def _require(self, cap: Capability) -> None:
        if cap not in self.capabilities():
            raise CredentialsMissing(
                f"{self.platform.value}: '{cap.value}' unavailable - "
                f"missing credentials or unsupported. See .env.example."
            )

    def search(self, keyword: str, limit: int = 50) -> list[PostRecord]:
        raise CapabilityUnavailable(f"{self.platform.value} search not supported")

    def publish(self, request: PublishRequest) -> PublishResult:
        raise CapabilityUnavailable(f"{self.platform.value} publish not supported")

    def fetch_metrics(self, external_id: str) -> MetricRecord:
        raise CapabilityUnavailable(f"{self.platform.value} insights not supported")
