"""Real / stock footage sourcing (visual mode ``footage``).

Two sources, one interface:

* a **local library** - point ``SNSAUTO_FOOTAGE_DIR`` at a folder of clips and
  they are indexed with their real duration and resolution (read with ffmpeg),
  plus keywords taken from the filename and an optional sidecar ``.json``.
* a **stock API** - a generic keyword-search endpoint.

Matching scores a clip against a shot on keyword overlap, orientation and
duration fit, and - importantly - will not reuse a clip inside one video while
an unused one still scores. Nothing looks more automated than the same stock
shot appearing three times in twenty seconds.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import get_settings
from ..media.ffmpeg import FFmpegError, probe
from ..models import ClipAsset

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

_STOP = {
    "the", "and", "for", "with", "this", "that", "shot", "video", "vertical",
    "no", "text", "letters", "logos", "watermark", "clean", "modern", "soft",
    "natural", "light", "shallow", "depth", "field", "muted", "colour", "color",
    "grade", "composition", "background", "of", "a", "an", "in", "on",
}


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z]{3,}|[぀-ヿ一-鿿]{2,}", text.lower())
    return [t for t in tokens if t not in _STOP]


@dataclass(slots=True)
class Match:
    clip: ClipAsset
    score: float
    reason: str


class FootageLibrary:
    """Indexes a directory of clips and matches them to shots."""

    def __init__(self, session, settings=None):
        self.session = session
        self.settings = settings or get_settings()

    # ---------- indexing ----------

    def index(self, directory: str | Path | None = None) -> list[ClipAsset]:
        directory = Path(directory or self.settings.footage_dir or "")
        if not directory or not directory.is_dir():
            raise FileNotFoundError(f"footage directory not found: {directory}")

        found: list[ClipAsset] = []
        for path in sorted(directory.rglob("*")):
            if path.suffix.lower() not in VIDEO_SUFFIXES or not path.is_file():
                continue
            asset = self._upsert(path)
            if asset:
                found.append(asset)
        self.session.flush()
        return found

    def _upsert(self, path: Path) -> ClipAsset | None:
        resolved = str(path.resolve())
        asset = self.session.query(ClipAsset).filter_by(path=resolved).one_or_none()
        try:
            info = probe(path)
        except FFmpegError as exc:
            log.warning("skipping unreadable clip %s: %s", path.name, exc)
            return None

        # A sidecar JSON lets a librarian add keywords the filename cannot carry.
        sidecar = path.with_suffix(".json")
        meta, keywords, label = {}, [], None
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                keywords = [str(k).lower() for k in meta.get("keywords", [])]
                label = meta.get("label")
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("bad sidecar %s: %s", sidecar.name, exc)

        keywords = sorted(set(keywords) | set(tokenize(path.stem.replace("_", " ").replace("-", " "))))

        if asset is None:
            asset = ClipAsset(path=resolved)
            self.session.add(asset)
        asset.label = label or path.stem
        asset.keywords = keywords
        asset.duration_sec = info["duration"]
        asset.width = info["width"]
        asset.height = info["height"]
        asset.has_audio = info["has_audio"]
        asset.source = "local"
        asset.meta = meta
        return asset

    # ---------- matching ----------

    def candidates(self) -> list[ClipAsset]:
        return list(self.session.query(ClipAsset).all())

    def match(
        self,
        text: str,
        duration: float,
        prefer_vertical: bool = True,
        exclude_ids: set[int] | None = None,
        pool: list[ClipAsset] | None = None,
        avoid_id: int | None = None,
    ) -> Match | None:
        pool = pool if pool is not None else self.candidates()
        exclude_ids = exclude_ids or set()
        available = [c for c in pool if c.id not in exclude_ids]
        if not available:
            # The library is smaller than the storyboard, so reuse is required.
            # Still refuse the clip on the previous cut: a repeat across a cut
            # reads as a glitch, while a repeat a few shots apart does not.
            available = [c for c in pool if c.id != avoid_id] or list(pool)
        if not available:
            return None

        wanted = set(tokenize(text))
        best: Match | None = None
        for clip in available:
            overlap = wanted & set(clip.keywords or [])
            score = len(overlap) * 2.0

            if prefer_vertical:
                score += 1.5 if clip.is_vertical else -0.5

            # A clip shorter than the shot has to be looped or the cut runs dry.
            if clip.duration_sec >= duration:
                slack = clip.duration_sec - duration
                score += 1.0 if slack <= 6.0 else 0.4
            else:
                score -= 1.5

            reason = (
                f"keywords {sorted(overlap)}" if overlap else "no keyword overlap"
            )
            if best is None or score > best.score:
                best = Match(clip=clip, score=score, reason=reason)
        return best


class StockProvider:
    """Generic keyword-search stock footage API."""

    def __init__(self, endpoint: str, api_key: str | None = None,
                 client: httpx.Client | None = None):
        self.endpoint = endpoint
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=60.0)

    def search(self, query: str, limit: int = 10) -> list[dict]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = self._client.get(
            self.endpoint, params={"query": query, "per_page": limit}, headers=headers
        )
        resp.raise_for_status()
        body = resp.json()
        rows = body.get("videos") or body.get("hits") or body.get("results") or []
        return rows if isinstance(rows, list) else []

    def download(self, url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, timeout=None) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
        return out_path


def build_stock_provider(settings=None) -> StockProvider | None:
    settings = settings or get_settings()
    if (settings.stock_provider or "none").lower() in ("none", "", "off"):
        return None
    if not settings.stock_endpoint:
        log.warning("STOCK_PROVIDER set but STOCK_ENDPOINT is unset")
        return None
    return StockProvider(settings.stock_endpoint, settings.stock_api_key)
