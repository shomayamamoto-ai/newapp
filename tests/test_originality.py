"""Checking a generated script against the competitor copy it was shown."""

import pytest

from snsauto.creative.originality import (
    RUN_THRESHOLD,
    avoid_instruction,
    check_line,
    longest_common_run,
    normalise,
    review,
    script_lines,
)

CORPUS = [
    "朝食を抜くと逆に太る理由",
    "3ヶ月で10kg痩せた結果、わかったこと",
    "タンパク質を毎食20g摂るだけで変わります",
    "保存して明日から試してみてください",
]


class TestWhatCountsAsALift:
    def test_a_verbatim_line_is_caught(self):
        assert check_line("朝食を抜くと逆に太る理由", CORPUS)

    def test_a_line_wrapped_around_a_lifted_phrase_is_caught(self):
        assert check_line("【衝撃】朝食を抜くと逆に太る理由とは？", CORPUS)

    def test_the_same_idea_in_different_words_is_not_a_lift(self):
        """Shared vocabulary is the topic. Shared phrasing is the problem."""
        assert check_line("朝ごはんを食べないと、かえって体重が増えます", CORPUS) is None

    def test_a_common_collocation_is_not_a_lift(self):
        assert check_line("朝食は大事です", CORPUS) is None
        assert check_line("タンパク質が大切", CORPUS) is None

    def test_an_unrelated_line_is_not_a_lift(self):
        assert check_line("夜のストレッチで睡眠の質が上がります", CORPUS) is None

    def test_it_reports_what_matched_and_how_much(self):
        finding = check_line("朝食を抜くと逆に太る理由", CORPUS)
        assert finding["shared"] == "朝食を抜くと逆に太る理由"
        assert finding["shared_length"] >= RUN_THRESHOLD
        assert finding["coverage"] == pytest.approx(1.0)


class TestNormalisation:
    def test_punctuation_and_width_do_not_hide_a_copy(self):
        # Changing 「」 and full-width characters is how a copy evades a naive
        # string comparison.
        assert check_line("「朝食を抜くと、逆に太る理由」！", CORPUS)

    def test_case_is_folded(self):
        assert normalise("ABC def") == normalise("abc DEF")


class TestLongestCommonRun:
    def test_it_returns_the_shared_stretch(self):
        assert longest_common_run("あいうえおかきく", "うえおかき") == "うえおかき"

    def test_no_overlap_is_empty(self):
        assert longest_common_run("あいう", "かきく") == ""

    def test_an_empty_side_is_empty(self):
        assert longest_common_run("", "かきく") == ""


class TestReview:
    def _script(self, hook):
        return {
            "title": "テスト台本", "hook": hook, "body": "本文",
            "cta": "フォローしてください",
            "lines": [{"telop": "独自のテロップ", "narration": "独自のナレーション"}],
        }

    def test_a_clean_script_passes(self):
        result = review(self._script("朝ごはんを抜くと何が起きるか"), CORPUS)
        assert result["checked"] and result["clean"]

    def test_a_lifted_hook_is_reported(self):
        result = review(self._script("朝食を抜くと逆に太る理由"), CORPUS)
        assert not result["clean"]
        assert result["findings"][0]["shared"] == "朝食を抜くと逆に太る理由"

    def test_with_no_corpus_it_says_it_did_not_check(self):
        """"Not checked" and "checked and clean" must stay distinguishable."""
        result = review(self._script("何か"), [])
        assert result["checked"] is False
        assert result["findings"] == []

    def test_every_visible_line_is_examined(self):
        data = self._script("独自のフック")
        data["lines"] = [{"telop": "保存して明日から試してみてください",
                          "narration": "独自のナレーション"}]
        assert not review(data, CORPUS)["clean"]

    def test_script_lines_covers_telop_and_narration(self):
        lines = script_lines(self._script("フック"))
        assert "独自のテロップ" in lines and "独自のナレーション" in lines


class TestRetryInstruction:
    def test_it_names_the_exact_phrases_to_avoid(self):
        """"Be more original" does not tell a model what to change."""
        findings = review(
            {"hook": "朝食を抜くと逆に太る理由", "lines": []}, CORPUS
        )["findings"]
        instruction = avoid_instruction(findings)
        assert "朝食を抜くと逆に太る理由" in instruction
        # Structure is legitimately copyable; wording is not.
        assert "構成" in instruction


