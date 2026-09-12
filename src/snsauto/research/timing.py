"""When to post, answered from performance rather than from popularity.

The previous implementation counted the publication hour of every post in the
corpus and returned the three most common. That is the mode of when *people
post*, which is not the question. If everyone in a niche uploads at 20:00 and
those uploads do badly, counting them returns 20:00 as the best hour - the
busiest slot is often the worst one precisely because it is the busiest.

What is asked is which hours the posts that *worked* were published in. So
each hour is scored by the engagement of the posts in it, buckets with too few
posts are refused rather than ranked on one lucky video, and the result is
reported in the operator's own timezone: nobody schedules a Japanese account
against UTC hours.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from ..analytics.stats import median, percentile

# An hour holding one post tells you about that post. Three is the smallest
# number where a bucket's median means anything at all, and it matches the
# MIN_SAMPLE used for PDCA verdicts.
MIN_BUCKET = 3

DEFAULT_TZ = "Asia/Tokyo"

# Weekday-scale grouping, because a Tuesday 7am and a Sunday 7am audience are
# different audiences on every platform.
DAYPARTS = (
    (5, 9, "早朝 (5-9時)"),
    (9, 12, "午前 (9-12時)"),
    (12, 15, "昼 (12-15時)"),
    (15, 18, "夕方 (15-18時)"),
    (18, 21, "夜 (18-21時)"),
    (21, 24, "深夜前半 (21-24時)"),
    (0, 5, "深夜 (0-5時)"),
)

WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")


def resolve_zone(name: str | None):
    """The operator's timezone, falling back to JST rather than to UTC."""
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return timezone(timedelta(hours=9))


def _local(published_at, zone):
    if published_at is None:
        return None
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(zone)


def _daypart(hour: int) -> str:
    for start, end, label in DAYPARTS:
        if start <= hour < end:
            return label
    return DAYPARTS[-1][2]


def posting_time_analysis(
    records,
    engagement_of,
    timezone_name: str | None = None,
    min_bucket: int = MIN_BUCKET,
) -> dict:
    """Which hours and dayparts the well-performing posts were published in.

    ``engagement_of`` maps a record to its engagement rate, so this works on
    both competitor records and the project's own publications.
    """
    zone = resolve_zone(timezone_name)
    rows = []
    for record in records:
        local = _local(getattr(record, "published_at", None), zone)
        if local is None:
            continue
        rows.append({
            "hour": local.hour,
            "weekday": local.weekday(),
            "daypart": _daypart(local.hour),
            "engagement": engagement_of(record),
        })

    if not rows:
        return {"usable": False,
                "reason": "投稿日時が取得できた投稿がありません。",
                "timezone": str(zone)}

    overall = median([r["engagement"] for r in rows])

    hours = _score_buckets(rows, "hour", min_bucket, overall)
    dayparts = _score_buckets(rows, "daypart", min_bucket, overall)
    weekdays = _score_buckets(rows, "weekday", min_bucket, overall)

    # Hour buckets are the finest grain and the first to run out of posts. When
    # none of them qualify, dayparts usually still do, and saying "evening"
    # from 12 posts beats saying "20:00" from one.
    grain = "hour" if hours["qualified"] else ("daypart" if dayparts["qualified"] else None)

    return {
        "usable": bool(grain),
        "timezone": str(zone),
        "sample": len(rows),
        "median_engagement": overall,
        "grain": grain,
        "reason": None if grain else (
            f"どの時間帯も{min_bucket}本に届かないため、"
            "時間帯による差を判定できません。"
        ),
        "by_hour": hours,
        "by_daypart": dayparts,
        "by_weekday": _label_weekdays(weekdays),
        "best": (hours if grain == "hour" else dayparts)["best"] if grain else [],
        "note": (
            "投稿件数ではなく、その時間帯の投稿のエンゲージ率中央値で並べています。"
            "件数の多い時間帯が良い時間帯とは限りません。"
        ),
    }


def _score_buckets(rows, key: str, min_bucket: int, overall: float) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row["engagement"])

    scored, thin = [], []
    for bucket, values in grouped.items():
        entry = {
            "bucket": bucket,
            "posts": len(values),
            "median_engagement": round(median(values), 5),
            "p75_engagement": round(percentile(values, 0.75), 5),
        }
        if len(values) < min_bucket:
            thin.append(entry)
            continue
        entry["lift_vs_median"] = (
            round(entry["median_engagement"] / overall - 1, 3) if overall else None
        )
        # "Above the corpus median" is the bar. A bucket that merely exists is
        # not a recommendation.
        entry["above_median"] = entry["median_engagement"] > overall
        scored.append(entry)

    scored.sort(key=lambda e: e["median_engagement"], reverse=True)
    return {
        "qualified": [e for e in scored if e["above_median"]],
        "all": scored,
        "thin": sorted(thin, key=lambda e: e["posts"], reverse=True),
        "best": [e for e in scored if e["above_median"]][:3],
    }


def _label_weekdays(result: dict) -> dict:
    for key in ("qualified", "all", "thin", "best"):
        for entry in result.get(key, []):
            if isinstance(entry["bucket"], int) and 0 <= entry["bucket"] <= 6:
                entry["label"] = WEEKDAY_JA[entry["bucket"]]
    return result


def describe_best(analysis: dict) -> str:
    """One Japanese sentence, or an honest refusal."""
    if not analysis.get("usable"):
        return analysis.get("reason") or "時間帯を判定できません。"

    best = analysis["best"]
    if not best:
        return "どの時間帯も全体中央値を上回っておらず、時間帯による差は見られません。"

    zone = analysis["timezone"]
    parts = []
    for entry in best:
        bucket = entry["bucket"]
        name = f"{bucket}時" if analysis["grain"] == "hour" else str(bucket)
        lift = entry.get("lift_vs_median")
        parts.append(
            f"{name}（{entry['posts']}本・中央値比 {lift:+.0%}）" if lift is not None
            else f"{name}（{entry['posts']}本）"
        )
    return f"{zone} で {'、'.join(parts)} が上位です。"
