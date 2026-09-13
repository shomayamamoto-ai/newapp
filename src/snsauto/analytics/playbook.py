"""What works for *this* account, learned from its own results.

The generator has been shown competitor research and a brand profile since the
beginning, and nothing else. It has never been shown how the account's own
posts performed - so the hundredth script was written with exactly as much
knowledge as the first. That is the difference between a tool that imitates
competitors and one that compounds.

This closes the loop. Each published post is reduced to the choices that
produced it - hook archetype, duration, shots, telop density, posting hour,
narration - and paired with what those choices earned. Where a choice
separates from its alternatives by more than the sample's own noise, it
becomes a finding the next script is told about.

The discipline that makes it worth trusting is refusal. Three posts cannot
show that question hooks beat statements; they can only show that three posts
happened. Every finding carries its sample and clears the bootstrap's noise
floor before it is stated, and a playbook with nothing significant in it says
so instead of inventing guidance. Advice invented from four data points is
worse than no advice, because it gets followed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from ..models import Publication, PublicationStatus
from .collect import MATURITY_HOURS, summarize_publication
from .stats import bootstrap_difference, median, reliability

# A feature value seen fewer times than this cannot be compared to anything.
MIN_PER_VALUE = 3

# Below this many posts overall, nothing is a pattern - only a description.
MIN_POSTS = 6

# What we compare outcomes on, in the order they matter for distribution.
OUTCOMES = ("retention_rate", "engagement_rate", "views")

OUTCOME_JA = {
    "retention_rate": "視聴維持率",
    "engagement_rate": "エンゲージ率",
    "views": "再生数",
}

FEATURE_JA = {
    "hook_type": "フックの型",
    "duration_band": "尺",
    "shot_count_band": "カット数",
    "telop_density_band": "テロップ密度",
    "posting_hour_band": "投稿時間帯",
    "has_narration": "ナレーション",
    "has_cta": "CTA",
}


@dataclass
class Finding:
    feature: str
    value: str
    outcome: str
    better_by: float
    sample: int
    versus_sample: int
    confident: bool

    def sentence(self) -> str:
        feature = FEATURE_JA.get(self.feature, self.feature)
        outcome = OUTCOME_JA.get(self.outcome, self.outcome)
        return (
            f"{feature}が「{self.value}」の投稿は、それ以外より"
            f"{outcome}が{self.better_by:+.0%}高い"
            f"（{self.sample}本 vs {self.versus_sample}本）"
        )


@dataclass
class Playbook:
    project_id: int
    sample: int
    reliability: str
    findings: list[Finding] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)
    retention: dict = field(default_factory=dict)
    note: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.findings)


def _band(value: float | None, edges: list[tuple[float, str]]) -> str | None:
    if value is None:
        return None
    for limit, label in edges:
        if value < limit:
            return label
    return edges[-1][1]


def features_of(publication: Publication) -> dict:
    """The choices that produced this post, as comparable categories.

    Bands rather than raw numbers: with the sample sizes involved, comparing
    "28.4 seconds" to "31.2 seconds" finds differences that are not there.
    """
    from ..research.structure import classify_hook

    script = publication.script
    render = publication.render
    board = getattr(render, "storyboard", None)
    shots = sorted(getattr(board, "shots", []) or [], key=lambda s: s.index)

    duration = getattr(render, "duration_sec", None) or (
        script.target_duration_sec if script else None
    )
    telop_chars = sum(len((s.telop or "").replace("\n", "")) for s in shots)

    hook_type = None
    if script and script.hook:
        hook_type, _ = classify_hook(script.hook)

    published = publication.published_at
    hour = None
    if published is not None:
        from ..scheduling.worker import to_local

        local = to_local(published)
        hour = local.hour if local else None

    return {
        "hook_type": hook_type,
        "duration_band": _band(duration, [(16, "〜15秒"), (31, "16〜30秒"),
                                          (61, "31〜60秒"), (1e9, "60秒超")]),
        "shot_count_band": _band(
            len(shots) or None, [(5, "4カット以下"), (9, "5〜8カット"), (1e9, "9カット以上")]
        ),
        "telop_density_band": _band(
            (telop_chars / duration) if duration and telop_chars else None,
            [(2.0, "低め"), (4.0, "標準"), (1e9, "高め")],
        ),
        "posting_hour_band": _band(
            hour, [(6, "深夜"), (11, "朝"), (15, "昼"), (19, "夕方"), (1e9, "夜")]
        ) if hour is not None else None,
        "has_narration": (
            "あり" if any(s.narration for s in shots) else "なし"
        ) if shots else None,
        "has_cta": ("あり" if script.cta else "なし") if script else None,
    }


def outcomes_of(summary: dict) -> dict:
    """Age-matched where possible, so an older post is not simply ahead."""
    at_age = summary.get("at_24h") or {}
    return {
        "retention_rate": at_age.get("retention_rate", summary.get("retention_rate")),
        "engagement_rate": at_age.get("engagement_rate", summary.get("engagement_rate")),
        "views": at_age.get("views", summary.get("views")),
    }


def _rows(session, project_id: int, platform=None) -> list[dict]:
    stmt = select(Publication).where(
        Publication.project_id == project_id,
        Publication.status == PublicationStatus.PUBLISHED,
    )
    if platform is not None:
        stmt = stmt.where(Publication.platform == platform)

    rows = []
    for publication in session.scalars(stmt):
        summary = summarize_publication(publication)
        if not summary.get("has_data"):
            continue
        if summary.get("age_hours", 0) < MATURITY_HOURS:
            continue          # still climbing; would flatter whichever side it lands on
        rows.append({
            "features": features_of(publication),
            "outcomes": outcomes_of(summary),
            "publication": publication,
        })
    return rows


def _compare(rows: list[dict], feature: str, outcome: str) -> list[Finding]:
    """Does any value of this feature beat the rest by more than the noise?"""
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row["features"].get(feature)
        measured = row["outcomes"].get(outcome)
        if value is None or measured is None:
            continue
        groups[value].append(float(measured))

    usable = {k: v for k, v in groups.items() if len(v) >= MIN_PER_VALUE}
    if len(usable) < 2:
        return []

    findings = []
    for value, mine in usable.items():
        others = [m for k, v in usable.items() if k != value for m in v]
        if len(others) < MIN_PER_VALUE:
            continue
        result = bootstrap_difference(mine, others)
        if not result.get("comparable"):
            continue
        base = median(others)
        if not base:
            continue
        findings.append(Finding(
            feature=feature, value=value, outcome=outcome,
            better_by=result["difference"] / abs(base),
            sample=len(mine), versus_sample=len(others),
            confident=result["significant"],
        ))
    return findings


def build(session, project_id: int, platform=None) -> Playbook:
    """What this account's own results say, and how much they can say it."""
    rows = _rows(session, project_id, platform)
    sample = len(rows)
    band = reliability(sample)

    if sample < MIN_POSTS:
        return Playbook(
            project_id=project_id, sample=sample, reliability=band,
            note=(
                f"計測済みの投稿が{sample}本です。"
                f"{MIN_POSTS}本を超えると、自社の勝ちパターンを抽出して"
                "台本生成に反映できるようになります。"
                "それまでは競合調査のみを参照します。"
            ),
        )

    findings: list[Finding] = []
    for feature in FEATURE_JA:
        for outcome in OUTCOMES:
            findings.extend(_compare(rows, feature, outcome))

    # Only what cleared the noise floor, strongest first. Everything else was
    # measured and is deliberately not reported: a near-miss stated as a
    # finding is how a tool teaches an account the wrong lesson.
    confident = sorted(
        (f for f in findings if f.confident and f.better_by > 0),
        key=lambda f: f.better_by, reverse=True,
    )

    baseline = {
        outcome: round(median([
            r["outcomes"][outcome] for r in rows
            if r["outcomes"].get(outcome) is not None
        ]), 5)
        for outcome in OUTCOMES
        if any(r["outcomes"].get(outcome) is not None for r in rows)
    }

    return Playbook(
        project_id=project_id, sample=sample, reliability=band,
        findings=confident[:8], baseline=baseline,
        retention=_retention_summary(rows),
        note=(
            f"{sample}本の自社実績から抽出しました。"
            if confident else
            f"{sample}本を分析しましたが、ばらつきを超える差は見つかりませんでした。"
            "投稿本数が増えると検出できるようになります。"
        ),
    )


