"""A/B testing: generate variants, measure them, declare a winner honestly.

The discipline this enforces is *one changed variable*. A test where the hook,
the length and the hashtags all differ tells you a video did better - not why,
and not what to do again. So each experiment names a single `dimension` and the
generator only touches that; everything else is copied from the base script.

Declaring a winner is deliberately conservative. Social metrics are extremely
noisy: two identical videos routinely differ by 30%. A winner is only called
when every arm has a real sample and the gap is larger than the spread within
the arms - otherwise the verdict is "inconclusive", with the reason stated.
"""

from __future__ import annotations

import logging
import statistics

from sqlalchemy import select

from ..analytics.collect import summarize_publication
from ..analytics.pdca import MIN_SAMPLE
from ..models import (
    Experiment,
    Project,
    Publication,
    Script,
    Variant,
    utcnow,
)
from ..research.structure import HOOK_PATTERNS

log = logging.getLogger(__name__)

# What a variant may change, and how the generator changes it.
DIMENSIONS = {
    "hook": "Rewrite only the opening line; keep every other beat identical.",
    "telop_density": "Same narration, different amount of on-screen text.",
    "duration": "Same content, compressed or expanded runtime.",
    "cta": "Change only the closing call to action.",
    "hashtags": "Same video, different tag mix.",
}

HOOK_ARCHETYPES = [name for name, _ in HOOK_PATTERNS]

# A winner needs to beat the runner-up by more than the noise inside the arms.
MIN_RELATIVE_LIFT = 0.15


def variant_result(session, variant: Variant) -> dict:
    """Aggregate a variant's publications."""
    rows = [
        summarize_publication(p)
        for p in session.scalars(
            select(Publication).where(Publication.id.in_(variant.publication_ids or [-1]))
        )
    ]
    rows = [r for r in rows if r.get("has_data")]
    if not rows:
        return {"sample": 0, "per_post": []}

    out = {"sample": len(rows), "per_post": rows}
    for field in ("views", "likes", "comments", "shares", "engagement_rate", "views_per_hour"):
        values = [r.get(field, 0) or 0 for r in rows]
        out[field] = round(statistics.fmean(values), 5)
        if len(values) > 1:
            out[f"{field}_stdev"] = round(statistics.stdev(values), 5)
    return out


def compare_variants(results: dict[str, dict], metric: str = "engagement_rate") -> dict:
    """Pick a winner, or explain why the data does not support one."""
    measured = {k: v for k, v in results.items() if v.get("sample", 0) > 0}
    if len(measured) < 2:
        return {
            "verdict": "inconclusive",
            "reason": f"{len(measured)} arm(s) have data; a comparison needs at least 2",
        }

    thin = [k for k, v in measured.items() if v["sample"] < MIN_SAMPLE]
    if thin:
        return {
            "verdict": "inconclusive",
            "reason": (
                f"arms {sorted(thin)} have fewer than {MIN_SAMPLE} posts; "
                "social variance at that sample size swamps any real difference"
            ),
            "samples": {k: v["sample"] for k, v in measured.items()},
        }

    ranked = sorted(measured.items(), key=lambda kv: kv[1].get(metric, 0), reverse=True)
    (best_label, best), (second_label, second) = ranked[0], ranked[1]
    best_value = best.get(metric, 0.0)
    second_value = second.get(metric, 0.0)

    if second_value <= 0:
        lift = float("inf") if best_value > 0 else 0.0
    else:
        lift = (best_value - second_value) / second_value

    # Spread inside the arms sets the bar the gap has to clear.
    spreads = [v.get(f"{metric}_stdev", 0.0) for v in measured.values()]
    noise = max(spreads) if spreads else 0.0
    gap = best_value - second_value

    if lift < MIN_RELATIVE_LIFT or (noise and gap < noise):
        return {
            "verdict": "inconclusive",
            "reason": (
                f"{best_label} leads {second_label} by {lift:.0%} "
                f"({gap:.4f}), within the {noise:.4f} spread inside the arms"
            ),
            "ranking": [(k, v.get(metric, 0)) for k, v in ranked],
            "samples": {k: v["sample"] for k, v in measured.items()},
        }

    return {
        "verdict": "winner",
        "winner": best_label,
        "metric": metric,
        "reason": f"{best_label} beat {second_label} by {lift:.0%} on {metric}",
        "lift": round(lift, 4),
        "ranking": [(k, v.get(metric, 0)) for k, v in ranked],
        "samples": {k: v["sample"] for k, v in measured.items()},
    }


