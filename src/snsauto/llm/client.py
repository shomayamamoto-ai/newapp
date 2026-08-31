"""Claude wrapper for every generative step in the pipeline.

Structured output is enforced with ``output_config.format`` (json_schema) so
callers get a validated dict rather than prose they have to parse. When no API
key is configured the client is simply absent and callers fall back to their
heuristic path - the pipeline degrades, it does not crash.
"""

from __future__ import annotations

import json
import logging

from ..config import get_settings
from .schemas import (
    IMPROVEMENT_SCHEMA,
    SCRIPT_SCHEMA,
    STORYBOARD_SCHEMA,
    STRUCTURE_SCHEMA,
)

log = logging.getLogger(__name__)

MAX_TOKENS = 16000

SYSTEM = """You are a senior short-form social video strategist and scriptwriter.

You write for TikTok, Instagram Reels, YouTube Shorts and X. You have studied
the retention patterns of vertical video: the first 2 seconds decide the view,
the first 5 decide the watch-through, and a single clear call to action beats
three competing ones.

Rules you always follow:
- Write in the same language as the brief. If the brief is Japanese, write
  Japanese, including telop.
- Telop is not narration. It is the 15-20 characters a muted viewer reads.
- Never invent statistics, prices, medical or legal claims. If the brief does
  not supply a number, describe the benefit qualitatively.
- Respect the brand profile's banned words and tone.
- Every beat must earn the next one. No filler."""


class LLMUnavailable(RuntimeError):
    """No API key configured, or the anthropic SDK is not installed."""


class LLMClient:
    """Thin, typed wrapper over the Messages API."""

    def __init__(self, api_key: str, model: str = "claude-opus-5"):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LLMUnavailable(
                "anthropic SDK not installed. `pip install 'snsauto[llm]'`"
            ) from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _json(self, prompt: str, schema: dict, effort: str = "high") -> dict:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": prompt}],
        )
        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise LLMUnavailable(f"request refused: {getattr(detail, 'category', None)}")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMUnavailable("model returned no text block")
        return json.loads(text)

    # ---------- generation steps ----------

    def write_script(
        self,
        *,
        keyword: str,
        platform: str,
        duration: float,
        brand_profile: dict,
        research: dict,
    ) -> dict:
        prompt = f"""Write a {duration:.0f}-second short-form video script.

# Topic
{keyword}

# Platform
{platform}

# Brand profile
{json.dumps(brand_profile, ensure_ascii=False, indent=2)}

# Competitor research (top performers for this keyword)
{json.dumps(research, ensure_ascii=False, indent=2)[:12000]}

# Requirements
- Total runtime must be exactly {duration:.0f} seconds.
- `lines` must tile 0 -> {duration:.0f} with no gaps and no overlaps.
- Each beat runs 1.5-4 seconds. Shorter beats near the hook, longer in the body.
- The hook must be readable as telop alone, with no audio.
- Base your structural choices on the research: match the winning duration
  band, hook archetype and pacing. Say which in `rationale`.
- 5-10 hashtags, mixing one broad, several mid-tail and one niche."""
        return self._json(prompt, SCRIPT_SCHEMA)

    def draw_storyboard(
        self, *, script: dict, aspect_ratio: str = "9:16", style_hint: str | None = None
    ) -> dict:
        prompt = f"""Turn this script into a shot-by-shot storyboard.

# Script
{json.dumps(script, ensure_ascii=False, indent=2)}

# Requirements
- Aspect ratio {aspect_ratio}. Compose for vertical: subject in the upper two
  thirds, because platform UI covers the bottom of the frame.
- One shot per script beat, preserving its start/end exactly.
- `visual_prompt` must be a complete English image-generation prompt that works
  with no other context: subject, setting, lighting, lens, mood.
- Every visual_prompt must repeat the same style keywords so the shots look
  like one video, not a collage.
- No text, letters, logos or watermarks in the images - telop is burned in
  later by the editor.
{f'- Style direction: {style_hint}' if style_hint else ''}"""
        return self._json(prompt, STORYBOARD_SCHEMA)

    def analyze_structure(
        self, *, title: str | None, caption: str | None, duration: float
    ) -> dict | None:
        prompt = f"""Break down this competing post's structure.

Title: {title or '(none)'}
Caption: {caption or '(none)'}
Duration: {duration:.0f}s

Infer the beat map from the text and the duration. `takeaways` must be
transferable rules another creator could apply, not a description of this post."""
        try:
            return self._json(prompt, STRUCTURE_SCHEMA, effort="medium")
        except (LLMUnavailable, json.JSONDecodeError) as exc:
            log.warning("structure analysis failed, using heuristics: %s", exc)
            return None

    def review_cycle(self, *, cycle: dict, metrics: dict, baseline: dict) -> dict:
        prompt = f"""Review this PDCA cycle and decide what to do next.

# Cycle
{json.dumps(cycle, ensure_ascii=False, indent=2)}

# Measured result
{json.dumps(metrics, ensure_ascii=False, indent=2)}

# Baseline (the project's prior average)
{json.dumps(baseline, ensure_ascii=False, indent=2)}

Judge against the stated target only. If the sample is too small to
distinguish from noise, return "inconclusive" and say what sample size is
needed - do not manufacture a conclusion."""
        return self._json(prompt, IMPROVEMENT_SCHEMA)


def build_client(settings=None) -> LLMClient | None:
    """Return a client, or None when unconfigured - callers fall back."""
    settings = settings or get_settings()
    if not settings.anthropic_api_key:
        return None
    try:
        return LLMClient(settings.anthropic_api_key, settings.llm_model)
    except LLMUnavailable as exc:
        log.warning("LLM disabled: %s", exc)
        return None
