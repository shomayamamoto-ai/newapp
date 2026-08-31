"""Platform adapter registry."""

from __future__ import annotations

from ..models import Platform
from .base import (
    BaseAdapter,
    Capability,
    CapabilityUnavailable,
    CredentialsMissing,
    MetricRecord,
    PlatformAdapter,
    PlatformError,
    PostRecord,
    PublishRequest,
    PublishResult,
)
from .instagram import InstagramAdapter
from .tiktok import TikTokAdapter
from .x import XAdapter
from .youtube import YouTubeAdapter

_REGISTRY = {
    Platform.YOUTUBE: YouTubeAdapter,
    Platform.TIKTOK: TikTokAdapter,
    Platform.INSTAGRAM: InstagramAdapter,
    Platform.X: XAdapter,
}


def get_adapter(platform: Platform | str, settings=None) -> BaseAdapter:
    if isinstance(platform, str):
        platform = Platform(platform)
    try:
        cls = _REGISTRY[platform]
    except KeyError:
        raise ValueError(f"unknown platform: {platform}") from None
    return cls(settings=settings)


def capability_matrix(settings=None) -> dict[str, dict[str, bool]]:
    """What this installation can actually do right now, given its credentials."""
    matrix = {}
    for platform in Platform:
        caps = get_adapter(platform, settings=settings).capabilities()
        matrix[platform.value] = {c.value: (c in caps) for c in Capability}
    return matrix


__all__ = [
    "BaseAdapter", "Capability", "CapabilityUnavailable", "CredentialsMissing",
    "MetricRecord", "PlatformAdapter", "PlatformError", "PostRecord",
    "PublishRequest", "PublishResult", "InstagramAdapter", "TikTokAdapter",
    "XAdapter", "YouTubeAdapter", "get_adapter", "capability_matrix",
]
