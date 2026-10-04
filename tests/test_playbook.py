"""Learning from the account's own results, and refusing to when it cannot."""

from datetime import datetime, timedelta, timezone


from snsauto.analytics.playbook import (
    MIN_POSTS,
    MIN_PER_VALUE,
    build,
    features_of,
    to_prompt,
)
from snsauto.models import (
    MetricSnapshot, Platform, Publication, PublicationStatus, Render, Script,
    Shot, Storyboard,
)

NOW = datetime.now(timezone.utc)


def publish(session, project, *, hook, engagement, retention=0.4, duration=25.0,
            shots=4, telop="テロップです", ident="v"):
    published = NOW - timedelta(days=10)
    script = Script(project_id=project.id, title="t", platform=Platform.YOUTUBE,
                    target_duration_sec=duration, hook=hook, cta="c", lines=[])
    session.add(script)
    session.flush()
    board = Storyboard(script_id=script.id)
    session.add(board)
    session.flush()
    for i in range(shots):
        session.add(Shot(
            storyboard_id=board.id, index=i,
            start=i * duration / shots, end=(i + 1) * duration / shots,
            telop=telop, narration="ナレ",
        ))
    render = Render(storyboard_id=board.id, path="/tmp/x.mp4", duration_sec=duration)
    session.add(render)
    session.flush()
    row = Publication(
        project_id=project.id, render_id=render.id, script_id=script.id,
        platform=Platform.YOUTUBE, status=PublicationStatus.PUBLISHED,
        external_id=ident, published_at=published,
    )
    session.add(row)
    session.flush()
    session.add(MetricSnapshot(
        publication_id=row.id, views=10000, likes=int(10000 * engagement),
        retention_rate=retention, captured_at=published + timedelta(hours=24),
    ))
    session.flush()
    return row


class TestFeatureExtraction:
    def test_the_hook_is_classified_with_the_same_rules_as_competitors(
        self, session, project
    ):
        row = publish(session, project, hook="なぜ朝食を抜くと太るのか？",
                      engagement=0.1, ident="q1")
        assert features_of(row)["hook_type"] == "question"

    def test_choices_come_back_as_bands_not_raw_numbers(self, session, project):
        """At these sample sizes, 28.4s versus 31.2s is not a difference."""
        row = publish(session, project, hook="断言します", engagement=0.05,
                      duration=25.0, shots=4, ident="b1")
        features = features_of(row)
        assert features["duration_band"] == "16〜30秒"
        assert features["shot_count_band"] == "4カット以下"
        assert features["has_narration"] == "あり"


class TestRefusal:
    def test_too_few_posts_produces_no_guidance_at_all(self, session, project):
        for i in range(MIN_POSTS - 1):
            publish(session, project, hook="なぜ？", engagement=0.1, ident=f"a{i}")

        playbook = build(session, project.id, Platform.YOUTUBE)
        assert not playbook.usable
        assert str(MIN_POSTS) in playbook.note
        # Nothing reaches the generator: guesses would be followed as
        # confidently as measurements.
        assert to_prompt(playbook) == ""

    def test_noise_produces_no_findings(self, session, project):
        """Twelve posts whose hook type has nothing to do with the outcome."""
        import random

        rng = random.Random(3)
        for i in range(12):
            publish(session, project,
                    hook="なぜ？" if i % 2 else "断言します。",
                    engagement=rng.uniform(0.03, 0.09), ident=f"n{i}")

        playbook = build(session, project.id, Platform.YOUTUBE)
        assert playbook.findings == []
        assert "見つかりませんでした" in playbook.note
        assert to_prompt(playbook) == ""

    def test_a_value_seen_once_cannot_be_compared(self, session, project):
        for i in range(8):
            publish(session, project, hook="断言します。", engagement=0.05, ident=f"s{i}")
        publish(session, project, hook="なぜ？", engagement=0.9, ident="lucky")

        playbook = build(session, project.id, Platform.YOUTUBE)
        hooks = [f for f in playbook.findings if f.feature == "hook_type"]
        assert all(f.sample >= MIN_PER_VALUE for f in hooks)
        assert "question" not in [f.value for f in hooks]

    def test_immature_posts_are_not_counted(self, session, project):
        for i in range(8):
            row = publish(session, project, hook="なぜ？", engagement=0.1, ident=f"f{i}")
            row.published_at = NOW - timedelta(hours=2)
        session.flush()
        assert build(session, project.id, Platform.YOUTUBE).sample == 0


