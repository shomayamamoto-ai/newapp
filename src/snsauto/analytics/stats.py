"""Statistics for small, skewed, noisy samples - which is all social data.

Three properties of this data break the obvious approach:

* **The samples are tiny.** A PDCA cycle is 3-10 posts. Anything that assumes
  a normal distribution is guessing at that size.
* **The distribution is long-tailed.** One post in twenty carries ten times
  the views of the rest, so a mean describes the outlier, not the account.
* **Nothing is controlled.** Two posts differ in topic, timing, thumbnail and
  the platform's mood that day, all at once.

So: medians rather than means, a bootstrap rather than a t-test, and - the
part that matters most - an explicit statement of what the sample *cannot*
detect. A cycle that says "engagement rose 12%" when its own noise floor is
40% has not learned anything, and should say so instead of declaring a win.

Everything here is stdlib and deterministic: a verdict that changes when you
re-run it is not a verdict.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable, Sequence

# Resampling is deterministic so the same data always yields the same verdict.
BOOTSTRAP_SEED = 20260101
BOOTSTRAP_ITERATIONS = 2000
DEFAULT_CONFIDENCE = 0.90

# Sample-size bands. Chosen from what the bootstrap can actually resolve on
# this kind of data, not from a textbook: below 3 the CI spans everything,
# and past ~10 the interval stops shrinking quickly.
RELIABILITY_BANDS = ((3, "insufficient"), (6, "weak"), (12, "usable"))

RELIABILITY_JA = {
    "insufficient": "判定不能（本数が足りません）",
    "weak": "参考値（ばらつきに埋もれる可能性があります）",
    "usable": "実用可能",
    "solid": "十分",
}


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile.

    Nearest-rank makes p90 equal the maximum for any sample under ten, so the
    "top decile" of a 50-post corpus would be reported as a single post's
    value. Interpolating keeps small samples honest.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def mad(values: Sequence[float]) -> float:
    """Median absolute deviation - spread that one viral post cannot move."""
    if len(values) < 2:
        return 0.0
    centre = median(values)
    return median([abs(v - centre) for v in values])


def iqr(values: Sequence[float]) -> tuple[float, float]:
    return percentile(values, 0.25), percentile(values, 0.75)


# Below this many values there is nothing to spare: dropping the ends of a
# four-post sample discards half the evidence.
MIN_TRIMMABLE = 5


def trimmed_mean(values: Sequence[float], trim: float = 0.1) -> float:
    """Mean with the extremes dropped. Keeps a mean's meaning on skewed data.

    A proportional trim alone does nothing at the sizes this codebase works
    with - int(6 * 0.1) is 0, so a six-post sample containing one 500x outlier
    would come back with that outlier fully weighted, which is the exact case
    the function exists for. At least one value comes off each end once the
    sample can afford it.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) < MIN_TRIMMABLE:
        return float(statistics.fmean(ordered))
    drop = max(1, int(len(ordered) * trim))
    kept = ordered[drop: len(ordered) - drop] or ordered
    return float(statistics.fmean(kept))


def reliability(sample: int) -> str:
    """How much weight this sample size can carry."""
    for threshold, label in RELIABILITY_BANDS:
        if sample < threshold:
            return label
    return "solid"


def _resample(values: Sequence[float], rng: random.Random) -> list[float]:
    return [values[rng.randrange(len(values))] for _ in range(len(values))]


def bootstrap_difference(
    treatment: Sequence[float],
    control: Sequence[float],
    statistic: Callable[[Sequence[float]], float] = median,
    confidence: float = DEFAULT_CONFIDENCE,
    iterations: int = BOOTSTRAP_ITERATIONS,
    seed: int = BOOTSTRAP_SEED,
) -> dict:
    """Confidence interval for the difference between two groups.

    Resampling rather than a t-test because at n=4 the t-test's normality
    assumption is doing all the work and none of it is justified. The bootstrap
    makes no distributional claim: it asks what range of differences is
    consistent with the data actually collected.

    ``noise_floor`` is the half-width of the interval - the smallest difference
    this sample could have distinguished from chance. It is the number that
    tells an operator whether to keep posting or to conclude, and it is
    reported whether or not the result is significant.
    """
    if not treatment or not control:
        return {"comparable": False, "reason": "片方のサンプルが空です"}

    observed = statistic(treatment) - statistic(control)
    rng = random.Random(seed)
    differences = [
        statistic(_resample(treatment, rng)) - statistic(_resample(control, rng))
        for _ in range(iterations)
    ]
    tail = (1.0 - confidence) / 2
    low = percentile(differences, tail)
    high = percentile(differences, 1 - tail)

    return {
        "comparable": True,
        "difference": observed,
        "low": low,
        "high": high,
        # The interval excluding zero is the only thing that licenses the word
        # "improved". A positive mean difference alone never does.
        "significant": (low > 0) or (high < 0),
        "direction": "up" if observed > 0 else ("down" if observed < 0 else "flat"),
        "noise_floor": (high - low) / 2,
        "confidence": confidence,
        "n_treatment": len(treatment),
        "n_control": len(control),
    }


def relative_effect(difference: float, baseline: float) -> float | None:
    """Difference as a share of the baseline. None when there is no baseline."""
    if not baseline:
        return None
    return difference / abs(baseline)


def detectable_relative_effect(result: dict, baseline: float) -> float | None:
    """The smallest relative change this sample could have proved.

    Reported next to the measured change so the two can be compared directly:
    a 12% measured lift against a 40% noise floor is not a small win, it is no
    result at all.
    """
    if not result.get("comparable") or not baseline:
        return None
    return result["noise_floor"] / abs(baseline)


def describe(result: dict, baseline: float, metric_label: str = "指標") -> str:
    """One Japanese sentence that never overstates what was measured."""
    if not result.get("comparable"):
        return result.get("reason", "比較できません。")

    relative = relative_effect(result["difference"], baseline)
    floor = detectable_relative_effect(result, baseline)
    measured = f"{relative:+.0%}" if relative is not None else f"{result['difference']:+.4g}"

    if result["significant"]:
        return (
            f"{metric_label}が{measured}変化しました。"
            f"{result['confidence']:.0%}信頼区間がゼロを含まないため、"
            "偶然では説明しにくい差です。"
        )
    if floor is not None:
        return (
            f"{metric_label}の変化は{measured}ですが、"
            f"この本数（{result['n_treatment']}本 vs {result['n_control']}本）で"
            f"判別できるのは{floor:.0%}以上の差までです。差とばらつきを区別できません。"
        )
    return (
        f"{metric_label}の変化は{measured}ですが、"
        "ばらつきの範囲内で、差があるとは言えません。"
    )
