"""Reading a retention curve against what was on screen at the time.

A retention curve on its own says viewers left at 3.2 seconds. That is a fact
with nowhere to go. What makes it actionable is the other half, which this
codebase happens to already have: we *authored* the video, so the exact second
every telop card appears and every cut lands is stored on the storyboard, not
inferred from a finished file. Laying the curve over that timeline turns "they
left at 3.2s" into "they left one beat after the second telop card, which was
14 characters held for 1.1 seconds".

Two things about this worth being clear on.

**It is diagnosis before it is prediction.** With one video it says where that
video lost people. Saying which *features* cause drop-off needs many videos
varying in those features, and the aggregate below refuses to generalise until
there are enough. The mode is reported, always, because a prediction presented
as diagnosis and a diagnosis presented as prediction fail in opposite
directions.

**It is calibrated on this account's own audience.** Generic retention advice
is calibrated on somebody else's viewers. For an agency this is the whole
point: B社の視聴者は A社の視聴者ではない, and the drop-off that matters is the
one this client's audience actually produced.

Availability, unchanged from everything else: YouTube only, own videos only.
Instagram reports an average watch time but no curve; TikTok's Display API
reports neither.
"""

from __future__ import annotations

from dataclasses import dataclass

from .stats import median, reliability

# Below this many videos, "these features cause drop-off" is a story about
# noise. Matches the sample bands used for PDCA verdicts.
MIN_VIDEOS_FOR_PATTERN = 6

# A fall smaller than this across one step is the curve's normal texture, not
# a place where something happened.
MATERIAL_DROP = 0.05

# Short-form lives or dies here, so the opening is reported separately.
HOOK_WINDOW_SEC = 3.0


@dataclass(slots=True)
class Point:
    second: float
    watch_ratio: float
    relative: float | None = None


def to_seconds(curve: dict | None, duration_sec: float) -> list[Point]:
    """Turn the API's 0-1 progress ratios into seconds on our timeline."""
    if not curve or not duration_sec:
        return []
    points = [
        Point(
            second=round(row["elapsed_ratio"] * duration_sec, 2),
            watch_ratio=row["watch_ratio"],
            relative=row.get("relative"),
        )
        for row in curve.get("points") or []
        if row.get("elapsed_ratio") is not None
    ]
    return sorted(points, key=lambda p: p.second)


def drop_offs(points: list[Point], limit: int = 3) -> list[dict]:
    """Where the curve falls fastest. Steepest first."""
    falls = []
    for before, after in zip(points, points[1:]):
        delta = before.watch_ratio - after.watch_ratio
        if delta < MATERIAL_DROP:
            continue
        falls.append({
            "from_sec": before.second,
            "to_sec": after.second,
            "lost": round(delta, 4),
            "remaining": round(after.watch_ratio, 4),
        })
    falls.sort(key=lambda f: f["lost"], reverse=True)
    return falls[:limit]


def on_screen_at(second: float, shots) -> dict | None:
    """Which shot was showing, and what it was showing."""
    for shot in shots or []:
        if shot.start <= second < shot.end:
            return {
                "index": shot.index,
                "start": shot.start,
                "end": shot.end,
                "telop": shot.telop,
                "telop_chars": len((shot.telop or "").replace("\\n", "")),
                "hold_sec": round(shot.end - shot.start, 2),
                "narration": shot.narration,
                "transition": shot.transition,
            }
    return None


def hook_retention(points: list[Point], window: float = HOOK_WINDOW_SEC) -> float | None:
    """What share was still watching at the end of the opening.

    Interpolated rather than read off the last point inside the window. On a
    coarse curve - a short video, or a platform reporting few buckets - the
    only sample at or before 3 seconds can be the one at zero, where retention
    is 1.0 by definition. Returning that would report a perfect hook for every
    video, which is worse than reporting nothing.
    """
    if not points:
        return None
    if window <= points[0].second:
        return round(points[0].watch_ratio, 4)
    if window >= points[-1].second:
        return round(points[-1].watch_ratio, 4)

    for before, after in zip(points, points[1:]):
        if before.second <= window <= after.second:
            span = after.second - before.second
            if span <= 0:
                return round(before.watch_ratio, 4)
            weight = (window - before.second) / span
            value = before.watch_ratio + (after.watch_ratio - before.watch_ratio) * weight
            return round(value, 4)
    return round(points[-1].watch_ratio, 4)


def diagnose(curve: dict | None, duration_sec: float, shots=None) -> dict:
    """One video: where it lost people, and what was on screen there."""
    points = to_seconds(curve, duration_sec)
    if not points:
        return {
            "measured": False,
            "reason": (
                "視聴維持カーブが取得できていません。YouTube の自社動画のみ対象で、"
                "yt-analytics.readonly スコープが必要です。"
            ),
        }

    falls = drop_offs(points)
    for fall in falls:
        fall["on_screen"] = on_screen_at(fall["from_sec"], shots)

    relatives = [p.relative for p in points if p.relative is not None]
    return {
        "measured": True,
        "points": len(points),
        "duration_sec": duration_sec,
        "hook_retention": hook_retention(points),
        "hook_window_sec": HOOK_WINDOW_SEC,
        "end_retention": round(points[-1].watch_ratio, 4),
        # YouTube's own comparison against videos of similar length, so a curve
        # can be read as above or below par without us inventing a baseline.
        "relative_performance": round(median(relatives), 3) if relatives else None,
        "drop_offs": falls,
        "summary": describe(falls, hook_retention(points)),
    }


