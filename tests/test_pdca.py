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


def test_without_numeric_target_judges_against_baseline():
    metric = {"metric": "engagement_rate"}
    assert evaluate_target(metric, {"sample": 5, "engagement_rate": 0.09}, BASE)["verdict"] == "success"
    assert evaluate_target(metric, {"sample": 5, "engagement_rate": 0.01}, BASE)["verdict"] == "failure"


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
        assert any("more posts" in a["action"] for a in cycle.next_actions)
