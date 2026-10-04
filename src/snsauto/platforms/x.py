"""X (Twitter) API v2 adapter.

Reading uses a bearer token (app-only). Posting a video needs the v1.1 chunked
media-upload endpoints under OAuth 1.0a user context, then v2 POST /2/tweets.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import httpx

from ..config import get_settings
from ..models import Platform
from ..utils.oauth1 import sign
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
    CommentRecord,
)

API = "https://api.x.com/2"
UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"
TWEET_MAX = 280      # weighted, not characters - see captions.py
CHUNK = 4 * 1024 * 1024


class XAdapter(BaseAdapter):
    platform = Platform.X

    def __init__(self, settings=None, client: httpx.Client | None = None,
                 credentials=None):
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(timeout=60.0)
        self.credentials = credentials

    def _user_token(self) -> tuple[str | None, str | None]:
        """The user's OAuth 1.0a token pair - connected account, else env."""
        token = self.token() or self.settings.x_access_token
        secret = (
            getattr(self.credentials, "token_secret", None)
            or self.settings.x_access_token_secret
        )
        return token, secret

    def capabilities(self) -> set[Capability]:
        caps: set[Capability] = set()
        if self.settings.x_bearer_token:
            caps |= {Capability.SEARCH, Capability.INSIGHTS}
        token, secret = self._user_token()
        if all((self.settings.x_api_key, self.settings.x_api_secret, token, secret)):
            caps.add(Capability.PUBLISH)
        return caps

    # ---------- research ----------

    # recent search covers the last 7 days only, so a longer window cannot be
    # honoured and `published_within_days` is clamped rather than ignored.
    RECENT_WINDOW_DAYS = 7

    def supported_options(self) -> set[str]:
        return {"published_within_days", "order"}

    def search(
        self, keyword: str, limit: int = 50, options: SearchOptions | None = None
    ) -> list[PostRecord]:
        self._require(Capability.SEARCH)
        options = options or SearchOptions()
        records: list[PostRecord] = []
        next_token: str | None = None

        while len(records) < limit:
            params = {
                "query": f"{keyword} -is:retweet",
                "max_results": min(100, max(10, limit - len(records))),
                "tweet.fields": "public_metrics,created_at,author_id,entities",
                "expansions": "author_id",
                "user.fields": "username",
                "sort_order": "recency" if options.order == "date" else "relevancy",
            }
            since = options.published_after()
            if since:
                floor = datetime.now(timezone.utc) - timedelta(
                    days=self.RECENT_WINDOW_DAYS
                )
                # +1 minute: the endpoint rejects a start_time at the exact edge.
                start = max(since, floor + timedelta(minutes=1))
                params["start_time"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
            if next_token:
                params["next_token"] = next_token
            data = self._get(f"{API}/tweets/search/recent", params)

            users = {
                u["id"]: u.get("username")
                for u in data.get("includes", {}).get("users", [])
            }
            for item in data.get("data", []):
                m = item.get("public_metrics", {})
                author = users.get(item.get("author_id"))
                records.append(
                    PostRecord(
                        external_id=item["id"],
                        platform=Platform.X,
                        url=f"https://x.com/{author or 'i'}/status/{item['id']}",
                        caption=item.get("text"),
                        author=author,
                        published_at=_parse_dt(item.get("created_at")),
                        views=int(m.get("impression_count", 0) or 0),
                        likes=int(m.get("like_count", 0) or 0),
                        comments=int(m.get("reply_count", 0) or 0),
                        shares=int(m.get("retweet_count", 0) or 0),
                        raw=item,
                    )
                )
            next_token = data.get("meta", {}).get("next_token")
            if not next_token:
                break

        return records[:limit]

    # ---------- account watch ----------

    def fetch_account(
        self, handle: str, limit: int = 25
    ) -> tuple[AccountProfile, list[PostRecord]]:
        """A named account's profile and its recent posts.

        The posts come from recent search, so the same 7-day ceiling applies:
        a competitor who posts weekly may show one item, and that is the tier,
        not their cadence.
        """
        self._require(Capability.SEARCH)
        name = handle.lstrip("@")
        data = self._get(f"{API}/users/by/username/{name}", {
            "user.fields": "public_metrics,name,description",
        })
        user = data.get("data")
        if not user:
            raise PlatformError(f"X account not found: {handle}")
        metrics = user.get("public_metrics", {})
        profile = AccountProfile(
            handle=name,
            platform=Platform.X,
            external_id=user.get("id"),
            name=user.get("name"),
            followers=int(metrics.get("followers_count", 0) or 0),
            post_count=int(metrics.get("tweet_count", 0) or 0),
            raw=user,
        )
        posts = self.search(
            f"from:{name}", limit=limit, options=SearchOptions(order="date")
        )
        return profile, posts

    # ---------- comments ----------

    def fetch_comments(self, external_id: str, limit: int = 50) -> list[CommentRecord]:
        """Replies to a post. On X a reply *is* the comment.

        Replies live in the same recent-search index as everything else, so
        the 7-day window applies here too: a post older than a week returns
        nothing, which is a limit of the tier, not an empty conversation.
        """
        self._require(Capability.SEARCH)
        data = self._get(f"{API}/tweets/search/recent", {
            "query": f"conversation_id:{external_id} is:reply",
            "max_results": min(100, max(10, limit)),
            "tweet.fields": "public_metrics,created_at,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        })
        users = {
            u["id"]: u.get("username")
            for u in data.get("includes", {}).get("users", [])
        }
        out = []
        for item in data.get("data", []):
            metrics = item.get("public_metrics", {})
            out.append(CommentRecord(
                external_id=item["id"],
                text=item.get("text", ""),
                author=users.get(item.get("author_id")),
                likes=int(metrics.get("like_count", 0) or 0),
                published_at=_parse_dt(item.get("created_at")),
                reply_count=int(metrics.get("reply_count", 0) or 0),
            ))
        return out[:limit]

    # ---------- publish ----------

    def publish(self, request: PublishRequest) -> PublishResult:
        self._require(Capability.PUBLISH)
        # X counts weighted characters: kana and kanji are 2 each, so a
        # Japanese post is over the limit at half the character count.
        payload: dict = {"text": request.full_caption(limit=TWEET_MAX, weighted=True)}

        if request.video_path:
            if not os.path.exists(request.video_path):
                raise PlatformError(f"video not found: {request.video_path}")
            media_id = self._upload_video(request.video_path)
            payload["media"] = {"media_ids": [media_id]}

        resp = self._client.post(
            f"{API}/tweets",
            json=payload,
            headers={
                "Authorization": self._oauth1("POST", f"{API}/tweets"),
                "Content-Type": "application/json",
            },
        )
        if resp.status_code >= 400:
            raise PlatformError(f"X tweet failed {resp.status_code}: {resp.text}")
        data = resp.json().get("data", {})
        tid = data.get("id")
        return PublishResult(
            external_id=tid,
            url=f"https://x.com/i/status/{tid}" if tid else None,
            raw=data,
        )

    def _upload_video(self, path: str) -> str:
        """v1.1 chunked upload: INIT -> APPEND* -> FINALIZE -> poll STATUS."""
        size = os.path.getsize(path)
        init_params = {
            "command": "INIT",
            "total_bytes": str(size),
            "media_type": "video/mp4",
            "media_category": "tweet_video",
        }
        resp = self._client.post(
            UPLOAD,
            data=init_params,
            headers={"Authorization": self._oauth1("POST", UPLOAD, init_params)},
        )
        if resp.status_code >= 400:
            raise PlatformError(f"X media INIT failed: {resp.text}")
        media_id = resp.json()["media_id_string"]

        with open(path, "rb") as fh:
            index = 0
            while chunk := fh.read(CHUNK):
                # Binary body is excluded from the OAuth signature base string.
                append = {
                    "command": "APPEND",
                    "media_id": media_id,
                    "segment_index": str(index),
                }
                r = self._client.post(
                    UPLOAD,
                    data=append,
                    files={"media": chunk},
                    headers={"Authorization": self._oauth1("POST", UPLOAD)},
                )
                if r.status_code >= 400:
                    raise PlatformError(f"X media APPEND {index} failed: {r.text}")
                index += 1

        fin = {"command": "FINALIZE", "media_id": media_id}
        r = self._client.post(
            UPLOAD, data=fin, headers={"Authorization": self._oauth1("POST", UPLOAD, fin)}
        )
        if r.status_code >= 400:
            raise PlatformError(f"X media FINALIZE failed: {r.text}")

        self._await_processing(media_id, r.json())
        return media_id

    def _await_processing(self, media_id: str, finalize_body: dict) -> None:
        info = finalize_body.get("processing_info")
        while info and info.get("state") in ("pending", "in_progress"):
            time.sleep(max(1, int(info.get("check_after_secs", 1))))
            params = {"command": "STATUS", "media_id": media_id}
            r = self._client.get(
                UPLOAD,
                params=params,
                headers={"Authorization": self._oauth1("GET", UPLOAD, params)},
            )
            if r.status_code >= 400:
                raise PlatformError(f"X media STATUS failed: {r.text}")
            info = r.json().get("processing_info")
        if info and info.get("state") == "failed":
            raise PlatformError(f"X media processing failed: {info}")

    # ---------- insights ----------

    def fetch_metrics(self, external_id: str) -> MetricRecord:
        self._require(Capability.INSIGHTS)
        data = self._get(
            f"{API}/tweets", {"ids": external_id, "tweet.fields": "public_metrics"}
        )
        items = data.get("data") or []
        if not items:
            raise PlatformError(f"X post not found: {external_id}")
        m = items[0].get("public_metrics", {})
        return MetricRecord(
            views=int(m.get("impression_count", 0) or 0),
            likes=int(m.get("like_count", 0) or 0),
            comments=int(m.get("reply_count", 0) or 0),
            shares=int(m.get("retweet_count", 0) or 0),
            saves=int(m.get("bookmark_count", 0) or 0),
            raw=m,
        )

    # ---------- plumbing ----------

    def _oauth1(self, method: str, url: str, params: dict | None = None) -> str:
        token, secret = self._user_token()
        return sign(
            method,
            url,
            consumer_key=self.settings.x_api_key or "",
            consumer_secret=self.settings.x_api_secret or "",
            token=token or "",
            token_secret=secret or "",
            params=params,
        )

    def _get(self, url: str, params: dict) -> dict:
        resp = self._client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self.settings.x_bearer_token}"},
        )
        if resp.status_code >= 400:
            raise PlatformError(f"X API {resp.status_code}: {resp.text}")
        return resp.json()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
