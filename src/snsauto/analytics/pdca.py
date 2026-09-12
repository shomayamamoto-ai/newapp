"""PDCA cycle management.

A cycle is a hypothesis with a measurable target attached to a specific set of
publications. `check()` compares the result against both the stated target and
the project's own prior baseline.

Four things decide whether that comparison means anything, and all four are
enforced here rather than left to the reader:

* **Maturity.** A post measured three hours after publishing has not finished
  accumulating. Immature posts are excluded from both sides and counted, not
  silently averaged in - including them in the baseline drags it down and
  makes the next cycle look like a win.
* **Age matching.** Cumulative views favour whichever group has been live
  longer, which is almost always the baseline. Where the snapshot series
  allows it, both groups are read at the same age instead.
* **Like for like.** Engagement rate on YouTube is interactions over views; on
  Instagram there are no views to divide by. Averaging the two produces a
  number that is not a rate at all, so cycles compare within a platform.
* **Significance.** A mean that moved is not a result. Every verdict carries
  the smallest difference the sample could actually have resolved, and
  "improved" is only said when the interval excludes zero.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from sqlalchemy import select

from ..models import PdcaCycle, PdcaStage, Publication, PublicationStatus, utcnow
from .collect import MATURITY_HOURS, summarize_publication
from .stats import (
    bootstrap_difference,
    describe,
    detectable_relative_effect,
    median,
    percentile,
    relative_effect,
    reliability,
)

# Below this many posts, a difference in engagement rate is not distinguishable
# from normal variance on social platforms.
MIN_SAMPLE = 3

METRIC_FIELDS = (
    "views", "likes", "comments", "shares", "saves",
    "engagement_rate", "views_per_hour",
    # Retention is the metric that decides a short-form video, and it is only
    # present for posts whose platform reported it. A post without it
    # contributes nothing to these aggregates rather than contributing a zero.
    "retention_rate", "avg_watch_sec", "skip_rate",
)

# Metrics whose value keeps climbing with age. For these the age-matched
# reading is used when it exists, because the raw number mostly measures how
# long the post has been up.
CUMULATIVE_METRICS = {"views", "likes", "comments", "shares", "saves"}

METRIC_JA = {
    "views": "再生数", "likes": "いいね", "comments": "コメント数",
    "shares": "シェア", "saves": "保存", "engagement_rate": "エンゲージ率",
    "views_per_hour": "時間あたり再生数",
    "retention_rate": "視聴維持率", "avg_watch_sec": "平均視聴時間",
    "skip_rate": "スキップ率",
}

# Metrics where a smaller number is the better outcome.
LOWER_IS_BETTER = {"skip_rate"}


def _metric_value(row: dict, metric: str, age_matched: bool) -> float | None:
    """One post's value for a metric, age-matched where that is meaningful."""
    if age_matched and metric in CUMULATIVE_METRICS | {
        "engagement_rate", "retention_rate", "avg_watch_sec"
    }:
        at_age = row.get("at_24h")
        if at_age and metric in at_age:
            return at_age[metric]
        return None
    value = row.get(metric)
    return None if value is None else float(value)


def _collect(rows: list[dict], age_matched: bool) -> dict:
    """Turn per-post summaries into a comparable group."""
    group: dict = {
        "sample": len(rows),
        "reliability": reliability(len(rows)),
        "age_matched": age_matched,
        "values": {},
        "per_post": rows,
    }
    for field in METRIC_FIELDS:
        values = [
            v for v in (_metric_value(r, field, age_matched) for r in rows)
            if v is not None
        ]
        group["values"][field] = values
        if values:
            # Median leads. One post in twenty carries ten times the views of
            # the rest, and a mean of that describes the outlier.
            group[field] = round(median(values), 5)
            group[f"{field}_mean"] = round(statistics.fmean(values), 5)
            group[f"{field}_p25"] = round(percentile(values, 0.25), 5)
            group[f"{field}_p75"] = round(percentile(values, 0.75), 5)
    if rows:
        group["total_views"] = sum(r.get("views", 0) or 0 for r in rows)
        group["platforms"] = sorted({r["platform"] for r in rows})
    return group


def _eligible(
    publications, exclude: set[int], platform: str | None, min_age_hours: float
) -> tuple[list[dict], dict]:
    """Summaries that can legitimately enter a comparison, and why others could not."""
    kept, dropped = [], defaultdict(int)
    for publication in publications:
        if publication.id in exclude:
            continue
        row = summarize_publication(publication)
        if not row.get("has_data"):
            dropped["no_metrics"] += 1
            continue
        if row.get("age_hours", 0) < min_age_hours:
            # Still climbing. Including it understates whichever side it is on.
            dropped["too_recent"] += 1
            continue
        if platform and row["platform"] != platform:
            dropped["other_platform"] += 1
            continue
        kept.append(row)
    return kept, dict(dropped)


