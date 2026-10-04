"""Telop (burned-in caption) generation as ASS subtitles.

ASS rather than SRT because short-form telop needs per-style control over
outline, shadow, alignment and safe-area margins - and ffmpeg can burn ASS in
one filter pass. Styles are tuned per platform because the UI chrome that
covers the frame differs: TikTok's right-hand action rail and bottom caption
block eat far more of the frame than YouTube's player does.
"""

from __future__ import annotations

import subprocess
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from ..models import Platform


# libass resolves family names through fontconfig. Asking for a family that is
# not installed silently falls back to a Latin-only face, which renders every
# Japanese glyph as a tofu box - so resolve to a family that genuinely exists.
CJK_FONT_CANDIDATES = (
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "Source Han Sans JP",
    "Hiragino Sans",
    "Yu Gothic",
    "Meiryo",
    "IPAexGothic",
    "IPAGothic",
    "TakaoGothic",
    "VL Gothic",
)


@lru_cache
def _installed_families() -> frozenset[str]:
    try:
        out = subprocess.run(
            ["fc-list", "--format", "%{family}\n"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    families = set()
    for line in out.stdout.splitlines():
        for name in line.split(","):
            if name.strip():
                families.add(name.strip().casefold())
    return frozenset(families)


@lru_cache
def resolve_cjk_font(preferred: str | None = None) -> str:
    """First installed CJK family, preferring ``preferred`` when present."""
    installed = _installed_families()
    for candidate in ((preferred,) if preferred else ()) + CJK_FONT_CANDIDATES:
        if candidate and candidate.casefold() in installed:
            return candidate
    return preferred or CJK_FONT_CANDIDATES[0]


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def display_width(text: str) -> int:
    """Width in half-width units; CJK glyphs count as 2."""
    return sum(2 if _is_wide(ch) else 1 for ch in text)


# Never start a line with these; never end one with these.
_NO_START = "、。，．！？」』）】〉》’”ゝゞぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮー"
_NO_END = "「『（【〈《‘“"


def wrap_text(text: str, max_width: int = 22) -> list[str]:
    """Wrap on display width, honouring CJK line-breaking prohibitions.

    Japanese has no spaces, so a space-based wrapper produces one giant line.
    """
    lines: list[str] = []
    for paragraph in text.replace("\r", "").split("\n"):
        if not paragraph.strip():
            continue
        current = ""
        for ch in paragraph:
            candidate = current + ch
            if current and display_width(candidate) > max_width:
                if ch in _NO_START:
                    # Punctuation may overflow rather than open the next line.
                    current = candidate
                    continue
                if current[-1] in _NO_END:
                    # An opening bracket moves down with the text it opens.
                    lines.append(current[:-1])
                    current = current[-1] + ch
                    continue
                lines.append(current)
                current = ch
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


@dataclass(slots=True)
class TelopStyle:
    font: str = "Noto Sans CJK JP"
    size: int = 72
    primary: str = "&H00FFFFFF"        # ASS colours are &HAABBGGRR
    outline_colour: str = "&H00000000"
    back_colour: str = "&H80000000"
    bold: bool = True
    outline: float = 5.0
    shadow: float = 2.0
    alignment: int = 2                  # numpad layout; 2 = bottom-centre
    margin_l: int = 60
    margin_r: int = 60
    margin_v: int = 320
    max_width: int = 18
    fade_ms: int = 120


# Safe areas differ per surface; these keep telop clear of platform UI.
PLATFORM_TELOP_STYLES: dict[Platform, TelopStyle] = {
    Platform.TIKTOK: TelopStyle(size=76, margin_v=420, margin_r=200, max_width=16),
    Platform.INSTAGRAM: TelopStyle(size=74, margin_v=380, margin_r=180, max_width=17),
    Platform.YOUTUBE: TelopStyle(size=70, margin_v=260, max_width=20),
    Platform.X: TelopStyle(size=66, margin_v=200, max_width=24, alignment=2),
}


@dataclass(slots=True)
class TelopCue:
    start: float
    end: float
    text: str
    style: str = "Default"


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def build_ass(
    cues: list[TelopCue],
    style: TelopStyle,
    width: int = 1080,
    height: int = 1920,
) -> str:
    """Render cues to a complete ASS document."""
    bold = -1 if style.bold else 0
    font = resolve_cjk_font(style.font)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{style.size},{style.primary},{style.primary},{style.outline_colour},{style.back_colour},{bold},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},{style.alignment},{style.margin_l},{style.margin_r},{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for cue in cues:
        if cue.end <= cue.start or not cue.text.strip():
            continue
        body = "\\N".join(_escape(part) for part in wrap_text(cue.text, style.max_width))
        fade = f"{{\\fad({style.fade_ms},{style.fade_ms})}}" if style.fade_ms else ""
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},"
            f"{cue.style},,0,0,0,,{fade}{body}"
        )
    return header + "\n".join(lines) + "\n"
