"""PDCA cycle management.

A cycle is a hypothesis with a measurable target attached to a specific set of
publications. `check()` compares the measured result against both the stated
target and the project's own prior baseline, so "engagement went up" is only
called a success when it beat what the account was already doing.
"""

from __future__ import annotations

import statistics

from sqlalchemy import select

from ..models import PdcaCycle, PdcaStage, Publication, PublicationStatus, utcnow
from .collect import summarize_publication

# Below this many posts, a difference in engagement rate is not distinguishable
# from normal variance on social platforms.
MIN_SAMPLE = 3

METRIC_FIELDS = (
    "views", "likes", "comments", "shares", "saves",
    "engagement_rate", "views_per_hour",
)


def project_baseline(session, project_id: int, exclude_ids: list[int] | None = None) -> dict:
    """The project's prior average across published posts."""
    stmt = select(Publication).where(
        Publication.project_id == project_id,
        Publication.status == PublicationStatus.PUBLISHED,
    )
    exclude = set(exclude_ids or [])
    rows = [
        summarize_publication(p)
        for p in session.scalars(stmt)
        if p.id not in exclude
    ]
    rows = [r for r in rows if r.get("has_data")]
    if not rows:
        return {"sample": 0}

    baseline = {"sample": len(rows)}
    for field in METRIC_FIELDS:
        values = [r.get(field, 0) or 0 for r in rows]
        baseline[field] = round(statistics.fmean(values), 5)
    return baseline


def aggregate(session, publication_ids: list[int]) -> dict:
    rows = [
        summarize_publication(p)
        for p in session.scalars(
            select(Publication).where(Publication.id.in_(publication_ids or [-1]))
        )
    ]
    rows = [r for r in rows if r.get("has_data")]
    if not rows:
        return {"sample": 0}

    result = {"sample": len(rows)}
    for field in METRIC_FIELDS:
        values = [r.get(field, 0) or 0 for r in rows]
        result[field] = round(statistics.fmean(values), 5)
    result["total_views"] = sum(r["views"] for r in rows)
    result["per_post"] = rows
    return result


def evaluate_target(target: dict, result: dict, baseline: dict) -> dict:
    """Did the cycle hit its target? Reports honestly when it cannot tell."""
    metric = target.get("metric", "engagement_rate")
    goal = target.get("target")
    sample = result.get("sample", 0)

    if sample == 0:
        return {"verdict": "inconclusive", "reason": "計測済みの投稿がまだありません。"}

    actual = result.get(metric)
    if actual is None:
        return {"verdict": "inconclusive", "reason": f"指標 {metric} が収集されていません。"}

    base = target.get("baseline", baseline.get(metric))
    delta_vs_base = (actual - base) if isinstance(base, (int, float)) else None

    if sample < MIN_SAMPLE:
        return {
            "verdict": "inconclusive",
            "reason": (
                f"投稿{sample}本は最低{MIN_SAMPLE}本に届きません。"
                "この本数では差とばらつきを区別できません。"
            ),
            "metric": metric, "actual": actual, "baseline": base,
            "delta_vs_baseline": delta_vs_base,
        }

    if not isinstance(goal, (int, float)):
        verdict = "inconclusive" if delta_vs_base is None else (
            "success" if delta_vs_base > 0 else "failure"
        )
        return {
            "verdict": verdict, "reason": "数値目標が未設定のため、ベースラインとの比較で判定しました。",
            "metric": metric, "actual": actual, "baseline": base,
            "delta_vs_baseline": delta_vs_base,
        }

    ratio = actual / goal if goal else 0.0
    verdict = "success" if actual >= goal else ("partial" if ratio >= 0.8 else "failure")
    return {
        "verdict": verdict,
        "reason": f"{metric} は {actual}、目標 {goal} に対して {ratio:.0%} です。",
        "metric": metric, "actual": actual, "target": goal, "baseline": base,
        "attainment": round(ratio, 4), "delta_vs_baseline": delta_vs_base,
    }


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
            if metric in base:
                target = {**target, "baseline": base[metric]}

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
        result = aggregate(self.session, cycle.publication_ids or [])
        baseline = project_baseline(
            self.session, cycle.project_id, exclude_ids=cycle.publication_ids
        )
        evaluation = evaluate_target(cycle.target or {}, result, baseline)

        cycle.result = {
            "measured": {k: v for k, v in result.items() if k != "per_post"},
            "baseline": baseline,
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


def _default_next_actions(evaluation: dict) -> list[dict]:
    verdict = evaluation.get("verdict")
    metric = evaluation.get("metric", "engagement_rate")
    if verdict == "success":
        return [{
            "action": f"同じ型を別のキーワードでもう一度試し、{metric} が再現するか確認する。",
            "reason": "1回勝っただけでは候補にすぎず、再現性のある型とは言えません。",
            "priority": "high",
        }]
    if verdict == "inconclusive":
        return [{
            "action": f"同じ仮説であと{MIN_SAMPLE}本投稿してから判定する。",
            "reason": evaluation.get("reason", "sample too small"),
            "priority": "high",
        }]
    return [
        {"action": "題材と構成は固定したまま、フックだけを書き直す。",
         "reason": "維持率に対して、単独で最も効く変数がフックです。",
         "priority": "high"},
        {"action": "上位の競合投稿とテロップ量を比較する。",
         "reason": f"{metric} が目標に届きませんでした。", "priority": "medium"},
    ]
