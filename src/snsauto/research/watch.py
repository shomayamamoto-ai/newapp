"""Watching named competitors over time, and reading the difference.

A keyword run answers "what is working for this topic", and answers it fresh
every time - each run is an independent snapshot with nothing to compare
against. Two things follow from that, and this module addresses both:

* **Who** - a watched account is a competitor you check repeatedly, so the
  question becomes "what did they change" rather than "who is out there".
* **When** - two runs of the same target become a trend once something
  subtracts them. ``diff_runs`` is that subtraction.

Instagram's business_discovery and YouTube's channels.list both return a
follower count, which keyword search never does. That makes ``per_follower``
meaningful for watched accounts even though it stays unavailable for keyword
corpora - so it is reported only where it is real.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

from sqlalchemy import func, select

from ..models import CompetitorAccount, Platform, Project, ResearchRun
from ..platforms import PlatformError, get_adapter
from .keyword import ExclusionRules

log = logging.getLogger(__name__)

# A change smaller than this is noise on social metrics, not a movement worth
# putting in front of someone.
MATERIAL_CHANGE = 0.15


class WatchService:
    """Registers competitors and sweeps them on a schedule."""

    def __init__(self, session, settings=None, research=None):
        self.session = session
        self.settings = settings
        self._research = research

    def research(self):
        if self._research is None:
            from .keyword import ResearchService

            self._research = ResearchService(self.session, self.settings)
        return self._research

    # ---------- registry ----------

    def add(
        self,
        project: Project,
        platform: Platform,
        handle: str,
        label: str | None = None,
        notes: str | None = None,
    ) -> CompetitorAccount:
        handle = handle.strip().lstrip("@")
        # Query rather than walk ``project.competitor_accounts``: that
        # collection is cached on the loaded Project and does not see a row
        # added earlier in this same session, so a second add() would build a
        # duplicate and hit the unique constraint.
        existing = self.session.scalars(
            select(CompetitorAccount).where(
                CompetitorAccount.project_id == project.id,
                CompetitorAccount.platform == platform,
                func.lower(CompetitorAccount.handle) == handle.lower(),
            )
        ).first()
        if existing is not None:
            existing.active = True
            if label:
                existing.label = label
            self.session.flush()
            return existing

        account = CompetitorAccount(
            project_id=project.id, platform=platform, handle=handle,
            label=label, notes=notes,
        )
        self.session.add(account)
        self.session.flush()
        return account

    def active(self, project: Project) -> list[CompetitorAccount]:
        return list(self.session.scalars(
            select(CompetitorAccount)
            .where(
                CompetitorAccount.project_id == project.id,
                CompetitorAccount.active.is_(True),
            )
            .order_by(CompetitorAccount.id)
        ))

    # ---------- sweeping ----------

    def sweep(self, account: CompetitorAccount, limit: int = 25) -> ResearchRun:
        """Collect this competitor's recent posts as a run."""
        adapter = get_adapter(account.platform, settings=self.settings)
        profile, records = adapter.fetch_account(account.handle, limit=limit)

        if profile.external_id:
            account.external_id = profile.external_id
        if profile.name and not account.label:
            account.label = profile.name
        account.last_checked_at = datetime.now(timezone.utc)

        run = self.research().run(
            account.project,
            keyword=f"@{account.handle}",
            platform=account.platform,
            limit=limit,
            records=records,
            source="watch",
            account=account,
            # Exclusions are for cleaning a keyword corpus. Here every post is
            # by definition the account we chose to watch, so filtering it
            # would only ever delete the thing we asked for.
            exclusions=ExclusionRules(),
        )
        run.notes = (
            f"followers={profile.followers}" if profile.followers is not None else None
        )
        run.filters = {
            **(run.filters or {}),
            "watch": {
                "handle": account.handle,
                "followers": profile.followers,
                "post_count": profile.post_count,
                "name": profile.name,
            },
        }
        self.session.flush()
        return run

    def sweep_all(self, project: Project, limit: int = 25) -> dict:
        """Sweep every active competitor. One failure never stops the rest."""
        done, failed = [], {}
        for account in self.active(project):
            try:
                done.append(self.sweep(account, limit))
            except (PlatformError, Exception) as exc:
                log.warning("watch sweep failed for %s: %s", account.handle, exc)
                failed[account.display] = f"{type(exc).__name__}: {exc}"
        return {"runs": done, "failed": failed}

    # ---------- history ----------

    def previous_run(self, run: ResearchRun) -> ResearchRun | None:
        """The run before this one for the same target."""
        return self.session.scalars(
            select(ResearchRun)
            .where(
                ResearchRun.project_id == run.project_id,
                ResearchRun.id < run.id,
                ResearchRun.platform == run.platform,
                ResearchRun.keyword == run.keyword,
                ResearchRun.account_id.is_(run.account_id)
                if run.account_id is None
                else ResearchRun.account_id == run.account_id,
            )
            .order_by(ResearchRun.id.desc())
        ).first()