def project_baseline(
    session,
    project_id: int,
    exclude_ids: list[int] | None = None,
    platform: str | None = None,
    min_age_hours: float = MATURITY_HOURS,
    age_matched: bool = True,
) -> dict:
    """The project's prior performance, on posts old enough to have settled."""
    rows, dropped = _eligible(
        session.scalars(
            select(Publication).where(
                Publication.project_id == project_id,
                Publication.status == PublicationStatus.PUBLISHED,
            )
        ),
        set(exclude_ids or []),
        platform,
        min_age_hours,
    )
    if not rows:
        return {"sample": 0, "excluded": dropped, "platform": platform,
                "reliability": "insufficient"}

    group = _collect(rows, age_matched)
    group.update({"excluded": dropped, "platform": platform})
    group.pop("per_post", None)
    return group


def aggregate(
    session,
    publication_ids: list[int],
    platform: str | None = None,
    min_age_hours: float = MATURITY_HOURS,
    age_matched: bool = True,
) -> dict:
    rows, dropped = _eligible(
        session.scalars(
            select(Publication).where(Publication.id.in_(publication_ids or [-1]))
        ),
        set(),
        platform,
        min_age_hours,
    )
    if not rows:
        return {"sample": 0, "excluded": dropped, "reliability": "insufficient"}

    group = _collect(rows, age_matched)
    group["excluded"] = dropped
    return group


def dominant_platform(session, publication_ids: list[int]) -> str | None:
    """The platform a cycle is really about, when it is about one."""
    counts: dict[str, int] = defaultdict(int)
    for publication in session.scalars(
        select(Publication).where(Publication.id.in_(publication_ids or [-1]))
    ):
        counts[publication.platform.value] += 1
    if not counts:
        return None
    top, count = max(counts.items(), key=lambda kv: kv[1])
    # Only claim a platform when the cycle is overwhelmingly on it; a cycle
    # split across platforms is compared without a platform filter and says so.
    return top if count / sum(counts.values()) >= 0.8 else None


def evaluate_target(target: dict, result: dict, baseline: dict) -> dict:
    """Did the cycle hit its target? Reports honestly when it cannot tell."""
    metric = target.get("metric", "engagement_rate")
    label = METRIC_JA.get(metric, metric)
    goal = target.get("target")
    sample = result.get("sample", 0)

    base_out = {
        "metric": metric, "metric_label": label,
        "sample": sample, "reliability": result.get("reliability", "insufficient"),
        "age_matched": result.get("age_matched", False),
        "excluded": result.get("excluded", {}),
    }

    if sample == 0:
        dropped = result.get("excluded") or {}
        if dropped.get("too_recent"):
            reason = (
                f"対象の{dropped['too_recent']}本はまだ公開から"
                f"{MATURITY_HOURS:.0f}時間経っていません。数字が伸びきる前に"
                "判定すると、必ず過小評価になります。"
            )
        else:
            reason = "計測済みの投稿がまだありません。"
        return {**base_out, "verdict": "inconclusive", "reason": reason}

    actual = result.get(metric)
    if actual is None:
        return {**base_out, "verdict": "inconclusive",
                "reason": f"指標 {label} が収集されていません。"}

    # The live baseline wins over the one stored at planning time. The stored
    # value was computed before the cycle's posts were assigned to it, so it
    # can include those very posts - a baseline contaminated with the thing it
    # is meant to measure against. The freshly computed one excludes them by
    # construction. The planned figure is kept alongside, for the record.
    planned = target.get("baseline")
    base = baseline.get(metric)
    if not isinstance(base, (int, float)):
        base = planned if isinstance(planned, (int, float)) else None

    treatment_values = (result.get("values") or {}).get(metric) or []
    control_values = (baseline.get("values") or {}).get(metric) or []

    significance = (
        bootstrap_difference(treatment_values, control_values)
        if treatment_values and control_values
        else {"comparable": False, "reason": "比較できるベースラインがありません"}
    )
    delta_vs_base = (actual - base) if isinstance(base, (int, float)) else None

    out = {
        **base_out,
        "actual": actual,
        "baseline": base,
        "planned_baseline": planned if isinstance(planned, (int, float)) else None,
        "baseline_drifted": (
            isinstance(planned, (int, float)) and isinstance(base, (int, float))
            and abs(planned - base) > abs(base) * 0.02
        ),
        "baseline_sample": baseline.get("sample", 0),
        "delta_vs_baseline": delta_vs_base,
        "relative_change": (
            relative_effect(delta_vs_base, base) if delta_vs_base is not None else None
        ),
        "significance": significance,
        "detectable_change": (
            detectable_relative_effect(significance, base)
            if isinstance(base, (int, float)) else None
        ),
        "spread": {
            "p25": result.get(f"{metric}_p25"),
            "p75": result.get(f"{metric}_p75"),
        },
    }

    if sample < MIN_SAMPLE:
        out["verdict"] = "inconclusive"
        out["reason"] = (
            f"投稿{sample}本は最低{MIN_SAMPLE}本に届きません。"
            "この本数では差とばらつきを区別できません。"
        )
        return out

    # A numeric goal is checked on its own terms first: it is what was
    # promised. The significance test then says whether the sample can carry
    # that conclusion, and both appear in the reason.
    if isinstance(goal, (int, float)) and goal:
        ratio = actual / goal
        out["target"] = goal
        out["attainment"] = round(ratio, 4)
        verdict = "success" if actual >= goal else ("partial" if ratio >= 0.8 else "failure")
        reason = f"{label} は {actual}、目標 {goal} に対して {ratio:.0%} です。"
        if verdict == "success" and significance.get("comparable") and not significance["significant"]:
            # Hit the number, but the number is inside the noise. Say so
            # rather than banking a win that will not reproduce.
            verdict = "partial"
            reason += (
                " ただしベースラインとの差はばらつきの範囲内で、"
                f"{describe(significance, base or actual, label)}"
            )
        out["verdict"] = verdict
        out["reason"] = reason
        return out

    # No numeric goal: the baseline comparison is the whole verdict, so it has
    # to clear significance rather than merely point upwards.
    if not significance.get("comparable"):
        out["verdict"] = "inconclusive"
        out["reason"] = (
            "数値目標が未設定で、比較できる過去の投稿もありません。"
            f"目標値を設定するか、{MIN_SAMPLE}本以上の実績を貯めてください。"
        )
        return out

    sentence = describe(significance, base or actual, label)
    if not significance["significant"]:
        out["verdict"] = "inconclusive"
        out["reason"] = sentence
    else:
        improved = significance["direction"] == (
            "down" if metric in LOWER_IS_BETTER else "up"
        )
        out["verdict"] = "success" if improved else "failure"
        out["reason"] = sentence
    return out


