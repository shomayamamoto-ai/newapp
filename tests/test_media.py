import pytest

from snsauto.media.subtitles import (
    TelopCue,
    TelopStyle,
    build_ass,
    display_width,
    wrap_text,
)
from snsauto.platforms.youtube import parse_iso8601_duration


class TestWrapText:
    def test_cjk_counts_double_width(self):
        assert display_width("日本語") == 6
        assert display_width("abc") == 3

    def test_wraps_japanese_without_spaces(self):
        lines = wrap_text("これは日本語のテロップです。改行を確認します。", 10)
        assert len(lines) > 1
        assert all(display_width(l) <= 12 for l in lines)

    def test_never_starts_a_line_with_closing_punctuation(self):
        for line in wrap_text("テストです。もう一度テストです。さらに続きます。", 8):
            assert line[0] not in "、。」）"

    def test_never_ends_a_line_with_opening_bracket(self):
        for line in wrap_text("説明文「引用された言葉」が続く長い文章です", 6):
            assert line[-1] not in "「（"

    def test_respects_explicit_newlines(self):
        assert wrap_text("first\nsecond", 50) == ["first", "second"]

    def test_empty_input_yields_one_empty_line(self):
        assert wrap_text("") == [""]


class TestAss:
    def test_dialogue_per_cue(self):
        doc = build_ass([TelopCue(0, 2, "A"), TelopCue(2, 4, "B")], TelopStyle())
        assert doc.count("Dialogue:") == 2

    def test_skips_zero_and_negative_length_cues(self):
        doc = build_ass([TelopCue(1, 1, "zero"), TelopCue(3, 2, "reversed")], TelopStyle())
        assert "Dialogue:" not in doc

    def test_skips_blank_text(self):
        assert "Dialogue:" not in build_ass([TelopCue(0, 2, "   ")], TelopStyle())

    def test_time_format_is_centiseconds(self):
        doc = build_ass([TelopCue(3661.5, 3662.0, "x")], TelopStyle())
        assert "1:01:01.50" in doc

    def test_escapes_ass_override_braces(self):
        """Unescaped braces would be parsed as style overrides, not shown."""
        doc = build_ass([TelopCue(0, 2, "{\\b1}fake")], TelopStyle())
        assert "\\{" in doc

    def test_playres_matches_requested_frame(self):
        doc = build_ass([TelopCue(0, 1, "x")], TelopStyle(), 1080, 1920)
        assert "PlayResX: 1080" in doc and "PlayResY: 1920" in doc

    def test_long_text_becomes_multiline(self):
        doc = build_ass([TelopCue(0, 3, "とても長い日本語のテロップ文章です")], TelopStyle(max_width=8))
        assert "\\N" in doc


@pytest.mark.parametrize(
    "value,expected",
    [("PT1M30S", 90.0), ("PT45S", 45.0), ("PT1H2M3S", 3723.0),
     ("P1DT2H", 93600.0), (None, None), ("garbage", None)],
)
def test_iso8601_duration(value, expected):
    assert parse_iso8601_duration(value) == expected
