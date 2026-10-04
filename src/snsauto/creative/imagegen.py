"""Image generation for storyboard shots.

Two providers ship:

* ``placeholder`` - renders a typographic card locally with Pillow. No network,
  no cost, no API key. This is not a toy: it lets you render the full video and
  judge pacing, telop legibility and beat timing before spending anything on
  image generation.
* ``http`` - a provider-agnostic POST to ``IMAGEGEN_ENDPOINT``. It accepts the
  two response shapes image APIs actually return (raw bytes, or JSON carrying
  base64 / a URL), so most services can be wired up with env vars alone.
"""

from __future__ import annotations

import base64
import colorsys
import hashlib
import logging
from pathlib import Path
from typing import Protocol

import httpx
from PIL import Image, ImageDraw, ImageFont

from ..config import get_settings

log = logging.getLogger(__name__)

FONT_CANDIDATES = [
    "/etc/alternatives/fonts-japanese-gothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


class ImageProvider(Protocol):
    def generate(self, prompt: str, out_path: Path, width: int, height: int) -> Path: ...


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


class PlaceholderProvider:
    """Deterministic gradient card - same prompt always yields the same image."""

    def generate(self, prompt: str, out_path: Path, width: int, height: int) -> Path:
        seed = int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16)
        hue = (seed % 360) / 360.0

        top = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue, 0.28, 0.55))
        bottom = tuple(int(c * 255) for c in colorsys.hls_to_rgb((hue + 0.08) % 1.0, 0.12, 0.6))

        img = Image.new("RGB", (width, height), top)
        draw = ImageDraw.Draw(img)
        for y in range(height):
            t = y / max(1, height - 1)
            draw.line(
                [(0, y), (width, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
            )

        accent = tuple(int(c * 255) for c in colorsys.hls_to_rgb((hue + 0.5) % 1.0, 0.55, 0.6))
        radius = int(width * 0.34)
        cx, cy = width // 2, int(height * 0.38)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     outline=accent, width=max(3, width // 180))

        font = _load_font(max(18, width // 34))
        words = prompt.split()
        lines, current = [], ""
        for word in words[:26]:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=font) > width * 0.78 and current:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)

        y = int(height * 0.72)
        for line in lines[:5]:
            w = draw.textlength(line, font=font)
            draw.text(((width - w) / 2, y), line, fill=(245, 245, 245), font=font)
            y += int(font.size * 1.35)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        return out_path


class HttpProvider:
    """Generic HTTP image provider configured entirely by environment."""

    def __init__(self, endpoint: str, api_key: str | None = None, timeout: float = 180.0):
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout

    def generate(self, prompt: str, out_path: Path, width: int, height: int) -> Path:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        resp = httpx.post(
            self.endpoint,
            json={"prompt": prompt, "width": width, "height": height},
            headers=headers,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if resp.headers.get("content-type", "").startswith("image/"):
            out_path.write_bytes(resp.content)
            return out_path

        payload = resp.json()
        blob = _find_first(payload, ("b64_json", "base64", "image_base64", "data"))
        if blob:
            out_path.write_bytes(base64.b64decode(blob))
            return out_path

        url = _find_first(payload, ("url", "image_url", "output_url"))
        if url:
            img = httpx.get(url, timeout=self.timeout)
            img.raise_for_status()
            out_path.write_bytes(img.content)
            return out_path

        raise RuntimeError(f"unrecognised image response shape: {list(payload)[:6]}")


def _find_first(payload, keys: tuple[str, ...]):
    """Depth-first search for the first matching key holding a string."""
    stack = [payload]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, str) and value:
                    return value
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


class ImageGenerator:
    """Generates one image per storyboard shot and records the path."""

    def __init__(self, provider: ImageProvider | None = None, settings=None):
        self.settings = settings or get_settings()
        self.provider = provider or self._build_provider()

    def _build_provider(self) -> ImageProvider:
        name = (self.settings.imagegen_provider or "placeholder").lower()
        if name == "http" and self.settings.imagegen_endpoint:
            return HttpProvider(self.settings.imagegen_endpoint, self.settings.imagegen_api_key)
        if name not in ("placeholder", "http"):
            log.warning("unknown IMAGEGEN_PROVIDER %r - using placeholder", name)
        return PlaceholderProvider()

    def render_storyboard(
        self, storyboard, out_dir: str | Path, width: int = 1080, height: int = 1920
    ) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for shot in storyboard.shots:
            dest = out_dir / f"shot_{shot.index:03d}.png"
            prompt = shot.visual_prompt or shot.narration or "abstract background"
            try:
                self.provider.generate(prompt, dest, width, height)
            except Exception as exc:
                # One failed image must not lose the whole board - fall back so
                # the render still completes with a visible gap to fix.
                log.error("image generation failed for shot %s: %s", shot.index, exc)
                PlaceholderProvider().generate(prompt, dest, width, height)
            shot.image_path = str(dest)
            paths.append(dest)
        return paths
