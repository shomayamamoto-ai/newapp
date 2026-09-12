"""YouTube Data API v3 adapter.

Search + statistics need only an API key. Uploading needs an OAuth2 refresh
token with the `youtube.upload` scope, which is why the two capabilities are
gated separately.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from ..models import Platform
from .base import (
    AccountProfile,
    BaseAdapter,
    Capability,
    CommentRecord,
    MetricRecord,
    PlatformError,
    PostRecord,
    PublishRequest,
    PublishResult,
    SearchOptions,
)

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3/videos"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# Retention lives on a different API from the public counts, with its own
# scope. videos.list will never return it however many parts you ask for.
ANALYTICS_API = "https://youtubeanalytics.googleapis.com/v2/reports"

_ISO_DUR = re.compile(
    r"P(?:(?P<d>\d+)D)?T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?"
)


def parse_iso8601_duration(value: str | None) -> float | None:
    """'PT1M30S' -> 90.0"""
    if not value:
        return None
    m = _ISO_DUR.fullmatch(value)
    if not m:
        return None
    p = {k: int(v) for k, v in m.groupdict(default="0").items()}
    return float(p["d"] * 86400 + p["h"] * 3600 + p["m"] * 60 + p["s"])


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class YouTubeAdapter(BaseAdapter):
    platform = Platform.YOUTUBE

    def __init__(self, settings=None, client: httpx.Client | None = None,
                 credentials=None):
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(timeout=60.0)
        self.credentials = credentials

    def capabilities(self) -> set[Capability]:
        caps: set[Capability] = set()
        if self.settings.youtube_api_key:
            caps |= {Capability.SEARCH, Capability.INSIGHTS}
        # A connected account already holds a live access token, refreshed by
        # the worker; the env path exchanges a refresh token per call.
        if self.token():
            caps |= {Capability.PUBLISH, Capability.INSIGHTS}
        elif all(
            (
                self.settings.youtube_client_id,
                self.settings.youtube_client_secret,
                self.settings.youtube_refresh_token,
            )
        ):
            caps |= {Capability.PUBLISH, Capability.INSIGHTS}
        return caps

    # ---------- research ----------

    # YouTube's search.list expresses all four shaping options.
    ORDER_MAP = {"relevance": "relevance", "date": "date", "views": "viewCount"}

    def supported_options(self) -> set[str]:
        return {"published_within_days", "video_duration", "order", "region"}

    def search(
        self, keyword: str, limit: int = 50, options: SearchOptions | None = None
    ) -> list[PostRecord]:
        self._require(Capability.SEARCH)
        options = options or SearchOptions()
        key = self.settings.youtube_api_key
        ids: list[str] = []
        page_token: str | None = None

        # search.list caps at 50 per page; page until `limit` is satisfied.
        while len(ids) < limit:
            params = {
                "part": "id",
                "q": keyword,
                "type": "video",
                "order": self.ORDER_MAP.get(options.order, "relevance"),
                "maxResults": min(50, limit - len(ids)),
                "key": key,
            }
            since = options.published_after()
            if since:
                params["publishedAfter"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
            if options.video_duration in ("short", "medium", "long"):
                params["videoDuration"] = options.video_duration
            if options.region:
                params["regionCode"] = options.region
            if page_token:
                params["pageToken"] = page_token
            data = self._get(f"{API}/search", params)
            ids.extend(
                item["id"]["videoId"]
                for item in data.get("items", [])
                if item.get("id", {}).get("videoId")
            )
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return self._hydrate(ids[:limit])

    def _hydrate(self, video_ids: list[str]) -> list[PostRecord]:
        """videos.list gives statistics + duration that search.list omits."""
        records: list[PostRecord] = []
        for chunk_start in range(0, len(video_ids), 50):
            chunk = video_ids[chunk_start : chunk_start + 50]
            data = self._get(
                f"{API}/videos",
                {
                    "part": "snippet,statistics,contentDetails",
                    "id": ",".join(chunk),
                    "key": self.settings.youtube_api_key,
                },
            )
            for item in data.get("items", []):
                snippet = item.get("snippet", {})
                stats = item.get("statistics", {})
                details = item.get("contentDetails", {})
                records.append(
                    PostRecord(
                        external_id=item["id"],
                        platform=Platform.YOUTUBE,
                        url=f"https://www.youtube.com/watch?v={item['id']}",
                        title=snippet.get("title"),
                        caption=snippet.get("description"),
                        author=snippet.get("channelTitle"),
                        published_at=_parse_dt(snippet.get("publishedAt")),
                        duration_sec=parse_iso8601_duration(details.get("duration")),
                        views=int(stats.get("viewCount", 0) or 0),
                        likes=int(stats.get("likeCount", 0) or 0),
                        comments=int(stats.get("commentCount", 0) or 0),
                        raw=item,
                    )
                )
        return records

    # ---------- account watch ----------

    def fetch_account(
        self, handle: str, limit: int = 25
    ) -> tuple[AccountProfile, list[PostRecord]]:
        """Resolve a @handle to a channel, then its most recent uploads.

        ``forHandle`` accepts the @name form directly, so the operator does not
        have to dig a channel ID out of a URL. subscriberCount comes back here
        and nowhere else - search.list never carries it.
        """
        self._require(Capability.SEARCH)
        key = self.settings.youtube_api_key
        data = self._get(f"{API}/channels", {
            "part": "snippet,statistics",
            "forHandle": handle if handle.startswith("@") else f"@{handle}",
            "key": key,
        })
        items = data.get("items") or []
        if not items:
            raise PlatformError(f"YouTube channel not found: {handle}")
        channel = items[0]
        stats = channel.get("statistics", {})

        profile = AccountProfile(
            handle=handle.lstrip("@"),
            platform=Platform.YOUTUBE,
            external_id=channel["id"],
            name=channel.get("snippet", {}).get("title"),
            followers=(
                None if stats.get("hiddenSubscriberCount")
                else int(stats.get("subscriberCount", 0) or 0)
            ),
            post_count=int(stats.get("videoCount", 0) or 0),
            raw=channel,
        )

        search = self._get(f"{API}/search", {
            "part": "id", "channelId": channel["id"], "type": "video",
            "order": "date", "maxResults": min(50, limit), "key": key,
        })
        ids = [
            item["id"]["videoId"] for item in search.get("items", [])
            if item.get("id", {}).get("videoId")
        ]
        return profile, self._hydrate(ids[:limit])

    # ---------- comments ----------

    def fetch_comments(self, external_id: str, limit: int = 50) -> list[CommentRecord]:
        """Top-level comments, most relevant first.

        Available with the same plain API key that search uses - no OAuth - so
        this works on any competitor's video whose owner has left comments on.
        A video with comments disabled returns 403; that is a fact about the
        video, not an error, so it comes back as an empty list.
        """
        self._require(Capability.SEARCH)
        out: list[CommentRecord] = []
        page_token: str | None = None
        while len(out) < limit:
            params = {
                "part": "snippet",
                "videoId": external_id,
                "maxResults": min(100, limit - len(out)),
                "order": "relevance",
                "textFormat": "plainText",
                "key": self.settings.youtube_api_key,
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                data = self._get(f"{API}/commentThreads", params)
            except PlatformError as exc:
                if "commentsDisabled" in str(exc) or "403" in str(exc):
                    return out
                raise
            for item in data.get("items", []):
                top = (item.get("snippet", {})
                           .get("topLevelComment", {})
                           .get("snippet", {}))
                if not top.get("textDisplay"):
                    continue
                out.append(CommentRecord(
                    external_id=item["id"],
                    text=top["textDisplay"],
                    author=top.get("authorDisplayName"),
                    likes=int(top.get("likeCount", 0) or 0),
                    published_at=_parse_dt(top.get("publishedAt")),
                    reply_count=int(
                        item.get("snippet", {}).get("totalReplyCount", 0) or 0
                    ),
                ))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out[:limit]

    # ---------- publish ----------

    def publish(self, request: PublishRequest) -> PublishResult:
        self._require(Capability.PUBLISH)
        token = self._access_token()
        path = request.video_path
        if not os.path.exists(path):
            raise PlatformError(f"video not found: {path}")

        body = {
            "snippet": {
                "title": (request.title or request.caption or "Untitled")[:100],
                "description": request.full_caption(limit=5000),
                "tags": [t.lstrip("#") for t in request.hashtags][:500],
            },
            "status": {
                "privacyStatus": request.privacy,
                "selfDeclaredMadeForKids": False,
            },
        }
        if request.scheduled_for:
            # A scheduled video must be uploaded private first.
            body["status"]["privacyStatus"] = "private"
            body["status"]["publishAt"] = request.scheduled_for.isoformat()

        size = os.path.getsize(path)
        init = self._client.post(
            UPLOAD_API,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {token}",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/*",
            },
            json=body,
        )
        if init.status_code >= 400:
            raise PlatformError(f"YouTube upload init failed: {init.text}")
        session_url = init.headers.get("Location")
        if not session_url:
            raise PlatformError("YouTube upload init returned no session URL")

        with open(path, "rb") as fh:
            resp = self._client.put(
                session_url,
                content=fh.read(),
                headers={"Content-Type": "video/*", "Content-Length": str(size)},
                timeout=None,
            )
        if resp.status_code >= 400:
            raise PlatformError(f"YouTube upload failed: {resp.text}")

        data = resp.json()
        vid = data.get("id")
        return PublishResult(
            external_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}" if vid else None,
            status="scheduled" if request.scheduled_for else "published",
            raw=data,
        )

    # ---------- insights ----------

    def fetch_metrics(self, external_id: str) -> MetricRecord:
        self._require(Capability.INSIGHTS)
        data = self._get(
            f"{API}/videos",
            {
                "part": "statistics",
                "id": external_id,
                "key": self.settings.youtube_api_key,
            },
        )
        items = data.get("items") or []
        if not items:
            raise PlatformError(f"YouTube video not found: {external_id}")
        stats = items[0].get("statistics", {})
        record = MetricRecord(
            views=int(stats.get("viewCount", 0) or 0),
            likes=int(stats.get("likeCount", 0) or 0),
            comments=int(stats.get("commentCount", 0) or 0),
            raw={"statistics": stats},
        )

        # Retention is a separate API and a separate grant. Failing to get it
        # must not lose the public counts we already have, so this is additive
        # and never raises.
        retention = self.fetch_retention(external_id)
        if retention:
            record.avg_watch_sec = retention.get("avg_watch_sec")
            record.retention_rate = retention.get("retention_rate")
            record.impressions = retention.get("impressions")
            record.click_through_rate = retention.get("click_through_rate")
            record.watch_time_sec = retention.get("watch_time_sec") or 0.0
            record.raw["analytics"] = retention
        return record

    def fetch_retention(self, external_id: str) -> dict | None:
        """Average view duration and percentage, from the Analytics API.

        Only works for a video on the authenticated channel: YouTube exposes
        retention to the owner and to nobody else, so there is no competitor
        equivalent of this call to write.

        Returns None - not zeros - when the grant is missing or the video is
        not ours. A retention of "0%" and "not measured" are different facts
        and a PDCA baseline that averages them together is wrong.
        """
        token = self.token()
        if not token:
            return None
        try:
            resp = self._client.get(
                ANALYTICS_API,
                params={
                    "ids": "channel==MINE",
                    # Wide enough to cover the video's whole life; the API
                    # rejects a request with no date range.
                    "startDate": "2005-02-14",   # YouTube's own launch date
                    "endDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "metrics": ",".join([
                        "views", "estimatedMinutesWatched",
                        "averageViewDuration", "averageViewPercentage",
                    ]),
                    "filters": f"video=={external_id}",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            log.info("YouTube Analytics unreachable for %s: %s", external_id, exc)
            return None

        if resp.status_code == 403:
            log.info(
                "YouTube Analytics denied for %s - the connected account is "
                "missing the yt-analytics.readonly scope. Reconnect it at "
                "/accounts to collect retention.", external_id,
            )
            return None
        if resp.status_code >= 400:
            log.info("YouTube Analytics %s for %s", resp.status_code, external_id)
            return None

        rows = (resp.json() or {}).get("rows") or []
        if not rows:
            return None
        views, minutes, avg_duration, avg_percent = (list(rows[0]) + [0] * 4)[:4]
        return {
            "views": int(views or 0),
            "watch_time_sec": float(minutes or 0) * 60.0,
            "avg_watch_sec": float(avg_duration or 0) or None,
            # The API reports a percentage; store the fraction so it never
            # gets rendered as 4500%.
            "retention_rate": (float(avg_percent) / 100.0) if avg_percent else None,
            "impressions": None,
            "click_through_rate": None,
        }

    # ---------- plumbing ----------

    def _access_token(self) -> str:
        connected = self.token()
        if connected:
            return connected
        resp = self._client.post(
            TOKEN_URL,
            data={
                "client_id": self.settings.youtube_client_id,
                "client_secret": self.settings.youtube_client_secret,
                "refresh_token": self.settings.youtube_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            raise PlatformError(f"YouTube token refresh failed: {resp.text}")
        return resp.json()["access_token"]

    def _get(self, url: str, params: dict) -> dict:
        resp = self._client.get(url, params=params)
        if resp.status_code >= 400:
            raise PlatformError(f"YouTube API {resp.status_code}: {resp.text}")
        return resp.json()
