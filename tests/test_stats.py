"""Small-sample statistics: what the sample can and cannot prove."""

import pytest

from snsauto.analytics.stats import (
    bootstrap_difference,
    describe,
    detectable_relative_effect,
    mad,
    percentile,
    reliability,
    relative_effect,
    trimmed_mean,
)


class TestPercentile:
    def test_p90_of_a_small_sample_is_not_just_the_maximum(self):
        # Nearest-rank returns 5 here, so the "top decile" of a five-post
        # corpus would be reported as one post's value.
        assert percentile([1, 2, 3, 4, 5], 0.9) == pytest.approx(4.6)

    def test_the_median_matches_statistics_median(self):
        assert percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)

    def test_edges_are_the_min_and_max(self):
        assert percentile([3, 1, 2], 0.0) == 1
        assert percentile([3, 1, 2], 1.0) == 3

    def test_a_single_value_is_its_own_percentile(self):
        assert percentile([7], 0.9) == 7

    def test_empty_is_zero_not_an_error(self):
        assert percentile([], 0.5) == 0.0


class TestRobustSpread:
    def test_one_viral_post_moves_the_mean_and_not_the_median(self):
        normal = [100, 110, 95, 105, 102]
        viral = normal + [50_000]
        assert trimmed_mean(viral) < 1000
        # The untrimmed mean of the same data is over 8000.
        assert sum(viral) / len(viral) > 8000

    def test_mad_is_zero_for_a_single_value(self):
        assert mad([5]) == 0.0


class TestReliability:
    @pytest.mark.parametrize("n,expected", [
        (0, "insufficient"), (2, "insufficient"),
        (3, "weak"), (5, "weak"),
        (6, "usable"), (11, "usable"),
        (12, "solid"), (50, "solid"),
    ])
    def test_bands(self, n, expected):
        assert reliability(n) == expected


class TestBootstrap:
    def test_a_clear_separation_is_significant(self):
        result = bootstrap_difference(
            [0.09, 0.10, 0.11, 0.095, 0.105, 0.098],
            [0.04, 0.045, 0.042, 0.038, 0.041, 0.043],
        )
        assert result["significant"]
        assert result["direction"] == "up"
        assert result["low"] > 0

    def test_a_small_wobble_on_three_posts_is_not(self):
        # The failure this replaces: the old code called any positive delta a
        # success, so this would have been reported as an improvement.
        result = bootstrap_difference(
            [0.043, 0.051, 0.038], [0.040, 0.044, 0.039]
        )
        assert not result["significant"]

    def test_a_drop_is_reported_as_a_drop(self):
        result = bootstrap_difference(
            [0.01, 0.012, 0.009, 0.011], [0.05, 0.052, 0.048, 0.051]
        )
        assert result["significant"]
        assert result["direction"] == "down"

    def test_the_same_data_always_gives_the_same_verdict(self):
        """A verdict that changes on re-run is not a verdict."""
        args = ([0.05, 0.06, 0.04, 0.055], [0.04, 0.041, 0.039, 0.042])
        first = bootstrap_difference(*args)
        second = bootstrap_difference(*args)
        assert first["low"] == second["low"] and first["high"] == second["high"]

    def test_an_empty_group_is_not_comparable(self):
        assert not bootstrap_difference([], [0.04])["comparable"]

    def test_the_noise_floor_shrinks_as_the_sample_grows(self):
        small = bootstrap_difference([0.05, 0.06, 0.04], [0.04, 0.041, 0.039])
        large = bootstrap_difference(
            [0.05, 0.06, 0.04] * 8, [0.04, 0.041, 0.039] * 8
        )
        assert large["noise_floor"] < small["noise_floor"]


class TestReporting:
    def test_the_noise_floor_is_stated_even_when_nothing_is_proven(self):
        result = bootstrap_difference([0.043, 0.051, 0.038], [0.040, 0.044, 0.039])
        floor = detectable_relative_effect(result, 0.041)
        assert floor > 0
        sentence = describe(result, 0.041, "エンゲージ率")
        # An operator needs to know it is 3 vs 3 and what size of effect that
        # could have caught - "inconclusive" on its own is not actionable.
        assert "3本" in sentence
        assert f"{floor:.0%}" in sentence

    def test_a_significant_result_says_so_plainly(self):
        result = bootstrap_difference(
            [0.09, 0.10, 0.11, 0.095, 0.105, 0.098],
            [0.04, 0.045, 0.042, 0.038, 0.041, 0.043],
        )
        assert "偶然では説明しにくい" in describe(result, 0.042, "エンゲージ率")

    def test_relative_effect_needs_a_baseline(self):
        assert relative_effect(0.01, 0) is None
        assert relative_effect(0.01, 0.05) == pytest.approx(0.2)


class TestCalibration:
    """The false-positive rate has to match what the interval claims.

    A significance test that fires on noise is worse than no test: it hands
    the operator a confident wrong answer. Both groups here are drawn from the
    same distribution, so every "significant" verdict is a false positive, and
    a 90% interval should produce them about 10% of the time.

    Measured across sample sizes 3-20 at 400 trials each, the rate came out
    between 6.5% and 13.2%. The bound below is loose enough not to flake and
    tight enough to catch a method that has stopped being a test at all.
    """

    def test_noise_is_not_reported_as_a_result(self):
        import random

        rng = random.Random(7)
        trials, false_positives = 150, 0
        for trial in range(trials):
            # Lognormal: social engagement is skewed, not normal.
            a = [rng.lognormvariate(-3.2, 0.45) for _ in range(5)]
            b = [rng.lognormvariate(-3.2, 0.45) for _ in range(5)]
            if bootstrap_difference(a, b, iterations=300, seed=1000 + trial)["significant"]:
                false_positives += 1
        assert false_positives / trials < 0.20
