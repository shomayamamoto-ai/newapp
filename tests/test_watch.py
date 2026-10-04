"""Watched competitors and the trend diff between two sweeps."""

from datetime import datetime, timedelta, timezone

import pytest

from snsauto.models import CompetitorPost, Platform, ResearchRun, StructureAnalysis
from snsauto.research.watch import (
    MATERIAL_CHANGE,
    WatchService,
    _delta,
    diff_runs,
    per_follower_engagement,
)


def make_run(session, project, keyword="@rival", followers=None, ids=("a", "b"),
             engagement=0.05, views=1000, account_id=None, created=None):
    run = ResearchRun(
        project_id=project.id, keyword=keyword, platform=Platform.YOUTUBE,
        source="watch", account_id=account_id,
        filters={"watch": {"followers": followers}} if followers else {},
    )
    if created:
        run.created_at = created
    session.add(run)
    session.flush()
    for i, external in enumerate(ids):
        session.add(CompetitorPost(
            run_id=run.id, external_id=external, platform=Platform.YOUTUBE,
            rank=i + 1, views=views, likes=int(views * engagement),
            engagement_rate=engagement, score=1.0 - i * 0.1,
            duration_sec=30.0,
        ))
    session.flush()
    session.refresh(run)
    return run


class TestRegistry:
    def test_adding_the_same_handle_twice_reuses_the_row(self, session, project):
        service = WatchService(session)
        first = service.add(project, Platform.YOUTUBE, "@rival")
        second = service.add(project, Platform.YOUTUBE, "rival", label="Rival Co")

        assert first.id == second.id
        assert second.label == "Rival Co"
        assert second.handle == "rival"   # stored without the @

    def test_re_adding_a_retired_competitor_reactivates_it(self, session, project):
        service = WatchService(session)
        account = service.add(project, Platform.YOUTUBE, "rival")
        account.active = False
        session.flush()

        assert service.add(project, Platform.YOUTUBE, "rival").active

    def test_the_same_handle_on_two_platforms_is_two_competitors(self, session, project):
        service = WatchService(session)
        a = service.add(project, Platform.YOUTUBE, "rival")
        b = service.add(project, Platform.X, "rival")
        assert a.id != b.id

    def test_display_falls_back_to_the_handle(self, session, project):
        account = WatchService(session).add(project, Platform.X, "rival")
        assert account.display == "@rival"
        account.label = "Rival Co"
        assert account.display == "Rival Co"


class TestDelta:
    def test_a_change_below_the_noise_floor_is_not_material(self):
        change = _delta(1.05, 1.0)
        assert change["change"] == pytest.approx(0.05)
        assert not change["material"]

    def test_a_change_above_the_noise_floor_is_material(self):
        assert _delta(1.0 + MATERIAL_CHANGE + 0.01, 1.0)["material"]

    def test_a_missing_baseline_yields_no_percentage(self):
        # Dividing by a zero or absent baseline would produce an infinite
        # "change" and a headline claiming a movement nobody can verify.
        assert _delta(10, 0) is None
        assert _delta(10, None) is None
        assert _delta(None, 10) is None


