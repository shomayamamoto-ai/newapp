"""What the workspace is holding, and a way to clear it by hand.

Nothing here runs on its own. Deletion is always an explicit command, and it
previews before it acts, because the operator asked to keep that decision -
and because for an agency the files are client deliverables, not scratch.

What accumulates, in the order it usually gets large:

* ``research-media``  competitor videos fetched for telop analysis. Up to
                      200 MB each, and the only category that is purely a
                      cache: deleting it costs a re-fetch, nothing else.
* ``renders``         finished videos. These are the deliverable.
* ``assets``          generated stills and narration for each storyboard.
* ``reports``         HTML and PDF reports.
* ``public``          files exposed for Instagram's fetch-by-URL publishing.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

# Ordered by how safe they are to remove. `disposable` marks a category whose
# only cost of deletion is recomputation.
CATEGORIES = (
    ("research-media", "競合動画（解析用キャッシュ）", True),
    ("assets", "生成画像・音声（中間ファイル）", True),
    ("sub", "字幕の中間ファイル", True),
    ("reports", "レポート（HTML / PDF）", False),
    ("public", "公開用ファイル（Instagram投稿に使用）", False),
    ("renders", "完成動画", False),
)

DISPOSABLE = {name for name, _, throwaway in CATEGORIES if throwaway}


@dataclass
class Usage:
    category: str
    label: str
    disposable: bool
    files: int
    bytes: int
    oldest_days: float | None

    @property
    def megabytes(self) -> float:
        return round(self.bytes / 1048576, 1)


def _scan(path: Path) -> tuple[int, int, float | None]:
    if not path.exists():
        return 0, 0, None
    files = total = 0
    oldest = None
    now = time.time()
    for entry in path.rglob("*"):
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        files += 1
        total += stat.st_size
        age = (now - stat.st_mtime) / 86400
        oldest = age if oldest is None else max(oldest, age)
    return files, total, (round(oldest, 1) if oldest is not None else None)


def usage(workspace: Path | str) -> list[Usage]:
    root = Path(workspace)
    rows = []
    for name, label, disposable in CATEGORIES:
        files, total, oldest = _scan(root / name)
        rows.append(Usage(name, label, disposable, files, total, oldest))
    return sorted(rows, key=lambda r: r.bytes, reverse=True)


def total_bytes(workspace: Path | str) -> int:
    return sum(row.bytes for row in usage(workspace))


def free_bytes(workspace: Path | str) -> int | None:
    try:
        return shutil.disk_usage(Path(workspace)).free
    except OSError:
        return None


@dataclass
class Removal:
    path: Path
    bytes: int
    age_days: float


def plan_clean(
    workspace: Path | str,
    categories: list[str] | None = None,
    older_than_days: float = 0.0,
) -> list[Removal]:
    """What a clean would delete. Always computed before anything is removed.

    Only the disposable categories are eligible unless a category is named
    explicitly, so a bare ``clean`` can never take a finished video or a
    client's report.
    """
    root = Path(workspace)
    wanted = categories or sorted(DISPOSABLE)
    now = time.time()
    removals = []
    for name in wanted:
        folder = root / name
        if not folder.exists():
            continue
        for entry in folder.rglob("*"):
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            age = (now - stat.st_mtime) / 86400
            if age >= older_than_days:
                removals.append(Removal(entry, stat.st_size, round(age, 1)))
    return sorted(removals, key=lambda r: r.bytes, reverse=True)


def apply_clean(removals: list[Removal]) -> tuple[int, int]:
    """Delete the planned files. Returns (count, bytes) actually removed."""
    count = freed = 0
    for removal in removals:
        try:
            removal.path.unlink()
        except OSError:
            continue
        count += 1
        freed += removal.bytes
    return count, freed


def warning(workspace: Path | str, threshold_gb: float = 5.0) -> str | None:
    """A line for the UI when the disk is getting tight.

    Worth surfacing because the failure is not isolated: when the disk fills,
    rendering and publishing stop at the same time, and the first sign is a
    scheduled post that never went out.
    """
    free = free_bytes(workspace)
    if free is None:
        return None
    free_gb = free / 1073741824
    if free_gb >= threshold_gb:
        return None
    disposable_mb = sum(
        row.megabytes for row in usage(workspace) if row.disposable
    )
    return (
        f"ディスクの空きが {free_gb:.1f}GB です。"
        f"`snsauto workspace clean` で解放できるキャッシュが {disposable_mb:.0f}MB あります。"
        "空き容量が尽きると、動画生成と予約投稿が同時に止まります。"
    )
