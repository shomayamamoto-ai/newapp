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
    AccountProfile,
    BaseAdapter,
    Capability,
    MetricRecord,
    PlatformError,
    PostRecord,
    PublishRequest,
    PublishResult,
    SearchOptions,
)

API = "https://graph.facebook.com/v21.0"


class InstagramAdapter(BaseAdapter):
    platform = Platform.INSTAGRAM

    def __init__(self, settings=None, client: httpx.Client | None = None,
                 credentials=None):
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(timeout=60.0)
        self.credentials = credentials

    def _token(self) -> str | None:
        return self.token() or self.settings.ig_access_token

    def _user_id(self) -> str | None:
        return self.account_external_id() or self.settings.ig_user_id

    def capabilities(self) -> set[Capability]:
        if self._token() and self._user_id():
            return {Capability.SEARCH, Capability.PUBLISH, Capability.INSIGHTS}
        return set()

    def publishing_limit(self) -> dict:
        """Ask Instagram how much of its own publishing quota is left.

        Meta's documentation quotes 25, 50 and 100 in different places, so the
        only trustworthy number is the one the account reports.
        """
        self._require(Capability.PUBLISH)
        data = self._get(
            f"{API}/{self._user_id()}/content_publishing_limit",
            {"fields": "config,quota_usage"},
        )
        rows = data.get("data") or [{}]
        row = rows[0]
        config = row.get("config") or {}
        quota = int(row.get("quota_usage") or 0)
        cap = int(config.get("quota_total") or 0) or None
        return {
            "used": quota,
            "cap": cap,
            "remaining": (cap - quota) if cap else None,
            "window_hours": config.get("quota_duration", 86400) / 3600
            if config.get("quota_duration") else 24,
            "raw": row,
        }

    # ---------- research ----------

    def supported_options(self) -> set[str]:
        # The hashtag endpoints take no filters at all: no date range, no
        # duration, no sort. Everything has to be filtered after collection.
        return set()

    def search(
        self, keyword: str, limit: int = 50, options: SearchOptions | None = None
    ) -> list[PostRecord]:
        self._require(Capability.SEARCH)
        tag = keyword.lstrip("#").replace(" ", "")
        found = self._get(
            f"{API}/ig_hashtag_search",
            {"user_id": self._user_id(), "q": tag},
        )
        items = found.get("data") or []
        if not items:
            raise PlatformError(f"Instagram hashtag not found: {tag}")
        hashtag_id = items[0]["id"]

        # top_media caps at 50 per page. Without following `paging.next` a
        # request for 50 could return 20 and silently look like "that is all
        # there is"; media_url is requested so the frame tier has something
        # sanctioned to fetch.
        raw_items: list[dict] = []
        params = {
            "user_id": self._user_id(),
            "fields": "id,caption,media_type,media_url,permalink,"
                      "like_count,comments_count,timestamp",
            "limit": min(50, limit),
        }
        url = f"{API}/{hashtag_id}/top_media"
        seen_pages = 0
        while len(raw_items) < limit and seen_pages < 10:
            media = self._get(url, params)
            page = media.get("data") or []
            if not page:
                break
            raw_items.extend(page)
            after = (media.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not (media.get("paging") or {}).get("next"):
                break
            params = {**params, "after": after}
            seen_pages += 1

        records = []
        for item in raw_items[:limit]:
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

    # ---------- account watch ----------

    def fetch_account(
        self, handle: str, limit: int = 25
    ) -> tuple[AccountProfile, list[PostRecord]]:
        """A competitor's Business/Creator account, via business_discovery.

        This is the one Instagram endpoint that returns a *rival's* numbers,
        and it returns the two the hashtag endpoints never do: followers_count,
        and media_product_type, which is how a Reel is told from a feed post.
        It only works when the target is a Business or Creator account - a
        personal account is invisible to the API, which is a property of their
        account rather than an error in the request.
        """
        self._require(Capability.SEARCH)
        name = handle.lstrip("@")
        fields = (
            f"business_discovery.username({name}){{"
            "followers_count,media_count,name,username,"
            f"media.limit({min(50, limit)}){{"
            "id,caption,like_count,comments_count,media_type,"
            "media_product_type,media_url,permalink,timestamp"
            "}}}"
        )
        try:
            data = self._get(f"{API}/{self._user_id()}", {"fields": fields})
        except PlatformError as exc:
            if "business_discovery" in str(exc) or "110" in str(exc):
                raise PlatformError(
                    f"Instagram: @{name} をビジネス/クリエイターアカウントとして"
                    "取得できません。相手が個人アカウントの場合、APIからは参照でき"
                    "ません（規約上の制限であり、回避策はありません）。"
                ) from exc
            raise

        discovery = data.get("business_discovery") or {}
        if not discovery:
            raise PlatformError(f"Instagram account not found: {handle}")

        profile = AccountProfile(
            handle=name,
            platform=Platform.INSTAGRAM,
            external_id=discovery.get("id"),
            name=discovery.get("name"),
            followers=int(discovery.get("followers_count", 0) or 0),
            post_count=int(discovery.get("media_count", 0) or 0),
            raw={k: v for k, v in discovery.items() if k != "media"},
        )

        records = []
        for item in ((discovery.get("media") or {}).get("data") or [])[:limit]:
            records.append(PostRecord(
                external_id=item["id"],
                platform=Platform.INSTAGRAM,
                url=item.get("permalink"),
                caption=item.get("caption"),
                author=name,
                published_at=_parse_dt(item.get("timestamp")),
                likes=int(item.get("like_count", 0) or 0),
                comments=int(item.get("comments_count", 0) or 0),
                raw=item,
            ))
        return profile, records

    # ---------- publish ----------

    def publish(self, request: PublishRequest) -> PublishResult:
        self._require(Capability.PUBLISH)
        video_url = request.extra.get("video_url")
        if not video_url:
            raise PlatformError(
                "Instagram requires a public HTTPS video_url. Upload the render "
                "to your own storage and pass extra={'video_url': ...}."
            )

        ig_user = self._user_id()
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
                {"metric": ",".join([
                    "views", "reach", "saved", "shares",
                    "ig_reels_video_view_total_time",
                    # Retention. Average watch time is the one that decides
                    # whether a reel worked; skip rate is its mirror image.
                    "ig_reels_avg_watch_time", "reels_skip_rate",
                ])},
            )
        except PlatformError:
            return record  # Insights need a recent post and extra permissions.

        values = {
            row["name"]: (row.get("values") or [{}])[0].get("value", 0)
            for row in ins.get("data", [])
        }
        record.views = int(values.get("views") or values.get("reach") or 0)
        record.reach = int(values.get("reach") or 0) or None
        record.saves = int(values.get("saved") or 0)
        record.shares = int(values.get("shares") or 0)
        # Graph reports both watch-time metrics in milliseconds.
        record.watch_time_sec = (
            float(values.get("ig_reels_video_view_total_time") or 0) / 1000.0
        )
        avg_ms = values.get("ig_reels_avg_watch_time")
        record.avg_watch_sec = (float(avg_ms) / 1000.0) if avg_ms else None
        skip = values.get("reels_skip_rate")
        # Reported as a percentage; stored as a fraction so it is never
        # rendered as 3400%.
        record.skip_rate = (float(skip) / 100.0) if skip else None
        record.raw["insights"] = values
        return record

    def retention_for(self, external_id: str, duration_sec: float | None) -> float | None:
        """Average watch time as a fraction of the reel's length.

        Instagram reports the seconds but never the ratio, and the ratio is
        what compares across reels of different lengths. Needs the duration
        from our own publication record, because the insights edge does not
        carry it either.
        """
        if not duration_sec:
            return None
        record = self.fetch_metrics(external_id)
        if record.avg_watch_sec is None:
            return None
        return min(1.0, record.avg_watch_sec / duration_sec)

    # ---------- plumbing ----------

    def _get(self, url: str, params: dict) -> dict:
        params = {**params, "access_token": self._token()}
        resp = self._client.get(url, params=params)
        if resp.status_code >= 400:
            raise PlatformError(f"Instagram API {resp.status_code}: {resp.text}")
        return resp.json()

    def _post(self, url: str, data: dict) -> dict:
        data = {**data, "access_token": self._token()}
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