class TestDiff:
    def test_new_posts_are_the_ones_absent_from_the_previous_sweep(self, session, project):
        before = make_run(session, project, ids=("a", "b"))
        after = make_run(session, project, ids=("b", "c", "d"))
        result = diff_runs(before, after)

        assert result["comparable"]
        assert result["new_posts"] == 2

    def test_an_empty_sweep_is_not_comparable(self, session, project):
        before = make_run(session, project, ids=())
        after = make_run(session, project, ids=("a",))
        result = diff_runs(before, after)

        assert not result["comparable"]
        assert "比較できません" in result["reason"]

    def test_a_material_engagement_move_reaches_the_headline(self, session, project):
        before = make_run(session, project, engagement=0.04)
        after = make_run(session, project, engagement=0.08)
        result = diff_runs(before, after)

        assert result["engagement"]["material"]
        assert "エンゲージ率" in result["headline"]

    def test_a_quiet_week_says_so_rather_than_inventing_a_movement(self, session, project):
        before = make_run(session, project, ids=("a", "b"), engagement=0.05)
        after = make_run(session, project, ids=("a", "b"), engagement=0.051)
        result = diff_runs(before, after)

        assert result["new_posts"] == 0
        assert result["headline"] == "前回から有意な変化はありません。"

    def test_follower_growth_is_reported_when_both_sweeps_have_it(self, session, project):
        before = make_run(session, project, followers=10_000)
        after = make_run(session, project, followers=13_000)
        result = diff_runs(before, after)

        assert result["followers"]["material"]
        assert "フォロワー" in result["headline"]

    def test_a_hook_shift_needs_structure_analysis_on_both_sides(self, session, project):
        before = make_run(session, project, ids=("a", "b"))
        after = make_run(session, project, ids=("c", "d"))
        for post in before.posts:
            post.structure = StructureAnalysis(hook_type="statement")
        for post in after.posts:
            post.structure = StructureAnalysis(hook_type="question")
        session.flush()

        shifts = diff_runs(before, after)["hook_shift"]
        assert {s["hook"] for s in shifts} == {"statement", "question"}
        assert shifts[0]["change"] in (1.0, -1.0)

    def test_posting_cadence_is_normalised_to_a_week(self, session, project):
        now = datetime.now(timezone.utc)
        before = make_run(session, project, ids=("a",), created=now - timedelta(days=14))
        after = make_run(session, project, ids=("a", "b", "c"), created=now)

        result = diff_runs(before, after)
        assert result["days_between"] == pytest.approx(14.0, abs=0.2)
        assert result["posts_per_week"] == pytest.approx(1.0, abs=0.1)


class TestPerFollower:
    def test_it_is_none_for_a_keyword_run(self, session, project):
        # Keyword search returns no follower count on any platform. Returning
        # a placeholder would make the number mean something different from
        # run to run.
        run = make_run(session, project, keyword="ダイエット", followers=None)
        assert per_follower_engagement(run) is None

    def test_it_is_computed_for_a_watched_account(self, session, project):
        run = make_run(session, project, followers=1000, views=2000,
                       engagement=0.1, ids=("a",))
        # 2000 views x 0.1 = 200 likes, over 1000 followers
        assert per_follower_engagement(run) == pytest.approx(0.2)


class TestPreviousRun:
    def test_it_finds_the_last_sweep_of_the_same_target(self, session, project):
        older = make_run(session, project, keyword="@rival", account_id=None)
        newer = make_run(session, project, keyword="@rival", account_id=None)
        make_run(session, project, keyword="@someone-else")
        session.refresh(project)

        assert WatchService(session).previous_run(newer).id == older.id

    def test_the_first_sweep_has_nothing_to_compare_against(self, session, project):
        run = make_run(session, project, keyword="@rival")
        session.refresh(project)
        assert WatchService(session).previous_run(run) is None


class TestCadenceIsNotExtrapolatedFromNothing:
    def test_two_sweeps_hours_apart_report_no_weekly_rate(self, session, project):
        # Sweeping twice in one afternoon would otherwise claim a weekly
        # posting rate computed from a couple of hours.
        now = datetime.now(timezone.utc)
        before = make_run(session, project, ids=("a",), created=now - timedelta(hours=2))
        after = make_run(session, project, ids=("a", "b", "c"), created=now)

        result = diff_runs(before, after)
        assert result["new_posts"] == 2
        assert result["posts_per_week"] is None
        assert "ペース" not in result["headline"]
        assert "新規2本" in result["headline"]

    def test_a_week_apart_does_report_a_rate(self, session, project):
        now = datetime.now(timezone.utc)
        before = make_run(session, project, ids=("a",), created=now - timedelta(days=7))
        after = make_run(session, project, ids=("a", "b", "c"), created=now)

        assert diff_runs(before, after)["posts_per_week"] == pytest.approx(2.0, abs=0.1)
