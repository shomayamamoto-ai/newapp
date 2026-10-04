"""Retention curves read against the timeline that produced them."""

from dataclasses import dataclass

import pytest

from snsauto.analytics.retention import (
    HOOK_WINDOW_SEC,
    MATERIAL_DROP,
    MIN_VIDEOS_FOR_PATTERN,
    aggregate,
    compare_to_plan,
    diagnose,
    drop_offs,
    on_screen_at,
    to_seconds,
)


@dataclass
class Shot:
    index: int
    start: float
    end: float
    telop: str = ""
    narration: str = ""
    transition: str = "cut"


SHOTS = [
    Shot(0, 0.0, 2.5, "朝食を抜くのは逆効果"),
    Shot(1, 2.5, 5.0, "理由は3つあります"),
    Shot(2, 5.0, 6.2, "血糖値の急上昇が食欲を増やし昼に過食しやすくなるため"),
    Shot(3, 6.2, 12.0, "まずはタンパク質から"),
]


def curve(ratios, relative=0.9):
    span = max(1, len(ratios) - 1)
    return {"points": [
        {"elapsed_ratio": i / span, "watch_ratio": r, "relative": relative}
        for i, r in enumerate(ratios)
    ]}


FALLING = [1.0, 0.92, 0.78, 0.62, 0.58, 0.55, 0.52, 0.34, 0.31, 0.29, 0.27, 0.25]


class TestScaling:
    def test_progress_ratios_become_seconds_on_our_timeline(self):
        points = to_seconds(curve([1.0, 0.5, 0.25]), duration_sec=12.0)
        assert [p.second for p in points] == [0.0, 6.0, 12.0]

    def test_no_curve_is_no_points(self):
        assert to_seconds(None, 12.0) == []
        assert to_seconds(curve([1.0]), 0) == []

    def test_a_coarse_curve_does_not_report_a_perfect_hook(self):
        """With samples at 0/4/8/12s the only point inside the 3s window is
        the one at zero, where retention is 1.0 by definition."""
        from snsauto.analytics.retention import hook_retention

        points = to_seconds(curve([1.0, 0.4, 0.35, 0.3]), 12.0)
        value = hook_retention(points)
        assert value < 1.0
        # Interpolated three quarters of the way from 1.0 down to 0.4.
        assert value == pytest.approx(0.55, abs=0.01)


class TestDropOffs:
    def test_the_curve_s_normal_texture_is_not_a_drop_off(self):
        gentle = curve([1.0, 0.99, 0.98, 0.97])
        assert drop_offs(to_seconds(gentle, 10.0)) == []

    def test_a_real_fall_is_found_and_ranked(self):
        falls = drop_offs(to_seconds(curve(FALLING), 12.0))
        assert falls
        assert falls[0]["lost"] >= MATERIAL_DROP
        assert falls == sorted(falls, key=lambda f: f["lost"], reverse=True)


class TestAlignment:
    def test_a_second_resolves_to_the_shot_that_was_showing(self):
        shot = on_screen_at(5.5, SHOTS)
        assert shot["index"] == 2
        assert shot["telop_chars"] == len(SHOTS[2].telop)

    def test_past_the_end_is_nothing(self):
        assert on_screen_at(99.0, SHOTS) is None

    def test_the_summary_names_the_beat_not_just_the_timestamp(self):
        """"They left at 6.5s" has nowhere to go. The beat does."""
        result = diagnose(curve(FALLING), 12.0, SHOTS)
        assert "カット目" in result["summary"]
        assert result["drop_offs"][0]["on_screen"] is not None


class TestDiagnosis:
    def test_it_reports_hook_and_end_retention(self):
        result = diagnose(curve(FALLING), 12.0, SHOTS)
        assert result["measured"]
        assert 0 < result["hook_retention"] <= 1
        assert result["end_retention"] == pytest.approx(0.25)
        assert result["hook_window_sec"] == HOOK_WINDOW_SEC

    def test_youtube_s_own_benchmark_is_carried_through(self):
        # Saves inventing a baseline of our own.
        assert diagnose(curve(FALLING, relative=1.4), 12.0, SHOTS)[
            "relative_performance"
        ] == pytest.approx(1.4)

    def test_a_missing_curve_says_why_rather_than_showing_zeros(self):
        result = diagnose(None, 12.0, SHOTS)
        assert not result["measured"]
        assert "yt-analytics.readonly" in result["reason"]


