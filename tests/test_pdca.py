import pytest

from snsauto.analytics.pdca import MIN_SAMPLE, PdcaService, evaluate_target
from snsauto.models import PdcaStage


BASE = {"engagement_rate": 0.041}
TARGET = {"metric": "engagement_rate", "target": 0.06}


def test_success_when_target_met():
    r = evaluate_target(TARGET, {"sample": 5, "engagement_rate": 0.071}, BASE)
    assert r["verdict"] == "success"
    assert r["attainment"] > 1


def test_partial_when_close():
    assert evaluate_target(TARGET, {"sample": 5, "engagement_rate": 0.050}, BASE)["verdict"] == "partial"


def test_failure_when_far_below():
    assert evaluate_target(TARGET, {"sample": 5, "engagement_rate": 0.020}, BASE)["verdict"] == "failure"


def test_small_sample_is_inconclusive_even_when_target_smashed():
    """One good post is not evidence; the tool must not claim a win."""
    r = evaluate_target(TARGET, {"sample": 1, "engagement_rate": 0.30}, BASE)
    assert r["verdict"] == "inconclusive"
    assert str(MIN_SAMPLE) in r["reason"]


def test_no_data_is_inconclusive():
    assert evaluate_target(TARGET, {"sample": 0}, BASE)["verdict"] == "inconclusive"


def test_uncollected_metric_is_inconclusive():
    r = evaluate_target({"metric": "watch_time_sec"}, {"sample": 9}, BASE)
    assert r["verdict"] == "inconclusive"


def test_without_numeric_target_a_bare_mean_difference_is_not_a_verdict():
    """The old behaviour called any positive delta a success.

    With no target and no per-post values there is nothing to test the
    difference against, so the only honest answer is that it cannot be told -
    and the reason has to name what is missing.
    """
    metric = {"metric": "engagement_rate"}
    up = evaluate_target(metric, {"sample": 5, "engagement_rate": 0.09}, BASE)
    assert up["verdict"] == "inconclusive"
    assert "目標値" in up["reason"]


def test_without_numeric_target_a_real_lift_is_a_success():
    metric = {"metric": "engagement_rate"}
    result = {"sample": 6, "engagement_rate": 0.10,
              "values": {"engagement_rate": [0.09, 0.10, 0.11, 0.095, 0.105, 0.098]}}
    baseline = {"engagement_rate": 0.041,
                "values": {"engagement_rate": [0.040, 0.045, 0.042, 0.038, 0.041, 0.043]}}
    verdict = evaluate_target(metric, result, baseline)
    assert verdict["verdict"] == "success"
    assert verdict["significance"]["significant"]


def test_delta_vs_baseline_reported():
    r = evaluate_target(TARGET, {"sample": 5, "engagement_rate": 0.071}, BASE)
    assert r["delta_vs_baseline"] == pytest.approx(0.03)


class TestCycleLifecycle:
    def test_stages_advance(self, session, project):
        service = PdcaService(session)
        cycle = service.plan(project, "hook test", "questions retain better", TARGET, ["rewrite hooks"])
        assert cycle.stage == PdcaStage.PLAN

        service.do(cycle, [1, 2])
        assert cycle.stage == PdcaStage.DO and cycle.publication_ids == [1, 2]

        service.check(cycle)
        assert cycle.stage == PdcaStage.CHECK
        # No publications exist, so it must refuse to conclude.
        assert cycle.verdict == "inconclusive"

        service.act(cycle)
        assert cycle.stage == PdcaStage.ACT
        assert cycle.closed_at is not None
        assert cycle.next_actions

    def test_attach_is_idempotent(self, session, project):
        service = PdcaService(session)
        cycle = service.plan(project, "t", "h", TARGET, [])
        service.do(cycle, [1, 2])
        service.do(cycle, [2, 3])
        assert cycle.publication_ids == [1, 2, 3]

    def test_inconclusive_next_action_asks_for_more_posts(self, session, project):
        service = PdcaService(session)
        cycle = service.plan(project, "t", "h", TARGET, [])
        service.run_check_act(cycle)
        assert any("あと" in a["action"] and "本投稿" in a["action"]
                   for a in cycle.next_actions)


