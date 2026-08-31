"""Storyboard (絵コンテ) generation from a script."""

from __future__ import annotations

from ..models import Script, Shot, Storyboard

STYLE_DEFAULT = (
    "clean modern editorial photography, soft natural light, shallow depth of "
    "field, muted colour grade, vertical composition"
)


def _fallback_storyboard(script: Script, style_hint: str | None) -> dict:
    style = style_hint or STYLE_DEFAULT
    shots = []
    for line in script.lines or []:
        shots.append({
            "index": line.get("index", len(shots)),
            "start": line.get("start", 0.0),
            "end": line.get("end", 0.0),
            "narration": line.get("narration", ""),
            "telop": line.get("telop", ""),
            # Keep the style string identical across shots so the sequence
            # reads as one video rather than a collage.
            "visual_prompt": (
                f"{line.get('visual') or line.get('narration') or script.title}. "
                f"{style}. No text, no letters, no logos, no watermark."
            ),
            "camera": "medium shot, slow push in" if shots else "close-up, static",
            "transition": "cut",
        })
    return {"style": style, "shots": shots}


class StoryboardService:
    def __init__(self, session, llm=None):
        self.session = session
        self.llm = llm

    def generate(
        self,
        script: Script,
        aspect_ratio: str = "9:16",
        style_hint: str | None = None,
    ) -> Storyboard:
        if self.llm is not None:
            data = self.llm.draw_storyboard(
                script={
                    "title": script.title,
                    "hook": script.hook,
                    "body": script.body,
                    "cta": script.cta,
                    "lines": script.lines,
                },
                aspect_ratio=aspect_ratio,
                style_hint=style_hint,
            )
        else:
            data = _fallback_storyboard(script, style_hint)

        board = Storyboard(
            script_id=script.id,
            aspect_ratio=aspect_ratio,
            style=data.get("style") or style_hint or STYLE_DEFAULT,
        )
        self.session.add(board)
        self.session.flush()

        for i, shot in enumerate(data.get("shots", [])):
            self.session.add(
                Shot(
                    storyboard_id=board.id,
                    index=shot.get("index", i),
                    start=float(shot.get("start", 0.0)),
                    end=float(shot.get("end", 0.0)),
                    narration=shot.get("narration"),
                    telop=shot.get("telop"),
                    visual_prompt=shot.get("visual_prompt"),
                    camera=shot.get("camera"),
                    transition=shot.get("transition", "cut"),
                )
            )
        self.session.flush()
        return board
