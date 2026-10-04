"""Unified visual sourcing: one entry point over the three modes.

    still    generated image + Ken Burns move          (cheap, always available)
    animate  image -> video model, the shot moves      (closest to "映像生成")
    footage  real or stock footage matched to the shot (no generative cost)
    auto     the best mode each shot can actually have, given what is configured

Degradation is deliberate and one-directional: animate falls back to the still
it was going to animate, footage falls back to a still when nothing matches.
Every shot therefore ends up with *something*, and what actually happened is
recorded per shot so the render is never silently worse than you think.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings
from ..models import Storyboard, VisualMode
from .footage import FootageLibrary, build_stock_provider
from .imagegen import ImageGenerator
from .videogen import VideoGenError, build_video_provider

log = logging.getLogger(__name__)


@dataclass
class ShotVisual:
    shot_index: int
    mode: VisualMode
    image_path: str | None = None
    clip_path: str | None = None
    note: str = ""
    degraded_from: VisualMode | None = None


@dataclass
class VisualResult:
    visuals: list[ShotVisual] = field(default_factory=list)
    requested_mode: str = "still"

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.visuals:
            out[v.mode.value] = out.get(v.mode.value, 0) + 1
        return out

    @property
    def degraded(self) -> list[ShotVisual]:
        return [v for v in self.visuals if v.degraded_from]

    def summary(self) -> dict:
        return {
            "requested_mode": self.requested_mode,
            "produced": self.counts,
            "degraded": [
                {"shot": v.shot_index, "from": v.degraded_from.value,
                 "to": v.mode.value, "why": v.note}
                for v in self.degraded
            ],
        }


class VisualSourcer:
    """Produces the picture for every shot in a storyboard."""

    def __init__(self, session, settings=None, images=None, video=None, library=None):
        self.session = session
        self.settings = settings or get_settings()
        self.images = images or ImageGenerator(settings=self.settings)
        self.video = video if video is not None else build_video_provider(self.settings)
        self.library = library or FootageLibrary(session, self.settings)
        self.stock = build_stock_provider(self.settings)

    def available_modes(self) -> set[VisualMode]:
        """Modes this installation can actually deliver right now."""
        modes = {VisualMode.STILL}  # always: the placeholder provider is local
        if self.video is not None:
            modes.add(VisualMode.ANIMATE)
        if self.library.candidates() or self.stock is not None:
            modes.add(VisualMode.FOOTAGE)
        return modes

    def resolve_mode(self, requested: str | VisualMode | None = None) -> VisualMode:
        requested = str(requested or self.settings.visual_mode or "still").lower()
        available = self.available_modes()
        if requested == "auto":
            # Best effort, most cinematic first.
            for mode in (VisualMode.ANIMATE, VisualMode.FOOTAGE, VisualMode.STILL):
                if mode in available:
                    return mode
            return VisualMode.STILL
        try:
            mode = VisualMode(requested)
        except ValueError:
            log.warning("unknown visual mode %r - using still", requested)
            return VisualMode.STILL
        if mode not in available:
            log.warning("visual mode %s is not configured - using still", mode.value)
            return VisualMode.STILL
        return mode

    def produce(
        self,
        storyboard: Storyboard,
        out_dir: str | Path,
        width: int = 1080,
        height: int = 1920,
        mode: str | VisualMode | None = None,
    ) -> VisualResult:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        chosen = self.resolve_mode(mode)
        result = VisualResult(requested_mode=str(mode or self.settings.visual_mode))

        # Stills are the substrate for animate and the fallback everywhere, so
        # they are always produced first.
        self.images.render_storyboard(storyboard, out_dir, width, height)
        self.session.flush()

        if chosen is VisualMode.ANIMATE:
            result.visuals = self._animate(storyboard, out_dir)
        elif chosen is VisualMode.FOOTAGE:
            result.visuals = self._footage(storyboard, height > width)
        else:
            result.visuals = [
                ShotVisual(s.index, VisualMode.STILL, image_path=s.image_path,
                           note="generated still + Ken Burns")
                for s in storyboard.shots
            ]

        self.session.flush()
        return result

    # ---------- per-mode ----------

    def _animate(self, storyboard: Storyboard, out_dir: Path) -> list[ShotVisual]:
        from .videogen import VideoRequest

        visuals = []
        for shot in storyboard.shots:
            dest = out_dir / f"shot_{shot.index:03d}.mp4"
            request = VideoRequest(
                prompt=shot.visual_prompt or shot.narration or "",
                duration=max(1.0, shot.duration or 3.0),
                image_path=shot.image_path,
                aspect_ratio=storyboard.aspect_ratio or "9:16",
                extra={"camera": shot.camera} if shot.camera else {},
            )
            try:
                self.video.generate(request, dest)
                shot.clip_path = str(dest)
                visuals.append(ShotVisual(shot.index, VisualMode.ANIMATE,
                                          image_path=shot.image_path,
                                          clip_path=str(dest), note="generated clip"))
            except (VideoGenError, OSError) as exc:
                # One failed cut must not cost the whole render.
                log.error("animate failed on shot %s: %s", shot.index, exc)
                shot.clip_path = None
                visuals.append(ShotVisual(
                    shot.index, VisualMode.STILL, image_path=shot.image_path,
                    note=str(exc)[:200], degraded_from=VisualMode.ANIMATE,
                ))
        return visuals

    def _footage(self, storyboard: Storyboard, prefer_vertical: bool) -> list[ShotVisual]:
        pool = self.library.candidates()
        used: set[int] = set()
        previous_id: int | None = None
        visuals = []

        for shot in storyboard.shots:
            text = " ".join(filter(None, [shot.visual_prompt, shot.narration, shot.telop]))
            match = self.library.match(
                text, shot.duration or 3.0, prefer_vertical, used, pool,
                avoid_id=previous_id,
            ) if pool else None

            if match is None:
                shot.clip_path = None
                visuals.append(ShotVisual(
                    shot.index, VisualMode.STILL, image_path=shot.image_path,
                    note="no footage matched", degraded_from=VisualMode.FOOTAGE,
                ))
                continue

            used.add(match.clip.id)
            previous_id = match.clip.id
            shot.clip_path = match.clip.path
            visuals.append(ShotVisual(
                shot.index, VisualMode.FOOTAGE, image_path=shot.image_path,
                clip_path=match.clip.path,
                note=f"{Path(match.clip.path).name} ({match.reason}, score {match.score:.1f})",
            ))
        return visuals