class PdcaService:
    def __init__(self, session, llm=None):
        self.session = session
        self.llm = llm

    def plan(
        self, project, title: str, hypothesis: str, target: dict, actions: list[str]
    ) -> PdcaCycle:
        if "baseline" not in target:
            base = project_baseline(self.session, project.id)
            metric = target.get("metric", "engagement_rate")
            if base.get(metric) is not None:
                target = {**target, "baseline": base[metric],
                          "baseline_sample": base.get("sample", 0)}

        cycle = PdcaCycle(
            project_id=project.id, title=title, stage=PdcaStage.PLAN,
            hypothesis=hypothesis, target=target, actions=actions,
        )
        self.session.add(cycle)
        self.session.flush()
        return cycle

    def do(self, cycle: PdcaCycle, publication_ids: list[int]) -> PdcaCycle:
        cycle.publication_ids = sorted(set(cycle.publication_ids or []) | set(publication_ids))
        cycle.stage = PdcaStage.DO
        self.session.flush()
        return cycle

    def check(self, cycle: PdcaCycle) -> PdcaCycle:
        ids = cycle.publication_ids or []
        # Compare within one platform where the cycle is on one: engagement
        # rate does not mean the same thing across them.
        platform = dominant_platform(self.session, ids)

        result = aggregate(self.session, ids, platform=platform)
        baseline = project_baseline(
            self.session, cycle.project_id, exclude_ids=ids, platform=platform
        )
        evaluation = evaluate_target(cycle.target or {}, result, baseline)
        evaluation["platform_scope"] = platform or "mixed"

        cycle.result = {
            "measured": {
                k: v for k, v in result.items() if k not in ("per_post", "values")
            },
            "baseline": {k: v for k, v in baseline.items() if k != "values"},
            "evaluation": evaluation,
            "per_post": result.get("per_post", []),
        }
        cycle.verdict = evaluation["verdict"]
        cycle.stage = PdcaStage.CHECK
        self.session.flush()
        return cycle

    def act(self, cycle: PdcaCycle) -> PdcaCycle:
        """Turn the measurement into learnings and the next cycle's actions."""
        result = cycle.result or {}
        evaluation = result.get("evaluation", {})

        if self.llm is not None:
            review = self.llm.review_cycle(
                cycle={
                    "title": cycle.title, "hypothesis": cycle.hypothesis,
                    "target": cycle.target, "actions": cycle.actions,
                },
                metrics=result.get("measured", {}),
                baseline=result.get("baseline", {}),
            )
            cycle.verdict = review.get("verdict", cycle.verdict)
            cycle.learnings = review.get("learnings")
            cycle.next_actions = review.get("next_actions", [])
        else:
            cycle.learnings = (
                f"{evaluation.get('verdict', 'unknown')}: {evaluation.get('reason', '')}"
            )
            cycle.next_actions = _default_next_actions(evaluation)

        cycle.stage = PdcaStage.ACT
        cycle.closed_at = utcnow()
        self.session.flush()
        return cycle

    def run_check_act(self, cycle: PdcaCycle) -> PdcaCycle:
        return self.act(self.check(cycle))


