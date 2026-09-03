"""A/B experiments: variant generation and honest winner declaration."""

import pytest

from snsauto.creative.script import ScriptService
from snsauto.experiments import DIMENSIONS, ExperimentService, compare_variants
from snsauto.models import Platform


def arm(sample, value, stdev=0.004, metric="engagement_rate"):
    row = {"sample": sample, metric: value}
    if stdev is not None:
        row[f"{metric}_stdev"] = stdev
    return row


@pytest.fixture
def base_script(session, project):
    return ScriptService(session).generate(project, "副業", Platform.TIKTOK, 20.0)


class TestVerdicts:
    def test_clear_lead_wins(self):
        result = compare_variants({"A": arm(4, 0.040), "B": arm(4, 0.062)})
        assert result["verdict"] == "winner" and result["winner"] == "B"

    def test_small_lead_is_inconclusive(self):
        result = compare_variants({"A": arm(4, 0.050), "B": arm(4, 0.053)})
        assert result["verdict"] == "inconclusive"

    def test_lead_inside_the_noise_is_inconclusive(self):
        """A 37% lead means nothing when the arms themselves vary that much."""
        result = compare_variants({"A": arm(5, 0.040, 0.03), "B": arm(5, 0.055, 0.03)})
        assert result["verdict"] == "inconclusive"
        assert "ばらつき" in result["reason"]

    def test_thin_samples_refuse_to_conclude(self):
        result = compare_variants({"A": arm(1, 0.02), "B": arm(1, 0.30)})
        assert result["verdict"] == "inconclusive"
        assert "本未満" in result["reason"]

    def test_one_measured_arm_is_not_a_comparison(self):
        result = compare_variants({"A": arm(5, 0.05), "B": {"sample": 0}})
        assert result["verdict"] == "inconclusive"

    def test_no_data_at_all(self):
        assert compare_variants({})["verdict"] == "inconclusive"

    def test_ranking_is_reported_even_when_inconclusive(self):
        result = compare_variants({"A": arm(4, 0.050), "B": arm(4, 0.053)})
        assert [label for label, _ in result["ranking"]] == ["B", "A"]


class TestVariantGeneration:
    def test_control_arm_is_the_base_unchanged(self, session, project, base_script):
        experiment = ExperimentService(session).create(
            project, "hook test", base_script, "hook", arms=3
        )
        control = experiment.variants[0]
        assert control.treatment["control"] is True
        assert control.script.hook == base_script.hook

    def test_hook_arms_differ_from_control_and_each_other(self, session, project, base_script):
        experiment = ExperimentService(session).create(
            project, "hook test", base_script, "hook", arms=3
        )
        hooks = [v.script.hook for v in experiment.variants]
        assert len(set(hooks)) == 3

    def test_only_the_named_dimension_changes(self, session, project, base_script):
        """The whole point: everything else must be identical."""
        experiment = ExperimentService(session).create(
            project, "hook test", base_script, "hook", arms=2
        )
        control, treated = experiment.variants
        assert control.script.hook != treated.script.hook
        assert control.script.cta == treated.script.cta
        assert control.script.hashtags == treated.script.hashtags
        assert control.script.target_duration_sec == treated.script.target_duration_sec

    def test_duration_arm_rescales_and_stays_contiguous(self, session, project, base_script):
        experiment = ExperimentService(session).create(
            project, "length", base_script, "duration", arms=2
        )
        treated = experiment.variants[1].script
        assert treated.target_duration_sec != base_script.target_duration_sec
        lines = treated.lines
        assert lines[0]["start"] == 0.0
        assert lines[-1]["end"] == pytest.approx(treated.target_duration_sec)
        for a, b in zip(lines, lines[1:]):
            assert a["end"] == pytest.approx(b["start"])

    def test_telop_arm_thins_the_on_screen_text(self, session, project, base_script):
        experiment = ExperimentService(session).create(
            project, "telop", base_script, "telop_density", arms=2
        )
        control, treated = experiment.variants
        filled = lambda s: len([l for l in s.lines if l.get("telop")])  # noqa: E731
        assert filled(treated.script) < filled(control.script)

    def test_cta_arm_changes_only_the_closing_line(self, session, project, base_script):
        experiment = ExperimentService(session).create(
            project, "cta", base_script, "cta", arms=2
        )
        control, treated = experiment.variants
        assert treated.script.cta != control.script.cta
        assert treated.script.hook == control.script.hook

    def test_arm_count_is_clamped(self, session, project, base_script):
        one = ExperimentService(session).create(project, "x", base_script, "hook", arms=1)
        assert len(one.variants) == 2
        many = ExperimentService(session).create(project, "y", base_script, "hook", arms=99)
        assert len(many.variants) == 5

    def test_unknown_dimension_is_rejected(self, session, project, base_script):
        with pytest.raises(ValueError):
            ExperimentService(session).create(project, "x", base_script, "vibes")

    def test_every_documented_dimension_generates(self, session, project, base_script):
        for dimension in DIMENSIONS:
            experiment = ExperimentService(session).create(
                project, dimension, base_script, dimension, arms=2
            )
            assert all(v.script_id for v in experiment.variants)


class TestEvaluation:
    def test_unmeasured_experiment_stays_open(self, session, project, base_script):
        service = ExperimentService(session)
        experiment = service.create(project, "x", base_script, "hook", arms=2)
        service.evaluate(experiment)
        assert experiment.conclusion["verdict"] == "inconclusive"
        assert experiment.winner_variant_id is None
        assert experiment.closed_at is None

    def test_learnings_read_as_a_sentence(self, session, project, base_script):
        service = ExperimentService(session)
        experiment = service.create(project, "hook test", base_script, "hook", arms=2)
        service.evaluate(experiment)
        assert "hook test" in service.learnings(experiment)