# ---------------------------------------------------------------- trends


def _run_stats(run: ResearchRun) -> dict:
    posts = list(run.posts)
    if not posts:
        return {"count": 0}
    engagements = [p.engagement_rate for p in posts]
    views = [p.views for p in posts if p.views]
    durations = [p.duration_sec for p in posts if p.duration_sec]
    followers = ((run.filters or {}).get("watch") or {}).get("followers")
    return {
        "count": len(posts),
        "median_engagement": statistics.median(engagements),
        "median_views": statistics.median(views) if views else 0,
        "median_duration": statistics.median(durations) if durations else None,
        "followers": followers,
        "ids": {p.external_id for p in posts},
        "hook_mix": _hook_mix(posts),
    }


def _hook_mix(posts) -> dict:
    from collections import Counter

    counts = Counter(
        p.structure.hook_type for p in posts if p.structure and p.structure.hook_type
    )
    total = sum(counts.values())
    return {k: round(v / total, 3) for k, v in counts.items()} if total else {}


def _delta(now, before) -> dict | None:
    """Relative change, with a flag for whether it clears the noise floor."""
    if before in (None, 0) or now is None:
        return None
    change = (now - before) / abs(before)
    return {
        "before": round(before, 4) if isinstance(before, float) else before,
        "after": round(now, 4) if isinstance(now, float) else now,
        "change": round(change, 3),
        "material": abs(change) >= MATERIAL_CHANGE,
    }


def diff_runs(previous: ResearchRun, current: ResearchRun) -> dict:
    """What moved between two sweeps of the same target.

    Reports movement, never a cause. A competitor's engagement halving between
    two sweeps is a fact; why it halved is not in this data, and the report
    says so rather than inventing a reason.
    """
    before, after = _run_stats(previous), _run_stats(current)
    if not before.get("count") or not after.get("count"):
        return {"comparable": False,
                "reason": "片方のスイープに投稿がないため比較できません"}

    new_ids = after["ids"] - before["ids"]
    new_posts = [p for p in current.posts if p.external_id in new_ids]

    days = max(
        0.5,
        (current.created_at - previous.created_at).total_seconds() / 86400
        if current.created_at and previous.created_at else 1.0,
    )

    result = {
        "comparable": True,
        "days_between": round(days, 1),
        "new_posts": len(new_posts),
        # Extrapolating a weekly rate from a few hours is arithmetic, not
        # information: two sweeps ten minutes apart would claim "28 posts a
        # week" from two posts. Below a day the rate is simply not knowable.
        "posts_per_week": (
            round(len(new_posts) / days * 7, 1) if days >= 1.0 else None
        ),
        "engagement": _delta(after["median_engagement"], before["median_engagement"]),
        "views": _delta(after["median_views"], before["median_views"]),
        "duration": _delta(after["median_duration"], before["median_duration"]),
        "followers": _delta(after["followers"], before["followers"]),
        "hook_shift": _hook_shift(before["hook_mix"], after["hook_mix"]),
        # Measured over the interval between the two sweeps, not averaged over
        # each post's lifetime.
        "velocity": observed_velocity(previous, current)[:5],
        "top_new": [
            {"title": p.title or (p.caption or "")[:80], "url": p.url,
             "views": p.views, "engagement_rate": round(p.engagement_rate, 4)}
            for p in sorted(new_posts, key=lambda p: p.score, reverse=True)[:5]
        ],
    }
    result["headline"] = _headline(result)
    return result