def _retention_summary(rows: list[dict]) -> dict:
    """Where this account's videos lose people, across all of them."""
    from .retention import aggregate, diagnose

    diagnoses = []
    for row in rows:
        publication = row["publication"]
        render = publication.render
        board = getattr(render, "storyboard", None)
        shots = sorted(getattr(board, "shots", []) or [], key=lambda s: s.index)
        snapshots = sorted(publication.snapshots, key=lambda s: s.captured_at)
        curve = next((s.retention_curve for s in reversed(snapshots)
                      if s.retention_curve), None)
        if curve:
            diagnoses.append(
                diagnose(curve, getattr(render, "duration_sec", 0.0) or 0.0, shots)
            )
    return aggregate(diagnoses) if diagnoses else {"usable": False, "sample": 0}


def to_prompt(playbook: Playbook) -> str:
    """The playbook as instructions, or nothing at all.

    Returning an empty string when there is no evidence is the point: a
    section headed "what works for this account" filled with guesses would be
    followed exactly as confidently as one filled with measurements.
    """
    if not playbook.usable:
        return ""

    lines = [
        "",
        "# このアカウントの実績（自社の過去投稿から統計的に有意だったもの）",
        f"対象 {playbook.sample}本。以下は偶然では説明しにくい差が出た項目です。",
        "競合の構成より、こちらを優先してください。",
    ]
    for finding in playbook.findings:
        lines.append(f"- {finding.sentence()}")

    retention = playbook.retention or {}
    if retention.get("usable") and retention.get("mode") == "pattern":
        drops = retention.get("recurring_drops") or []
        if drops:
            seconds = "、".join(f"{d['second']}秒" for d in drops[:3])
            lines.append(
                f"- このアカウントの動画は {seconds} で視聴者が離脱しがちです。"
                "その時間帯の構成を変えてください。"
            )
        hook = retention.get("median_hook_retention")
        if hook is not None:
            lines.append(
                f"- 冒頭3秒の残存は中央値{hook:.0%}です。"
                "最初のテロップは1秒以内に出し、1枚で意味が通る短さにしてください。"
            )
    return "\n".join(lines)


def describe(playbook: Playbook) -> list[str]:
    """Human-readable lines for the UI."""
    if not playbook.usable:
        return [playbook.note]
    return [finding.sentence() for finding in playbook.findings]