class TestCorpusFromRun:
    def test_it_reads_titles_captions_and_telop(self, session, project):
        from snsauto.creative.originality import corpus_from_run
        from snsauto.models import (
            CompetitorPost, Platform, ResearchRun, StructureAnalysis,
        )

        run = ResearchRun(project_id=project.id, keyword="k", platform=Platform.YOUTUBE)
        session.add(run)
        session.flush()
        post = CompetitorPost(
            run_id=run.id, external_id="v1", platform=Platform.YOUTUBE, rank=1,
            title="競合のタイトル", caption="競合のキャプション",
        )
        post.structure = StructureAnalysis(telop={
            "onscreen": {"events": [{"text": "画面内のテロップ"}]}
        })
        session.add(post)
        session.flush()

        corpus = corpus_from_run(run)
        assert "競合のタイトル" in corpus
        assert "競合のキャプション" in corpus
        # The telop is the text most likely to be reproduced verbatim, because
        # it is the text the generator was shown as "what works".
        assert "画面内のテロップ" in corpus

    def test_no_run_is_an_empty_corpus(self):
        from snsauto.creative.originality import corpus_from_run

        assert corpus_from_run(None) == []


class TestRegeneration:
    """The generator is shown competitor copy on purpose, so it retries."""

    class StubLLM:
        def __init__(self, outputs):
            self.outputs = list(outputs)
            self.prompts = []

        def write_script(self, *, keyword, platform, duration, brand_profile, research):
            self.prompts.append(keyword)
            return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]

    def _script_data(self, hook):
        return {
            "title": "台本", "hook": hook, "body": "本文", "cta": "CTA",
            "lines": [{"index": 0, "start": 0.0, "end": 10.0,
                       "telop": hook, "narration": hook}],
            "hashtags": ["タグ"], "rationale": "r",
        }

    def _run_with(self, session, project, texts):
        from snsauto.models import CompetitorPost, Platform, ResearchRun

        run = ResearchRun(project_id=project.id, keyword="k", platform=Platform.YOUTUBE)
        session.add(run)
        session.flush()
        for i, text in enumerate(texts):
            session.add(CompetitorPost(
                run_id=run.id, external_id=f"v{i}", platform=Platform.YOUTUBE,
                rank=i + 1, title=text,
            ))
        session.flush()
        session.refresh(run)
        return run

    def test_a_lifted_script_is_regenerated(self, session, project):
        from snsauto.creative.script import ScriptService
        from snsauto.models import Platform

        llm = self.StubLLM([
            self._script_data("朝食を抜くと逆に太る理由"),      # a copy
            self._script_data("朝ごはんを飛ばすと何が起きるか"),  # clean
        ])
        run = self._run_with(session, project, ["朝食を抜くと逆に太る理由"])
        script = ScriptService(session, llm).generate(
            project, "朝食", Platform.YOUTUBE, 30.0, run=run
        )

        assert len(llm.prompts) == 2
        assert script.originality["clean"]
        assert script.hook == "朝ごはんを飛ばすと何が起きるか"

    def test_the_retry_carries_the_phrases_to_avoid(self, session, project):
        from snsauto.creative.script import ScriptService
        from snsauto.models import Platform

        llm = self.StubLLM([
            self._script_data("朝食を抜くと逆に太る理由"),
            self._script_data("別の言い回しにしました"),
        ])
        run = self._run_with(session, project, ["朝食を抜くと逆に太る理由"])
        ScriptService(session, llm).generate(
            project, "朝食", Platform.YOUTUBE, 30.0, run=run
        )
        assert "朝食を抜くと逆に太る理由" in llm.prompts[1]

    def test_a_clean_first_attempt_is_not_regenerated(self, session, project):
        from snsauto.creative.script import ScriptService
        from snsauto.models import Platform

        llm = self.StubLLM([self._script_data("完全に独自のフックです")])
        run = self._run_with(session, project, ["朝食を抜くと逆に太る理由"])
        ScriptService(session, llm).generate(
            project, "朝食", Platform.YOUTUBE, 30.0, run=run
        )
        assert len(llm.prompts) == 1

    def test_a_model_that_keeps_copying_is_flagged_not_looped_forever(self, session, project):
        """After the retries are spent it has to be visible to a human."""
        from snsauto.creative.originality import MAX_REGENERATIONS
        from snsauto.creative.script import ScriptService
        from snsauto.models import Platform

        llm = self.StubLLM([self._script_data("朝食を抜くと逆に太る理由")])
        run = self._run_with(session, project, ["朝食を抜くと逆に太る理由"])
        script = ScriptService(session, llm).generate(
            project, "朝食", Platform.YOUTUBE, 30.0, run=run
        )

        assert len(llm.prompts) == MAX_REGENERATIONS + 1
        assert script.originality["exhausted"] is True
        assert not script.originality["clean"]
        # The evidence has to survive, not just the verdict.
        assert script.originality["findings"][0]["shared"]