class TestDiscovery:
    def _account_where_questions_win(self, session, project):
        for i, e in enumerate([0.11, 0.12, 0.10, 0.115]):
            publish(session, project, hook="なぜ朝食を抜くと太るのか？",
                    engagement=e, retention=0.62, ident=f"q{i}")
        for i, e in enumerate([0.04, 0.045, 0.038, 0.042]):
            publish(session, project, hook="朝食を抜くと太ります。",
                    engagement=e, retention=0.31, ident=f"s{i}")
        return build(session, project.id, Platform.YOUTUBE)

    def test_a_real_pattern_is_found(self, session, project):
        playbook = self._account_where_questions_win(session, project)
        assert playbook.usable
        assert any(f.feature == "hook_type" and f.value == "question"
                   for f in playbook.findings)

    def test_every_finding_carries_its_sample(self, session, project):
        for finding in self._account_where_questions_win(session, project).findings:
            assert finding.sample >= MIN_PER_VALUE
            assert finding.confident
            assert str(finding.sample) in finding.sentence()

    def test_the_prompt_tells_the_generator_to_prefer_it_over_competitors(
        self, session, project
    ):
        prompt = to_prompt(self._account_where_questions_win(session, project))
        assert "このアカウントの実績" in prompt
        assert "競合の構成より、こちらを優先" in prompt
        assert "question" in prompt

    def test_only_positive_findings_are_promoted(self, session, project):
        """"X does worse" is true but is not an instruction."""
        playbook = self._account_where_questions_win(session, project)
        assert all(f.better_by > 0 for f in playbook.findings)


class TestGeneratorIntegration:
    class StubLLM:
        def __init__(self):
            self.prompts = []

        def write_script(self, *, keyword, platform, duration, brand_profile, research):
            self.prompts.append(keyword)
            return {"title": "t", "hook": "独自のフック", "body": "b", "cta": "c",
                    "lines": [{"index": 0, "start": 0.0, "end": 10.0,
                               "telop": "独自", "narration": "独自"}],
                    "hashtags": [], "rationale": "r"}

    def test_the_playbook_reaches_the_generator(self, session, project):
        from snsauto.creative.script import ScriptService

        for i, e in enumerate([0.11, 0.12, 0.10, 0.115]):
            publish(session, project, hook="なぜ朝食を抜くと太るのか？",
                    engagement=e, retention=0.62, ident=f"q{i}")
        for i, e in enumerate([0.04, 0.045, 0.038, 0.042]):
            publish(session, project, hook="朝食を抜くと太ります。",
                    engagement=e, retention=0.31, ident=f"s{i}")

        llm = self.StubLLM()
        script = ScriptService(session, llm).generate(
            project, "朝食", Platform.YOUTUBE, 30.0
        )
        assert "このアカウントの実績" in llm.prompts[0]
        # Recorded on the script, so it can be read back against the evidence.
        assert script.playbook["sample"] == 8
        assert script.playbook["applied"]

    def test_a_new_account_gets_no_playbook_and_still_generates(
        self, session, project
    ):
        from snsauto.creative.script import ScriptService

        llm = self.StubLLM()
        script = ScriptService(session, llm).generate(
            project, "朝食", Platform.YOUTUBE, 30.0
        )
        assert "このアカウントの実績" not in llm.prompts[0]
        assert script.playbook["sample"] == 0
        assert script.hook
