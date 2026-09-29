"""表記ゆれの統一。

指示文に必ず入る「『アストラ』は『Astra』と表記」を機械に任せる部分。
置換そのものは単純で、危険なのは**巻き込み**のほう。
"""

import pytest

from snsauto.creative.notation import NotationRules


@pytest.fixture
def rules():
    return NotationRules(
        {"アストラ": "Astra", "プレミア": "Premiere", "プレミアプロ": "Premiere Pro"},
        exceptions=["プレミアリーグ"],
    )


class TestReplacement:
    def test_it_replaces_what_the_dictionary_names(self, rules):
        assert rules.apply("アストラで編集する").text == "Astraで編集する"

    def test_the_longer_entry_wins(self, rules):
        """「プレミア」を先に当てると「Premiereプロ」になって壊れる。"""
        assert rules.apply("プレミアプロで書き出す").text == "Premiere Proで書き出す"

    def test_text_already_in_the_target_form_is_left_alone(self, rules):
        assert rules.apply("Astra は Astra のまま").text == "Astra は Astra のまま"

    def test_every_occurrence_is_replaced(self, rules):
        result = rules.apply("アストラとアストラ")
        assert result.text == "AstraとAstra"
        assert len(result.replacements) == 2


class TestTheDangerousCase:
    """「プレミア」→「Premiere」を入れた状態で「プレミアリーグ」と書いたとき。"""

    def test_an_exception_is_not_touched(self, rules):
        assert rules.apply("プレミアリーグの話").text == "プレミアリーグの話"

    def test_an_exception_does_not_disable_the_rest_of_the_line(self, rules):
        result = rules.apply("アストラとプレミアリーグとプレミア")
        assert result.text == "AstraとプレミアリーグとPremiere"

    def test_without_the_exception_it_would_have_been_replaced(self):
        """除外語が効いていることを、効かない側と比べて確かめる。"""
        naive = NotationRules({"プレミア": "Premiere"})
        assert naive.apply("プレミアリーグ").text == "Premiereリーグ"


class TestItReportsWhatItDid:
    """辞書の事故は書いた本人しか気づけないので、履歴が要る。"""

    def test_each_replacement_is_recorded_with_its_position(self, rules):
        result = rules.apply("今日はアストラの話")
        assert [(r.source, r.target) for r in result.replacements] == [("アストラ", "Astra")]
        assert result.replacements[0].index == 3

    def test_an_untouched_string_reports_nothing(self, rules):
        result = rules.apply("表記指定に無い言葉")
        assert not result.changed
        assert result.summary_ja() == "表記の置換はありません"

    def test_the_summary_counts_by_pair(self, rules):
        summary = rules.apply("アストラとアストラとプレミア").summary_ja()
        assert "「アストラ」→「Astra」2件" in summary
        assert "「プレミア」→「Premiere」1件" in summary


class TestEmptyAndBroken:
    def test_no_dictionary_means_no_change(self):
        rules = NotationRules()
        assert not rules
        assert rules.apply("そのまま").text == "そのまま"

    def test_an_entry_that_maps_to_itself_is_dropped(self):
        """辞書に書かれていても無意味な組は持たない。無限ループの元になる。"""
        assert NotationRules({"Astra": "Astra"}).mapping == {}

    def test_blank_entries_are_dropped(self):
        assert NotationRules({"": "x", "y": "  "}).mapping == {}

    def test_none_and_empty_text_are_safe(self, rules):
        assert rules.apply(None).text == ""
        assert rules.apply("").text == ""


class TestScriptLines:
    def test_it_rewrites_telop_and_narration_in_place(self, rules):
        lines = [
            {"index": 0, "telop": "アストラで編集", "narration": "プレミアを使います"},
            {"index": 1, "telop": "関係ない行"},
        ]
        found = rules.apply_to_lines(lines)
        assert lines[0]["telop"] == "Astraで編集"
        assert lines[0]["narration"] == "Premiereを使います"
        assert lines[1]["telop"] == "関係ない行"
        assert len(found) == 2

    def test_a_line_without_the_fields_is_skipped(self, rules):
        lines = [{"index": 0}, {"index": 1, "telop": None}]
        assert rules.apply_to_lines(lines) == []


class TestProjectWiring:
    def test_the_dictionary_comes_from_the_brand_profile(self):
        class FakeProject:
            brand_profile = {
                "notation": {"アストラ": "Astra"},
                "notation_exceptions": ["アストラゼネカ"],
            }

        rules = NotationRules.from_project(FakeProject())
        assert rules.apply("アストラ").text == "Astra"
        assert rules.apply("アストラゼネカ").text == "アストラゼネカ"

    def test_a_project_without_one_is_inert(self):
        class Bare:
            brand_profile = {}

        assert not NotationRules.from_project(Bare())
