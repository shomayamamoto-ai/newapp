"""Getting the competitor's video file, so the frames can be read.

Telop analysis, cut detection and audio analysis all need the actual file.
How that file may be obtained differs sharply per platform, and the difference
is contractual rather than technical, so it is made explicit here instead of
being buried in a downloader:

* ``api``      - the platform's own API handed us a media URL (Instagram
                 returns ``media_url`` for a media object). Fetching it is
                 ordinary, sanctioned API use.
* ``local``    - the operator put the file in the workspace themselves.
* ``external`` - a downloader command the operator configured. **Off by
                 default.** Downloading another account's video is governed by
                 each platform's terms of service, and that call belongs to the
                 operator, not to this code. When it is enabled the provenance
                 is recorded on every fetch so a report can never present
                 externally-obtained footage as API-sourced.

Nothing here scrapes a page or reverse-engineers a private endpoint.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..models import CompetitorPost

log = logging.getLogger(__name__)

# Direct-media fields the official APIs return, in the order we prefer them.
API_MEDIA_FIELDS = ("media_url", "video_url", "secure_url")

MAX_BYTES = 200 * 1024 * 1024  # a 3-minute 1080p short is ~60MB; 200MB is slack


class FetchError(RuntimeError):
    """The video could not be obtained. Never fatal - analysis degrades."""


@dataclass(slots=True)
class FetchedVideo:
    path: Path
    source: str          # api | local | external | cache
    url: str | None = None
    bytes: int = 0

    def as_dict(self) -> dict:
        return {"source": self.source, "bytes": self.bytes,
                "path": str(self.path), "url": self.url}


class VideoFetcher:
    """Resolves a competitor post to a local video file, or gives up cleanly."""

    def __init__(self, settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(timeout=120.0, follow_redirects=True)

    # ---------- cache ----------

    def cache_dir(self) -> Path:
        path = Path(self.settings.workspace) / "research-media"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cached_path(self, post: CompetitorPost) -> Path:
        digest = hashlib.sha256(
            f"{post.platform.value}:{post.external_id}".encode()
        ).hexdigest()[:20]
        return self.cache_dir() / f"{post.platform.value}-{digest}.mp4"

    # ---------- resolution ----------

    def api_media_url(self, post: CompetitorPost) -> str | None:
        """A media URL the platform's own API gave us."""
        raw = post.raw or {}
        for field in API_MEDIA_FIELDS:
            value = raw.get(field)
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None

    def fetch(self, post: CompetitorPost) -> FetchedVideo | None:
        """Return the video for this post, or None when it cannot be obtained.

        Callers treat None as "analyse what you can from the text" - a missing
        video must never fail a research run.
        """
        target = self.cached_path(post)
        if target.exists() and target.stat().st_size > 0:
            return FetchedVideo(target, "cache", bytes=target.stat().st_size)

        media_url = self.api_media_url(post)
        if media_url:
            try:
                return self._download(media_url, target, "api")
            except FetchError as exc:
                log.warning("api media fetch failed for %s: %s", post.external_id, exc)

        if self.settings.video_fetch_cmd:
            try:
                return self._external(post, target)
            except FetchError as exc:
                log.warning("external fetch failed for %s: %s", post.external_id, exc)

        return None

    # ---------- backends ----------

    def _download(self, url: str, target: Path, source: str) -> FetchedVideo:
        try:
            with self._client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise FetchError(f"HTTP {response.status_code} for {url[:80]}")
                written = 0
                partial = target.with_suffix(".part")
                with partial.open("wb") as fh:
                    for chunk in response.iter_bytes(65536):
                        written += len(chunk)
                        if written > MAX_BYTES:
                            partial.unlink(missing_ok=True)
                            raise FetchError(f"video exceeds {MAX_BYTES // 1048576}MB")
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        partial.replace(target)
        return FetchedVideo(target, source, url=url, bytes=target.stat().st_size)

    def _external(self, post: CompetitorPost, target: Path) -> FetchedVideo:
        """Run the operator's configured downloader.

        The command is a template with {url} and {out}. It is taken from
        configuration, never from the post, so a competitor's caption cannot
        influence what runs. Split with shlex and executed without a shell.
        """
        if not post.url:
            raise FetchError("post has no URL to hand the downloader")

        template = self.settings.video_fetch_cmd
        try:
            argv = [
                part.format(url=post.url, out=str(target))
                for part in shlex.split(template)
            ]
        except (KeyError, IndexError, ValueError) as exc:
            raise FetchError(
                f"VIDEO_FETCH_CMD is not a valid template: {exc}. "
                "Use {url} and {out}, e.g. "
                "'yt-dlp -f mp4 -o {out} {url}'"
            ) from exc

        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=self.settings.video_fetch_timeout,
        )
        if proc.returncode != 0 or not target.exists():
            raise FetchError(
                f"downloader exited {proc.returncode}: {proc.stderr[-400:] or 'no output'}"
            )
        return FetchedVideo(
            target, "external", url=post.url, bytes=target.stat().st_size
        )


def provenance_note(source: str) -> str:
    """One line, in the report, saying how this footage was obtained."""
    return {
        "api": "プラットフォーム公式APIが返したメディアURLから取得",
        "cache": "以前に取得済みのローカルキャッシュ",
        "local": "運用者がワークスペースに配置したファイル",
        "external": "運用者が設定した外部ダウンローダーで取得"
                    "（各プラットフォームの利用規約の確認は運用者の責任）",
    }.get(source, f"取得元不明: {source}")