class ExperimentService:
    def __init__(self, session, llm=None):
        self.session = session
        self.llm = llm

    def create(
        self,
        project: Project,
        name: str,
        base_script: Script,
        dimension: str = "hook",
        arms: int = 2,
        metric: str = "engagement_rate",
        cycle_id: int | None = None,
    ) -> Experiment:
        if dimension not in DIMENSIONS:
            raise ValueError(
                f"unknown dimension {dimension!r}; choose from {sorted(DIMENSIONS)}"
            )
        arms = max(2, min(5, arms))

        experiment = Experiment(
            project_id=project.id, cycle_id=cycle_id, name=name,
            dimension=dimension, metric=metric, base_script_id=base_script.id,
        )
        self.session.add(experiment)
        self.session.flush()

        for i in range(arms):
            label = chr(ord("A") + i)
            script, treatment = self._make_arm(project, base_script, dimension, i, label)
            self.session.add(Variant(
                experiment_id=experiment.id,
                script_id=script.id,
                label=label,
                treatment=treatment,
            ))
        self.session.flush()
        return experiment

    def _make_arm(
        self, project: Project, base: Script, dimension: str, index: int, label: str
    ) -> tuple[Script, dict]:
        """Arm A is the base as-is; later arms change exactly one thing."""
        lines = [dict(line) for line in (base.lines or [])]
        script = Script(
            project_id=project.id,
            run_id=base.run_id,
            title=f"{base.title}｜{label}",
            platform=base.platform,
            target_duration_sec=base.target_duration_sec,
            hook=base.hook,
            body=base.body,
            cta=base.cta,
            lines=lines,
            hashtags=list(base.hashtags or []),
            rationale=f"Variant {label} of script {base.id}, varying {dimension}.",
        )
        treatment: dict = {"dimension": dimension, "control": index == 0}

        if index > 0:
            if dimension == "hook":
                archetype = HOOK_ARCHETYPES[index % len(HOOK_ARCHETYPES)]
                hook = self._rewrite_hook(base, archetype)
                script.hook = hook
                if lines:
                    lines[0] = {**lines[0], "telop": hook, "narration": hook}
                    script.lines = lines
                treatment.update(hook=hook, hook_type=archetype)

            elif dimension == "telop_density":
                # Halve the on-screen text: same words spoken, fewer read.
                for i, line in enumerate(lines):
                    if i % 2 == 1:
                        line["telop"] = ""
                script.lines = lines
                treatment.update(telop="every other beat")

            elif dimension == "duration":
                factor = 0.7 if index == 1 else 1.3
                target = round(base.target_duration_sec * factor, 1)
                script.target_duration_sec = target
                script.lines = self._rescale(lines, target)
                treatment.update(duration_sec=target, factor=factor)

            elif dimension == "cta":
                cta = self._rewrite_cta(base, index)
                script.cta = cta
                if lines:
                    lines[-1] = {**lines[-1], "telop": cta, "narration": cta}
                    script.lines = lines
                treatment.update(cta=cta)

            elif dimension == "hashtags":
                tags = list(base.hashtags or [])
                # Narrow arm: drop the broadest tags and keep the niche ones.
                script.hashtags = tags[len(tags) // 2:] or tags
                treatment.update(hashtags=script.hashtags, strategy="niche only")

        self.session.add(script)
        self.session.flush()
        return script, treatment

    def _rewrite_hook(self, base: Script, archetype: str) -> str:
        if self.llm is not None:
            try:
                data = self.llm.write_script(
                    keyword=base.title, platform=base.platform.value,
                    duration=base.target_duration_sec, brand_profile={},
                    research={"instruction": f"hook archetype: {archetype}"},
                )
                if data.get("hook"):
                    return data["hook"]
            except Exception as exc:
                log.warning("LLM hook rewrite failed: %s", exc)
        topic = (base.title or "").split("｜")[0]
        templates = {
            "question": f"{topic}、なぜうまくいかないか知ってる？",
            "negative": f"{topic}でこれをやると損します",
            "listicle": f"{topic}の3つのポイント",
            "curiosity": f"実は{topic}には裏があります",
            "result": f"{topic}を試した結果がこちら",
            "authority": f"プロが教える{topic}",
            "urgency": f"今すぐ知るべき{topic}",
            "statement": f"{topic}の正解はこれです",
        }
        return templates.get(archetype, templates["statement"])

    def _rewrite_cta(self, base: Script, index: int) -> str:
        options = [
            "保存して後で見返してね",
            "コメントで質問してください",
            "フォローで続きが届きます",
            "プロフィールのリンクをチェック",
        ]
        return options[index % len(options)]

    @staticmethod
    def _rescale(lines: list[dict], target: float) -> list[dict]:
        """Stretch or compress beats to a new runtime, keeping them contiguous."""
        if not lines:
            return lines
        original = max((l.get("end", 0) for l in lines), default=0) or target
        factor = target / original
        cursor = 0.0
        out = []
        for i, line in enumerate(lines):
            length = max(0.5, (line.get("end", 0) - line.get("start", 0)) * factor)
            end = target if i == len(lines) - 1 else min(target, cursor + length)
            out.append({**line, "index": i, "start": round(cursor, 2), "end": round(end, 2)})
            cursor = end
        out[-1]["end"] = target
        return out

    # ---------- measurement ----------

    def attach(self, variant: Variant, publication_ids: list[int]) -> Variant:
        variant.publication_ids = sorted(
            set(variant.publication_ids or []) | set(publication_ids)
        )
        self.session.flush()
        return variant

    def evaluate(self, experiment: Experiment) -> Experiment:
        results = {}
        for variant in experiment.variants:
            variant.result = variant_result(self.session, variant)
            results[variant.label] = variant.result

        conclusion = compare_variants(results, experiment.metric)
        experiment.conclusion = conclusion

        if conclusion["verdict"] == "winner":
            winner = next(
                (v for v in experiment.variants if v.label == conclusion["winner"]), None
            )
            experiment.winner_variant_id = winner.id if winner else None
            experiment.closed_at = utcnow()
        self.session.flush()
        return experiment

    def learnings(self, experiment: Experiment) -> str:
        """One sentence a human can act on, safe to paste into a PDCA cycle."""
        conclusion = experiment.conclusion or {}
        if conclusion.get("verdict") != "winner":
            return f"{experiment.name}: {conclusion.get('reason', 'not yet measured')}"
        winner = next(
            (v for v in experiment.variants if v.label == conclusion["winner"]), None
        )
        treatment = (winner.treatment if winner else {}) or {}
        detail = ", ".join(
            f"{k}={v}" for k, v in treatment.items() if k not in ("dimension", "control")
        )
        return (
            f"{experiment.dimension} matters here: arm {conclusion['winner']} "
            f"({detail or 'control'}) beat the runner-up by "
            f"{conclusion['lift']:.0%} on {experiment.metric}."
        )