def describe(falls: list[dict], hook: float | None) -> str:
    """One Japanese sentence naming the worst moment and what was on it."""
    if hook is not None and hook < 0.5:
        opening = f"冒頭{HOOK_WINDOW_SEC:.0f}秒で{(1 - hook):.0%}が離脱しています。"
    elif hook is not None:
        opening = f"冒頭{HOOK_WINDOW_SEC:.0f}秒の残存は{hook:.0%}です。"
    else:
        opening = ""

    if not falls:
        return opening + "急な離脱点はありません。"

    worst = falls[0]
    shot = worst.get("on_screen")
    where = ""
    if shot:
        telop = (shot.get("telop") or "").replace("\n", " ")
        where = (
            f"そこは{shot['index'] + 1}カット目"
            + (f"（テロップ「{telop[:24]}」{shot['telop_chars']}字を"
               f"{shot['hold_sec']}秒表示）" if telop else "")
            + "です。"
        )
    return (
        opening
        + f"最大の離脱は {worst['from_sec']}秒 で、"
        + f"{worst['lost']:.0%}が離れています。" + where
    )


def aggregate(diagnoses: list[dict]) -> dict:
    """Across videos: where retention consistently falls.

    Refuses to call anything a pattern below the sample floor. One video with
    a drop at 3 seconds is that video; six videos dropping at 3 seconds is
    something about the format.
    """
    measured = [d for d in diagnoses if d.get("measured")]
    sample = len(measured)
    band = reliability(sample)

    if sample == 0:
        return {"usable": False, "sample": 0, "reliability": band,
                "reason": "維持カーブのある動画がまだありません。"}

    hooks = [d["hook_retention"] for d in measured if d.get("hook_retention") is not None]
    ends = [d["end_retention"] for d in measured if d.get("end_retention") is not None]

    # Bucket the worst drop of each video by second, so a repeated moment shows
    # up as a repeated bucket rather than as scattered timestamps.
    buckets: dict[int, list[dict]] = {}
    for entry in measured:
        for fall in entry.get("drop_offs") or []:
            buckets.setdefault(int(fall["from_sec"]), []).append(fall)

    recurring = sorted(
        (
            {
                "second": second,
                "videos": len(falls),
                "median_lost": round(median([f["lost"] for f in falls]), 4),
            }
            for second, falls in buckets.items()
            if len(falls) >= 2
        ),
        key=lambda row: (row["videos"], row["median_lost"]),
        reverse=True,
    )

    enough = sample >= MIN_VIDEOS_FOR_PATTERN
    return {
        "usable": True,
        "sample": sample,
        "reliability": band,
        # The distinction that decides how the numbers may be used.
        "mode": "pattern" if enough else "description",
        "median_hook_retention": round(median(hooks), 4) if hooks else None,
        "median_end_retention": round(median(ends), 4) if ends else None,
        "recurring_drops": recurring[:5],
        "note": (
            f"{sample}本の実測です。"
            if enough else
            f"{sample}本しかないため、個別の記述であって傾向ではありません。"
            f"{MIN_VIDEOS_FOR_PATTERN}本を超えると共通点として扱えます。"
        ),
    }


def compare_to_plan(diagnosis: dict, shots) -> list[str]:
    """Where the intended structure and the measured behaviour disagree.

    The storyboard says what each beat was for. The curve says whether it
    worked. This names the beats where the two do not match, which is the only
    output here that changes what the next script does.
    """
    if not diagnosis.get("measured"):
        return []

    notes = []
    hook = diagnosis.get("hook_retention")
    if hook is not None and hook < 0.6:
        first = next((s for s in shots or [] if s.index == 0), None)
        if first is not None:
            telop = (first.telop or "").replace("\n", " ")
            notes.append(
                f"フックが機能していません（冒頭{HOOK_WINDOW_SEC:.0f}秒で"
                f"{(1 - hook):.0%}離脱）。1カット目のテロップは「{telop[:28]}」で、"
                f"{round(first.end - first.start, 1)}秒表示されています。"
            )

    for fall in diagnosis.get("drop_offs") or []:
        shot = fall.get("on_screen")
        if not shot:
            continue
        if shot["telop_chars"] > 20 and shot["hold_sec"] < 2.0:
            notes.append(
                f"{fall['from_sec']}秒: テロップ{shot['telop_chars']}字を"
                f"{shot['hold_sec']}秒しか表示しておらず、読み切れない可能性があります。"
            )
        elif shot["hold_sec"] > 5.0:
            notes.append(
                f"{fall['from_sec']}秒: 同じ画が{shot['hold_sec']}秒続いています。"
            )
    return notes
