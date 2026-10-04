"""TikTok adapter.

Publishing uses the Content Posting API (direct post), which does accept raw
bytes via FILE_UPLOAD. Two hard limits are worth knowing before you rely on it:

* Until your app passes TikTok's audit, direct posts are forced to
  SELF_ONLY visibility - the call succeeds but nobody else sees the video.
* **There is no public keyword-search API.** Competitor discovery is only
  available through the Research API, which is granted to approved academic
  institutions. ``search()`` therefore raises rather than scraping: scraping
  TikTok violates its Terms of Service. Use ``ingest_manual()`` to load a CSV
  of posts you collected by hand or licensed from a data vendor.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..config import get_settings
from ..models import Platform
from .base import (
    BaseAdapter,
    Capability,
    CapabilityUnavailable,
    MetricRecord,
    PlatformError,
    PostRecord,
    PublishRequest,
    PublishResult,
)

log = logging.getLogger(__name__)

API = "https://open.tiktokapis.com/v2"
CHUNK = 10 * 1024 * 1024


class TikTokAdapter(BaseAdapter):
    platform = Platform.TIKTOK

    def __init__(self, settings=None, client: httpx.Client | None = None,
                 credentials=None):
        self.settings = settings or get_settings()
        self._client = client or httpx.Client(timeout=120.0)
        self.credentials = credentials

    def _token(self) -> str | None:
        return self.token() or self.settings.tiktok_access_token

    def capabilities(self) -> set[Capability]:
        # SEARCH is deliberately never advertised - see the module docstring.
        if self._token():
            return {Capability.PUBLISH, Capability.INSIGHTS}
        return set()

    # ---------- research ----------

    def search(self, keyword: str, limit: int = 50) -> list[PostRecord]:
        raise CapabilityUnavailable(
            "TikTok exposes no public keyword-search API. Competitor data must "
            "come from the Research API (academic access), a licensed data "
            "vendor, or manual collection - load it with "
            "TikTokAdapter.ingest_manual(csv_path)."
        )

    @staticmethod
    def ingest_manual(csv_path: str | Path) -> list[PostRecord]:
        """Load competitor posts from a CSV you collected or licensed.

        Expected columns: external_id, url, title, caption, author, published_at,
        duration_sec, views, likes, comments, shares. Unknown columns are kept
        in ``raw`` so nothing you gathered is lost.
        """
        path = Path(csv_path)
        if not path.exists():
            raise PlatformError(f"CSV not found: {path}")

        records: list[PostRecord] = []
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for i, row in enumerate(csv.DictReader(fh)):
                records.append(
                    PostRecord(
                        external_id=str(row.get("external_id") or f"manual-{i}"),
                        platform=Platform.TIKTOK,
                        url=row.get("url") or None,
                        title=row.get("title") or None,
                        caption=row.get("caption") or None,
                        author=row.get("author") or None,
                        published_at=_parse_dt(row.get("published_at")),
                        duration_sec=_num(row.get("duration_sec"), float),
                        views=_num(row.get("views"), int) or 0,
                        likes=_num(row.get("likes"), int) or 0,
                        comments=_num(row.get("comments"), int) or 0,
                        shares=_num(row.get("shares"), int) or 0,
                        raw=dict(row),
                    )
                )
        return records

    # ---------- publish ----------

    def creator_info(self) -> dict:
        """What this creator is allowed to post right now.

        The useful field is ``privacy_level_options``: an app that has not
        passed TikTok's audit gets ``["SELF_ONLY"]`` and nothing else. That is
        the only way to learn, before uploading, that the post nobody will see
        is about to be made.
        """
        self._require(Capability.PUBLISH)
        response = self._post(f"{API}/post/publish/creator_info/query/", {})
        return response.get("data") or {}

    def allowed_privacy_levels(self) -> list[str]:
        try:
            return list(self.creator_info().get("privacy_level_options") or [])
        except PlatformError as exc:
            # Not being able to ask is not the same as being refused; the
            # publish path treats an empty list as "unknown" and proceeds.
            log.info("TikTok creator_info unavailable: %s", exc)
            return []

    def publish(self, request: PublishRequest) -> PublishResult:
        self._require(Capability.PUBLISH)
        path = request.video_path
        if not os.path.exists(path):
            raise PlatformError(f"video not found: {path}")
        size = os.path.getsize(path)

        wanted = request.extra.get("privacy_level", "PUBLIC_TO_EVERYONE")
        allowed = self.allowed_privacy_levels()
        warnings: list[str] = []
        if allowed and wanted not in allowed:
            # Asked for before the bytes go up, because TikTok accepts the
            # upload either way and silently files it as SELF_ONLY.
            warnings.append(
                f"TikTok はこのアカウントで「{wanted}」を許可していません"
                f"（選択できるのは {', '.join(allowed)} です）。"
                "アプリがContent Posting APIの監査を通過していない場合、"
                "投稿は「自分のみ表示」に固定されます。"
                "`snsauto setup gates` を参照してください。"
            )

        init = self._post(
            f"{API}/post/publish/video/init/",
            {
                "post_info": {
                    "title": request.full_caption(limit=2200),
                    "privacy_level": wanted,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": size,  # single-chunk upload
                    "total_chunk_count": 1,
                },
            },
        )
        data = init.get("data") or {}
        upload_url = data.get("upload_url")
        publish_id = data.get("publish_id")
        if not upload_url or not publish_id:
            raise PlatformError(f"TikTok init failed: {init}")

        with open(path, "rb") as fh:
            body = fh.read()
        put = self._client.put(
            upload_url,
            content=body,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
            timeout=None,
        )
        if put.status_code >= 400:
            raise PlatformError(f"TikTok upload failed {put.status_code}: {put.text}")

        return PublishResult(
            external_id=publish_id,
            status="processing",
            # TikTok settles visibility asynchronously, so the honest value
            # here is what it told us it would allow, not an assumption.
            visibility=(wanted if not warnings else (allowed[0] if allowed else None)),
            requested_visibility=wanted,
            warnings=warnings,
            raw=data,
        )

    def check_publish_status(self, publish_id: str) -> dict:
        """Poll a submitted post. TikTok finishes processing asynchronously."""
        self._require(Capability.PUBLISH)
        return self._post(
            f"{API}/post/publish/status/fetch/", {"publish_id": publish_id}
        )

    # ---------- insights ----------

    def fetch_metrics(self, external_id: str) -> MetricRecord:
        self._require(Capability.INSIGHTS)
        resp = self._post(
            f"{API}/video/query/",
            {"filters": {"video_ids": [external_id]}},
            params={
                "fields": "id,like_count,comment_count,share_count,view_count,title"
            },
        )
        videos = (resp.get("data") or {}).get("videos") or []
        if not videos:
            raise PlatformError(f"TikTok video not found: {external_id}")
        v = videos[0]
        return MetricRecord(
            views=int(v.get("view_count", 0) or 0),
            likes=int(v.get("like_count", 0) or 0),
            comments=int(v.get("comment_count", 0) or 0),
            shares=int(v.get("share_count", 0) or 0),
            raw=v,
        )

    # ---------- plumbing ----------

    def _post(self, url: str, payload: dict, params: dict | None = None) -> dict:
        resp = self._client.post(
            url,
            json=payload,
            params=params,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        if resp.status_code >= 400:
            raise PlatformError(f"TikTok API {resp.status_code}: {resp.text}")
        body = resp.json()
        err = body.get("error") or {}
        if err.get("code") not in (None, "ok"):
            raise PlatformError(f"TikTok API error: {err}")
        return body


def _num(value, cast):
    try:
        return cast(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