# Past this many additional posts the honest answer is that the effect is too
# small to chase, not that more posting will eventually find it.
PRACTICAL_LIMIT = 40


def posts_needed(evaluation: dict) -> dict | None:
    """How many more posts would bring the noise floor under the measured change.

    The bootstrap's interval narrows roughly with the square root of the
    sample, so the shortfall is a ratio squared. This is an estimate and is
    presented as one - but "post four more" is actionable in a way that
    "inconclusive" is not.

    Beyond a practical limit it stops being advice. Returning the capped number
    as though it were the estimate would tell someone to post 40 more videos to
    prove a 1% difference; the answer there is that the difference is too small
    to be worth proving, and the caller is told which case it is.
    """
    measured = evaluation.get("relative_change")
    floor = evaluation.get("detectable_change")
    sample = evaluation.get("sample") or 0
    if not measured or not floor or not sample:
        return None
    if abs(measured) >= floor:
        return {"more": 0, "reachable": True}

    needed = sample * (floor / abs(measured)) ** 2
    more = max(1, int(needed - sample) + 1)
    return {
        "more": min(more, PRACTICAL_LIMIT),
        "reachable": more <= PRACTICAL_LIMIT,
        "estimated": more,
    }


def _default_next_actions(evaluation: dict) -> list[dict]:
    verdict = evaluation.get("verdict")
    label = evaluation.get("metric_label", "エンゲージ率")

    if verdict == "success":
        return [{
            "action": f"同じ型を別のキーワードでもう一度試し、{label} が再現するか確認する。",
            "reason": "1回勝っただけでは候補にすぎず、再現性のある型とは言えません。",
            "priority": "high",
        }]

    if verdict == "inconclusive":
        needed = posts_needed(evaluation)
        measured = evaluation.get("relative_change") or 0
        floor = evaluation.get("detectable_change") or 0
        if needed and needed["more"]:
            if needed["reachable"]:
                return [{
                    "action": f"同じ仮説であと{needed['more']}本投稿してから再判定する。",
                    "reason": (
                        f"測定した変化は{measured:+.0%}、"
                        f"いまの本数で判別できるのは{floor:.0%}以上です。"
                        f"この差を検出するには約{needed['more']}本の追加が必要です。"
                    ),
                    "priority": "high",
                }]
            return [{
                "action": "この仮説は打ち切り、変化量の大きい別の仮説を立てる。",
                "reason": (
                    f"測定した変化{measured:+.0%}を証明するには約{needed['estimated']}本の"
                    "追加投稿が必要で、現実的ではありません。"
                    "この差は、追いかける価値があるほど大きくありません。"
                ),
                "priority": "high",
            }]
        return [{
            "action": f"同じ仮説であと{MIN_SAMPLE}本投稿してから判定する。",
            "reason": evaluation.get("reason", "sample too small"),
            "priority": "high",
        }]

    if verdict == "partial":
        # Hit the number, or came close, but the sample cannot carry it.
        return [{
            "action": f"同じ条件でもう数本投稿し、{label} が再現するか確認する。",
            "reason": (
                "目標値そのものには届いていますが、ベースラインとの差はばらつきの"
                "範囲内です。いまの本数では、効果なのか偶然なのか判別できません。"
            ) if (evaluation.get("attainment") or 0) >= 1.0 else (
                f"{label} は目標の{(evaluation.get('attainment') or 0):.0%}でした。"
                "構成のどの要素が足りなかったかを1つだけ変えて再試行してください。"
            ),
            "priority": "high",
        }]

    actions = [
        {"action": "題材と構成は固定したまま、フックだけを書き直す。",
         "reason": "維持率に対して、単独で最も効く変数がフックです。",
         "priority": "high"},
        {"action": "上位の競合投稿とテロップ量を比較する。",
         "reason": f"{label} が目標に届きませんでした。", "priority": "medium"},
    ]
    spread = evaluation.get("spread") or {}
    if spread.get("p25") is not None and spread.get("p75"):
        # A wide spread means the average is hiding two different outcomes.
        if spread["p75"] > spread["p25"] * 2.5:
            actions.insert(0, {
                "action": "この回の投稿を上位と下位に分け、伸びた投稿だけの共通点を探す。",
                "reason": (
                    f"{label} が {spread['p25']} 〜 {spread['p75']} と大きくばらついており、"
                    "平均が性質の違う2群を隠している可能性があります。"
                ),
                "priority": "high",
            })
    return actions
