"""Instagram Graph API adapter (Business / Creator accounts).

Two constraints shape this adapter and are worth stating plainly:

1. Reels publishing is a two-step container flow, and the video must already be
   hosted at a **publicly reachable HTTPS URL** - the Graph API pulls it, you
   cannot POST bytes. Upload the render to your own CDN/bucket first and pass
   the URL via ``PublishRequest.extra['video_url']``.
2. There is no general keyword search. Only hashtag search is exposed, via
   ``ig_hashtag_search`` -> ``top_media``, and it returns a limited window.
"""

from __future__ import annotations

import time

import httpx

from ..config import get_settings
from ..models import Platform
from .base import (
    BaseAdapter,
    Capability,
    MetricRecord,
    PlatformError,
    PostRecord,
    PublishRequest,
    PublishResult,
)

API = "https://graph.facebook.com/v21.0"


class InstagramAdapter(BaseAdapter):
    platform = Platform.INSTAGRAM

    def __init__(self, settings=None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(timeout=60.0)

    def capabilities(self) -> set[Capability]:
        if self.settings.ig_access_token and self.settings.ig_user_id:
            return {Capability.SEARCH, Capability.PUBLISH, Capability.INSIGHTS}
        return set()

    # ---------- research ----------

    def search(self, keyword: str, limit: int = 50) -> list[PostRecord]:
        self._require(Capability.SEARCH)
        tag = keyword.lstrip("#").replace(" ", "")
        found = self._get(
            f"{API}/ig_hashtag_search",
            {"user_id": self.settings.ig_user_id, "q": tag},
        )
        items = found.get("data") or []
        if not items:
            raise PlatformError(f"Instagram hashtag not found: {tag}")
        hashtag_id = items[0]["id"]

        media = self._get(
            f"{API}/{hashtag_id}/top_media",
            {
                "user_id": self.settings.ig_user_id,
                "fields": "id,caption,media_type,permalink,like_count,comments_count,timestamp",
                "limit": min(50, limit),
            },
        )
        records = []
        for item in (media.get("data") or [])[:limit]:
            records.append(
                PostRecord(
                    external_id=item["id"],
                    platform=Platform.INSTAGRAM,
                    url=item.get("permalink"),
                    caption=item.get("caption"),
                    published_at=_parse_dt(item.get("timestamp")),
                    likes=int(item.get("like_count", 0) or 0),
                    comments=int(item.get("comments_count", 0) or 0),
                    raw=item,
                )
            )
        return records

    # ---------- publish ----------

    def publish(self, request: PublishRequest) -> PublishResult:
        self._require(Capability.PUBLISH)
        video_url = request.extra.get("video_url")
        if not video_url:
            raise PlatformError(
                "Instagram requires a public HTTPS video_url. Upload the render "
                "to your own storage and pass extra={'video_url': ...}."
            )

        ig_user = self.settings.ig_user_id
        container = self._post(
            f"{API}/{ig_user}/media",
            {
                "media_type": "REELS",
                "video_url": video_url,
                "caption": request.full_caption(limit=2200),
                "share_to_feed": "true",
            },
        )
        creation_id = container.get("id")
        if not creation_id:
            raise PlatformError(f"Instagram container failed: {container}")

        self._await_container(creation_id)

        published = self._post(
            f"{API}/{ig_user}/media_publish", {"creation_id": creation_id}
        )
        mid = published.get("id")
        if not mid:
            raise PlatformError(f"Instagram publish failed: {published}")

        permalink = None
        try:
            permalink = self._get(f"{API}/{mid}", {"fields": "permalink"}).get(
                "permalink"
            )
        except PlatformError:
            pass  # Publishing succeeded; the permalink lookup is best-effort.

        return PublishResult(external_id=mid, url=permalink, raw=published)

    def _await_container(self, creation_id: str, timeout: float = 300.0) -> None:
        """Reels containers transcode asynchronously; publish only when FINISHED."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self._get(f"{API}/{creation_id}", {"fields": "status_code,status"})
            code = status.get("status_code")
            if code == "FINISHED":
                return
            if code == "ERROR":
                raise PlatformError(f"Instagram container error: {status}")
            time.sleep(5)
        raise PlatformError(f"Instagram container {creation_id} timed out")

    # ---------- insights ----------

    def fetch_metrics(self, external_id: str) -> MetricRecord:
        self._require(Capability.INSIGHTS)
        base = self._get(
            f"{API}/{external_id}", {"fields": "like_count,comments_count"}
        )
        record = MetricRecord(
            likes=int(base.get("like_count", 0) or 0),
            comments=int(base.get("comments_count", 0) or 0),
            raw={"base": base},
        )
        try:
            ins = self._get(
                f"{API}/{external_id}/insights",
                {"metric": "views,reach,saved,shares,ig_reels_video_view_total_time"},
            )
        except PlatformError:
            return record  # Insights need a recent post and extra permissions.

        values = {
            row["name"]: (row.get("values") or [{}])[0].get("value", 0)
            for row in ins.get("data", [])
        }
        record.views = int(values.get("views") or values.get("reach") or 0)
        record.saves = int(values.get("saved") or 0)
        record.shares = int(values.get("shares") or 0)
        # Graph reports total watch time in milliseconds.
        record.watch_time_sec = (
            float(values.get("ig_reels_video_view_total_time") or 0) / 1000.0
        )
        record.raw["insights"] = values
        return record

    # ---------- plumbing ----------

    def _get(self, url: str, params: dict) -> dict:
        params = {**params, "access_token": self.settings.ig_access_token}
        resp = self._client.get(url, params=params)
        if resp.status_code >= 400:
            raise PlatformError(f"Instagram API {resp.status_code}: {resp.text}")
        return resp.json()

    def _post(self, url: str, data: dict) -> dict:
        data = {**data, "access_token": self.settings.ig_access_token}
        resp = self._client.post(url, data=data)
        if resp.status_code >= 400:
            raise PlatformError(f"Instagram API {resp.status_code}: {resp.text}")
        return resp.json()


def _parse_dt(value: str | None):
    from datetime import datetime

    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("+0000", "+00:00"))
    except ValueError:
        return None
