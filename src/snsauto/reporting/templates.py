"""HTML/CSS template engine.

Templates resolve user-first: anything in ``<workspace>/templates`` shadows the
built-in of the same name, so a brand can restyle every report by dropping one
file in without touching the package. ``snsauto template eject`` copies a
built-in out to that directory as a starting point.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

from ..config import get_settings

BUILTIN_DIR = Path(__file__).parent / "templates"


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_dur(seconds) -> str:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "-"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}" if m else f"{s}s"


def _fmt_dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value or "-")


class TemplateRegistry:
    def __init__(self, user_dir: Path | None = None, settings=None):
        settings = settings or get_settings()
        self.user_dir = Path(user_dir) if user_dir else settings.workspace / "templates"
        self.env = Environment(
            loader=ChoiceLoader([
                FileSystemLoader(str(self.user_dir)),   # user overrides win
                FileSystemLoader(str(BUILTIN_DIR)),
            ]),
            autoescape=select_autoescape(["html", "xml", "j2"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters.update(
            int_=_fmt_int, pct=_fmt_pct, dur=_fmt_dur, dt=_fmt_dt
        )

    def render(self, name: str, **context) -> str:
        context.setdefault("generated_at", datetime.now(timezone.utc))
        return self.env.get_template(name).render(**context)

    def names(self) -> list[str]:
        seen = {p.name for p in BUILTIN_DIR.glob("*.j2")}
        if self.user_dir.exists():
            seen |= {p.name for p in self.user_dir.glob("*.j2")}
        return sorted(seen)

    def eject(self, name: str) -> Path:
        """Copy a built-in template into the user directory for editing."""
        source = BUILTIN_DIR / name
        if not source.exists():
            raise FileNotFoundError(f"no built-in template named {name!r}")
        self.user_dir.mkdir(parents=True, exist_ok=True)
        dest = self.user_dir / name
        shutil.copy(source, dest)
        return dest

    def is_overridden(self, name: str) -> bool:
        return (self.user_dir / name).exists()


def render_template(name: str, **context) -> str:
    return TemplateRegistry().render(name, **context)


def list_templates() -> list[dict]:
    registry = TemplateRegistry()
    return [
        {"name": n, "overridden": registry.is_overridden(n)} for n in registry.names()
    ]