class TestMaturityAndAgeMatching:
    """A post measured three hours in has not finished accumulating."""

    def _publish(self, session, project, age_hours, snapshots):
        from datetime import datetime, timedelta, timezone

        from snsauto.models import (MetricSnapshot, Platform, Publication,
                                    PublicationStatus)

        now = datetime.now(timezone.utc)
        published = now - timedelta(hours=age_hours)
        pub = Publication(project_id=project.id, platform=Platform.YOUTUBE,
                          status=PublicationStatus.PUBLISHED, external_id=f"e{age_hours}",
                          published_at=published)
        session.add(pub)
        session.flush()
        for offset_h, views, likes in snapshots:
            session.add(MetricSnapshot(
                publication_id=pub.id, views=views, likes=likes,
                captured_at=published + timedelta(hours=offset_h),
            ))
        session.flush()
        session.refresh(pub)
        return pub

    def test_an_immature_post_is_excluded_and_counted(self, session, project):
        from snsauto.analytics.pdca import project_baseline

        self._publish(session, project, age_hours=2, snapshots=[(1, 40, 2)])
        baseline = project_baseline(session, project.id)

        assert baseline["sample"] == 0
        assert baseline["excluded"]["too_recent"] == 1

    def test_the_reason_names_the_maturity_rule(self, session, project):
        from snsauto.analytics.pdca import aggregate, evaluate_target

        pub = self._publish(session, project, age_hours=2, snapshots=[(1, 40, 2)])
        result = aggregate(session, [pub.id])
        verdict = evaluate_target({"metric": "engagement_rate"}, result, {"sample": 0})

        assert verdict["verdict"] == "inconclusive"
        assert "24時間" in verdict["reason"]

    def test_metrics_are_read_at_a_matched_age(self, session, project):
        from snsauto.analytics.collect import metrics_at_age

        # A month-old post whose 24h reading was modest but whose lifetime
        # total is large. Comparing lifetime totals would flatter it.
        old = self._publish(session, project, age_hours=24 * 30,
                            snapshots=[(24, 1000, 50), (24 * 30, 90_000, 4000)])
        at_24h = metrics_at_age(old)
        assert at_24h["views"] == 1000
        assert at_24h["age_hours"] == pytest.approx(24.0, abs=0.5)

    def test_a_post_with_no_snapshot_near_that_age_returns_nothing(self, session, project):
        from snsauto.analytics.collect import metrics_at_age

        # Only a 2-hour reading exists; substituting it for the 24-hour one
        # would reintroduce the bias the matching removes.
        pub = self._publish(session, project, age_hours=24 * 5, snapshots=[(2, 300, 10)])
        assert metrics_at_age(pub) is None