class TestPlanComparison:
    def test_text_too_long_for_its_hold_is_named(self):
        shots = [Shot(0, 0.0, 1.2, "血糖値の急上昇が食欲を増やし昼に過食しやすくなるため"),
                 Shot(1, 1.2, 12.0, "続き")]
        result = diagnose(curve([1.0, 0.5, 0.45, 0.4]), 12.0, shots)
        notes = compare_to_plan(result, shots)
        assert any("読み切れない" in n for n in notes)

    def test_a_shot_held_too_long_is_named(self):
        result = diagnose(curve(FALLING), 12.0, SHOTS)
        assert any("秒続いています" in n for n in compare_to_plan(result, SHOTS))

    def test_a_failing_hook_points_at_the_first_shot(self):
        result = diagnose(curve([1.0, 0.4, 0.35, 0.3]), 12.0, SHOTS)
        notes = compare_to_plan(result, SHOTS)
        assert any("フックが機能していません" in n for n in notes)

    def test_nothing_measured_means_nothing_claimed(self):
        assert compare_to_plan({"measured": False}, SHOTS) == []


class TestAggregation:
    def test_one_video_is_a_description_not_a_pattern(self):
        """A drop at 3s in one video is that video."""
        result = aggregate([diagnose(curve(FALLING), 12.0, SHOTS)])
        assert result["mode"] == "description"
        assert str(MIN_VIDEOS_FOR_PATTERN) in result["note"]

    def test_enough_videos_become_a_pattern(self):
        one = diagnose(curve(FALLING), 12.0, SHOTS)
        result = aggregate([one] * MIN_VIDEOS_FOR_PATTERN)
        assert result["mode"] == "pattern"

    def test_a_moment_repeated_across_videos_surfaces(self):
        one = diagnose(curve(FALLING), 12.0, SHOTS)
        recurring = aggregate([one] * 4)["recurring_drops"]
        assert recurring and recurring[0]["videos"] >= 2

    def test_videos_without_curves_do_not_count_towards_the_sample(self):
        result = aggregate([diagnose(None, 12.0, SHOTS)] * 5)
        assert not result["usable"]
        assert result["sample"] == 0


class TestAdapterContract:
    def test_the_curve_request_targets_the_analytics_report(self):
        import httpx

        from snsauto.config import Settings
        from snsauto.platforms.youtube import YouTubeAdapter

        captured = {}

        def handle(request):
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"rows": [
                [0.0, 1.0, 0.9], [0.5, 0.6, 0.9], [1.0, 0.3, 0.9],
            ]})

        class Creds:
            access_token = "owner-token"
            external_id = "UC1"

        adapter = YouTubeAdapter(
            settings=Settings(_env_file=None, YOUTUBE_API_KEY="k"),
            client=httpx.Client(transport=httpx.MockTransport(handle)),
            credentials=Creds(),
        )
        result = adapter.fetch_retention_curve("vid1")

        assert "elapsedVideoTimeRatio" in captured["url"]
        assert "audienceWatchRatio" in captured["url"]
        # Paid traffic leaves at a different rate and would blur the shape.
        assert "audienceType%3D%3DORGANIC" in captured["url"] or \
               "audienceType==ORGANIC" in captured["url"]
        assert captured["auth"] == "Bearer owner-token"
        assert len(result["points"]) == 3

    def test_a_missing_grant_returns_nothing_rather_than_an_empty_curve(self):
        import httpx

        from snsauto.config import Settings
        from snsauto.platforms.youtube import YouTubeAdapter

        class Creds:
            access_token = "t"
            external_id = "UC1"

        adapter = YouTubeAdapter(
            settings=Settings(_env_file=None, YOUTUBE_API_KEY="k"),
            client=httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(403, json={}))
            ),
            credentials=Creds(),
        )
        assert adapter.fetch_retention_curve("vid1") is None
