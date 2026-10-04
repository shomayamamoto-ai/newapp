import pytest

from snsauto.research.structure import (
    classify_hook,
    detect_cta,
    estimate_beats,
    extract_hashtags,
    telop_profile,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("なぜ9割の人が失敗するのか？", "question"),
        ("5つの神アプリを紹介します", "listicle"),
        ("7 Ways to Grow Fast", "listicle"),
        ("実は知られていない裏技", "curiosity"),
        ("今すぐやめて。危険です", "negative"),
        ("プロが教える正しい洗顔", "authority"),
        ("期限は今日まで", "urgency"),
        ("ただの説明文です", "statement"),
    ],
)
def test_classify_hook(text, expected):
    assert classify_hook(text)[0] == expected


def test_leading_number_alone_is_not_a_listicle():
    """'3ヶ月で10kg痩せた結果' starts with a digit but is a result hook."""
    assert classify_hook("3ヶ月で10kg痩せた結果")[0] == "result"


def test_classify_hook_handles_empty():
    assert classify_hook("")[0] == "unknown"
    assert classify_hook(None)[0] == "unknown"


def test_detect_cta_returns_last_matching_line():
    text = "保存してね\n本編の説明\nフォローで最新情報"
    assert detect_cta(text) == "フォローで最新情報"


def test_detect_cta_none_when_absent():
    assert detect_cta("ただの本文です") is None


def test_extract_hashtags_handles_japanese():
    assert extract_hashtags("最高 #副業 #お金の話 #Tips") == ["副業", "お金の話", "Tips"]


class TestBeats:
    def test_beats_tile_the_duration(self):
        beats = estimate_beats(30.0, "hook", "cta")
        assert beats[0]["start"] == 0.0
        assert beats[-1]["end"] == pytest.approx(30.0)
        for a, b in zip(beats, beats[1:]):
            assert a["end"] == pytest.approx(b["start"])

    def test_hook_lands_within_three_seconds(self):
        assert estimate_beats(60.0, "h", "c")[0]["end"] <= 3.0

    def test_short_durations_still_produce_ordered_beats(self):
        beats = estimate_beats(1.0, "h", "c")
        assert all(b["end"] > b["start"] for b in beats)


def test_telop_profile_computes_density():
    profile = telop_profile("あいうえお\nかきくけこさし", 10.0)
    assert profile["line_count"] == 2
    assert profile["max_chars"] == 7
    assert profile["chars_per_sec"] == pytest.approx(1.2)


def test_telop_profile_handles_missing_duration():
    assert telop_profile("text", None)["chars_per_sec"] is None