class TestSignificance:
    def _values(self, treatment, control):
        return (
            {"sample": len(treatment), "engagement_rate": sorted(treatment)[len(treatment) // 2],
             "values": {"engagement_rate": treatment}, "reliability": "usable"},
            {"sample": len(control), "engagement_rate": sorted(control)[len(control) // 2],
             "values": {"engagement_rate": control}},
        )

    def test_a_win_inside_the_noise_is_downgraded_from_success(self, session):
        """Hitting the number does not mean the number will reproduce."""
        result, baseline = self._values(
            [0.061, 0.059, 0.060, 0.062], [0.058, 0.061, 0.059, 0.060]
        )
        verdict = evaluate_target(
            {"metric": "engagement_rate", "target": 0.060}, result, baseline
        )
        assert verdict["verdict"] == "partial"
        assert "ばらつきの範囲内" in verdict["reason"]

    def test_a_genuine_win_stays_a_success(self, session):
        result, baseline = self._values(
            [0.090, 0.095, 0.101, 0.098], [0.040, 0.042, 0.039, 0.041]
        )
        verdict = evaluate_target(
            {"metric": "engagement_rate", "target": 0.060}, result, baseline
        )
        assert verdict["verdict"] == "success"

    def test_the_detectable_change_is_always_reported(self, session):
        result, baseline = self._values(
            [0.043, 0.051, 0.038], [0.040, 0.044, 0.039]
        )
        verdict = evaluate_target({"metric": "engagement_rate"}, result, baseline)
        assert verdict["detectable_change"] > 0
        assert verdict["relative_change"] is not None

    def test_it_says_how_many_more_posts_would_settle_it(self, session):
        """"Inconclusive" alone is not actionable; a post count is."""
        from snsauto.analytics.pdca import _default_next_actions, posts_needed

        # A 16% lift that five posts cannot separate from their own spread.
        result, baseline = self._values(
            [0.050, 0.064, 0.043, 0.057, 0.052],
            [0.043, 0.056, 0.037, 0.049, 0.045],
        )
        verdict = evaluate_target({"metric": "engagement_rate"}, result, baseline)
        assert verdict["verdict"] == "inconclusive"

        needed = posts_needed(verdict)
        assert needed["reachable"] and needed["more"] > 0
        action = _default_next_actions(verdict)[0]
        assert "本投稿してから再判定" in action["action"]
        assert str(needed["more"]) in action["action"]

    def test_a_difference_too_small_to_chase_says_to_stop(self, session):
        """Telling someone to post 40 more videos to prove 7% is not advice."""
        from snsauto.analytics.pdca import PRACTICAL_LIMIT, _default_next_actions, posts_needed

        result, baseline = self._values(
            [0.043, 0.051, 0.038, 0.047], [0.040, 0.055, 0.036, 0.044]
        )
        verdict = evaluate_target({"metric": "engagement_rate"}, result, baseline)
        needed = posts_needed(verdict)

        assert not needed["reachable"]
        assert needed["estimated"] > PRACTICAL_LIMIT
        # The capped number must never be presented as the estimate.
        assert needed["more"] == PRACTICAL_LIMIT
        assert "打ち切り" in _default_next_actions(verdict)[0]["action"]

    def test_the_live_baseline_beats_the_one_stored_at_planning_time(self, session):
        """The planned baseline can contain the cycle's own posts.

        `plan()` snapshots the project average before `do()` assigns posts to
        the cycle, so those posts are usually already in it. Comparing against
        that figure measures the cycle partly against itself.
        """
        result, baseline = self._values(
            [0.090, 0.095, 0.101, 0.098], [0.040, 0.042, 0.039, 0.041]
        )
        verdict = evaluate_target(
            {"metric": "engagement_rate", "baseline": 0.075}, result, baseline
        )
        assert verdict["baseline"] == baseline["engagement_rate"]
        assert verdict["planned_baseline"] == 0.075
        assert verdict["baseline_drifted"]

    def test_a_partial_verdict_is_not_told_it_failed(self, session):
        from snsauto.analytics.pdca import _default_next_actions

        result, baseline = self._values(
            [0.061, 0.059, 0.060, 0.062], [0.058, 0.061, 0.059, 0.060]
        )
        verdict = evaluate_target(
            {"metric": "engagement_rate", "target": 0.060}, result, baseline
        )
        assert verdict["verdict"] == "partial"
        reason = _default_next_actions(verdict)[0]["reason"]
        assert "届いていますが" in reason
        assert "届きませんでした" not in reason


class TestOutlierRobustness:
    def test_one_viral_post_does_not_carry_the_verdict(self, session, project):
        from datetime import datetime, timedelta, timezone

        from snsauto.analytics.pdca import aggregate
        from snsauto.models import (MetricSnapshot, Platform, Publication,
                                    PublicationStatus)

        now = datetime.now(timezone.utc)
        ids = []
        for views in (500, 520, 480, 510, 200_000):
            pub = Publication(project_id=project.id, platform=Platform.YOUTUBE,
                              status=PublicationStatus.PUBLISHED,
                              external_id=f"v{views}",
                              published_at=now - timedelta(days=5))
            session.add(pub)
            session.flush()
            session.add(MetricSnapshot(
                publication_id=pub.id, views=views, likes=views // 20,
                captured_at=now - timedelta(days=5) + timedelta(hours=24),
            ))
            ids.append(pub.id)
        session.flush()

        group = aggregate(session, ids)
        # The median describes the account; the mean describes the outlier.
        assert group["views"] == 510
        assert group["views_mean"] > 40_000
