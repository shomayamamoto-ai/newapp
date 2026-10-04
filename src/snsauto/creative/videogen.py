"""Image -> video generation (visual mode ``animate``).

Every provider in this space - Runway, Kling, Veo, Luma and the rest - follows
the same async shape: POST a prompt (and usually a first frame), get a job id
back, poll until it reports done, download the result. So this is written
against that shape rather than against one vendor, and the field names are
configurable because that is the only part that actually differs.

Generation is slow (tens of seconds to minutes per shot) and metered per
second of output, so a failure here degrades to a Ken Burns still rather than
aborting the render: losing motion on one cut is much cheaper than losing the
video.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)

# Keys that providers commonly use, searched in order.
JOB_ID_KEYS = ("id", "job_id", "task_id", "uuid", "request_id", "generation_id")
STATUS_KEYS = ("status", "state", "task_status")
VIDEO_URL_KEYS = ("video_url", "url", "output_url", "result_url", "video", "output")
DONE = {"succeeded", "success", "completed", "complete", "done", "finished", "ready"}
FAILED = {"failed", "error", "cancelled", "canceled", "rejected"}


class VideoGenError(RuntimeError):
    pass


def _find(payload, keys: tuple[str, ...], want_str: bool = True):
    """Breadth-first search for the first matching key with a usable value."""
    queue = [payload]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and value:
                    return value
                if not want_str and value is not None:
                    return value
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


@dataclass(slots=True)
class VideoRequest:
    prompt: str
    duration: float
    image_path: str | None = None
    aspect_ratio: str = "9:16"
    extra: dict = field(default_factory=dict)


class HttpVideoProvider:
    """Provider-agnostic async image-to-video client."""

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        status_endpoint: str | None = None,
        poll_seconds: float = 6.0,
        timeout_seconds: float = 900.0,
        client: httpx.Client | None = None,
    ):
        self.endpoint = endpoint
        self.api_key = api_key
        # "{id}" is substituted; without a template we re-poll the submit URL.
        self.status_endpoint = status_endpoint
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.Client(timeout=120.0)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def generate(self, request: VideoRequest, out_path: Path) -> Path:
        payload = {
            "prompt": request.prompt,
            "duration": round(request.duration, 2),
            "aspect_ratio": request.aspect_ratio,
            **request.extra,
        }
        if request.image_path and Path(request.image_path).exists():
            payload["image"] = base64.b64encode(
                Path(request.image_path).read_bytes()
            ).decode()

        resp = self._client.post(self.endpoint, json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise VideoGenError(f"submit failed {resp.status_code}: {resp.text[:400]}")
        body = resp.json()

        # Some providers return the finished asset immediately.
        url = _find(body, VIDEO_URL_KEYS)
        if url and url.startswith(("http://", "https://")):
            return self._download(url, out_path)

        job_id = _find(body, JOB_ID_KEYS)
        if not job_id:
            raise VideoGenError(f"no job id in response: {list(body)[:8]}")
        return self._await_job(job_id, out_path)

    def _await_job(self, job_id: str, out_path: Path) -> Path:
        status_url = (
            self.status_endpoint.replace("{id}", job_id)
            if self.status_endpoint
            else f"{self.endpoint.rstrip('/')}/{job_id}"
        )
        deadline = time.monotonic() + self.timeout_seconds

        while time.monotonic() < deadline:
            time.sleep(self.poll_seconds)
            resp = self._client.get(status_url, headers=self._headers())
            if resp.status_code >= 400:
                raise VideoGenError(f"poll failed {resp.status_code}: {resp.text[:300]}")
            body = resp.json()

            state = (_find(body, STATUS_KEYS) or "").lower()
            if state in FAILED:
                raise VideoGenError(f"job {job_id} reported {state}: {str(body)[:300]}")

            url = _find(body, VIDEO_URL_KEYS)
            if url and url.startswith(("http://", "https://")):
                return self._download(url, out_path)
            if state in DONE and not url:
                raise VideoGenError(f"job {job_id} finished with no video URL")

        raise VideoGenError(
            f"job {job_id} did not finish within {self.timeout_seconds:.0f}s"
        )

    def _download(self, url: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, timeout=None) as resp:
            if resp.status_code >= 400:
                raise VideoGenError(f"download failed {resp.status_code}")
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
        if out_path.stat().st_size == 0:
            raise VideoGenError("downloaded an empty file")
        return out_path


def build_video_provider(settings=None) -> HttpVideoProvider | None:
    """Return a provider, or None when animation is not configured."""
    settings = settings or get_settings()
    name = (settings.videogen_provider or "none").lower()
    if name in ("none", "", "off"):
        return None
    if not settings.videogen_endpoint:
        log.warning("VIDEOGEN_PROVIDER=%s but VIDEOGEN_ENDPOINT is unset", name)
        return None
    return HttpVideoProvider(
        settings.videogen_endpoint,
        settings.videogen_api_key,
        settings.videogen_status_endpoint,
        settings.videogen_poll_seconds,
        settings.videogen_timeout_seconds,
    )