def _hook_shift(before: dict, after: dict) -> list[dict]:
    """Hook archetypes whose share moved. Needs structure analysis on both."""
    shifts = []
    for hook in set(before) | set(after):
        was, now = before.get(hook, 0.0), after.get(hook, 0.0)
        if abs(now - was) >= 0.1:
            shifts.append({"hook": hook, "before": was, "after": now,
                           "change": round(now - was, 3)})
    return sorted(shifts, key=lambda s: abs(s["change"]), reverse=True)


def _headline(diff: dict) -> str:
    """One Japanese sentence. Silent when nothing cleared the noise floor."""
    moves = []
    engagement = diff.get("engagement")
    if engagement and engagement["material"]:
        direction = "上昇" if engagement["change"] > 0 else "低下"
        moves.append(f"エンゲージ率が{abs(engagement['change']):.0%}{direction}")
    followers = diff.get("followers")
    if followers and followers["material"]:
        direction = "増加" if followers["change"] > 0 else "減少"
        moves.append(f"フォロワーが{abs(followers['change']):.0%}{direction}")
    if diff.get("new_posts"):
        rate = diff.get("posts_per_week")
        moves.append(
            f"新規{diff['new_posts']}本（週{rate}本ペース）" if rate
            else f"新規{diff['new_posts']}本"
        )
    for shift in diff.get("hook_shift") or []:
        moves.append(
            f"フックが{shift['hook']}型に{shift['change']:+.0%}シフト"
        )
        break
    if not moves:
        return "前回から有意な変化はありません。"
    return "、".join(moves) + "。"


def observed_velocity(previous: ResearchRun, current: ResearchRun) -> list[dict]:
    """Real view velocity, from two observations of the same posts.

    Lifetime views-per-day averages a post's whole history and so cannot tell
    a video that is climbing now from one that climbed a year ago. Two sweeps
    give the actual delta over an actual interval, which is the number that
    says which competitor post is moving *today*.

    Only posts present in both sweeps qualify - a post seen once has no
    interval to divide by.
    """
    if not previous.created_at or not current.created_at:
        return []
    hours = (current.created_at - previous.created_at).total_seconds() / 3600.0
    if hours < 1.0:
        # Under an hour the delta is mostly rounding in the platform's own
        # counters, and dividing by it inflates everything.
        return []

    before = {p.external_id: p for p in previous.posts}
    rows = []
    for post in current.posts:
        earlier = before.get(post.external_id)
        if earlier is None:
            continue
        delta_views = post.views - earlier.views
        delta_interactions = (
            (post.likes + post.comments + post.shares)
            - (earlier.likes + earlier.comments + earlier.shares)
        )
        rows.append({
            "external_id": post.external_id,
            "title": post.title or (post.caption or "")[:80],
            "url": post.url,
            "views_per_day": round(delta_views / hours * 24, 1),
            "interactions_per_day": round(delta_interactions / hours * 24, 1),
            "delta_views": delta_views,
            "window_hours": round(hours, 1),
            # Interactions per new view over the window: whether the post is
            # still engaging the people it is newly reaching, or just being
            # pushed at a colder audience.
            "fresh_engagement": (
                round(delta_interactions / delta_views, 5) if delta_views > 0 else None
            ),
        })
    rows.sort(key=lambda r: r["views_per_day"], reverse=True)
    return rows


def still_climbing(velocity_rows: list[dict], lifetime_lookup=None) -> list[dict]:
    """Posts whose current rate beats their own lifetime average.

    This is the "trending now" signal a single snapshot cannot produce.
    """
    out = []
    for row in velocity_rows:
        lifetime = (lifetime_lookup or {}).get(row["external_id"])
        if not lifetime:
            continue
        if row["views_per_day"] > lifetime * 1.2:
            out.append({**row, "lifetime_per_day": round(lifetime, 1),
                        "acceleration": round(row["views_per_day"] / lifetime, 2)})
    return out


def per_follower_engagement(run: ResearchRun) -> float | None:
    """Interactions per follower - only where a follower count actually exists.

    Returns None rather than a placeholder for keyword runs, because a number
    that silently means something different from run to run is worse than an
    absent one.
    """
    followers = ((run.filters or {}).get("watch") or {}).get("followers")
    if not followers:
        return None
    posts = list(run.posts)
    if not posts:
        return None
    interactions = statistics.fmean(
        [p.likes + p.comments + p.shares for p in posts]
    )
    return interactions / followers
