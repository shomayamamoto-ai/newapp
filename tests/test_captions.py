"""Fitting captions to platform limits without corrupting them."""


from snsauto.platforms.base import PublishRequest
from snsauto.platforms.captions import truncate_caption, weighted_length


class TestWeightedLength:
    def test_japanese_weighs_double(self):
        """The bug this catches: a 243-character Japanese post weighs 480.

        Built to a 280-character limit it passes every local check and is then
        rejected by X - after the video has already uploaded.
        """
        assert weighted_length("あ" * 100) == 200
        assert weighted_length("a" * 100) == 100

    def test_a_url_counts_as_23_however_long(self):
        assert weighted_length("https://example.com/" + "a" * 300) == 23

    def test_empty_is_zero(self):
        assert weighted_length("") == 0


class TestTruncation:
    def test_a_hashtag_is_never_cut_in_half(self):
        # "#ダイエット" cut to "#ダイエ" files the post under a different tag.
        text = "本文です " + "あ" * 40 + " #ダイエット"
        assert "#ダイエ" not in truncate_caption(text, 50) or "#ダイエット" in truncate_caption(text, 50)

    def test_an_emoji_sequence_is_not_split(self):
        text = "テスト" + "👩‍👩‍👧‍👦" + "あ" * 30
        result = truncate_caption(text, 6)
        assert not result.endswith("‍")

    def test_text_that_already_fits_is_untouched(self):
        assert truncate_caption("短い本文", 100) == "短い本文"

    def test_weighted_mode_respects_x_counting(self):
        text = "あ" * 200
        assert weighted_length(truncate_caption(text, 280, weighted=True)) <= 280

    def test_no_limit_means_no_cut(self):
        assert truncate_caption("あ" * 500, None) == "あ" * 500

    def test_something_always_comes_back(self):
        assert truncate_caption("あいうえお", 2)


class TestFullCaption:
    def _request(self, caption, tags):
        return PublishRequest(video_path="v.mp4", caption=caption, hashtags=tags)

    def test_hashtags_survive_a_long_body(self):
        """Truncating the joined string drops the tags exactly when a post is
        long - which is when discovery matters most."""
        request = self._request("朝食を抜くと逆に太る理由を解説します。" * 12,
                                ["ダイエット", "朝食", "健康"])
        result = request.full_caption(limit=280, weighted=True)
        assert "#ダイエット" in result and "#健康" in result
        assert weighted_length(result) <= 280

    def test_a_short_post_keeps_everything(self):
        result = self._request("短い本文", ["タグ"]).full_caption(limit=280, weighted=True)
        assert result == "短い本文\n\n#タグ"

    def test_tags_already_prefixed_are_not_double_prefixed(self):
        result = self._request("本文", ["#すでに付いている"]).full_caption(limit=280)
        assert "##" not in result

    def test_when_the_tags_alone_exceed_the_limit_they_are_what_survives(self):
        request = self._request("本文", ["とても長いタグ" * 20])
        result = request.full_caption(limit=40, weighted=True)
        assert result.startswith("#")
        assert weighted_length(result) <= 40

    def test_no_limit_joins_body_and_tags(self):
        assert self._request("本文", ["タグ"]).full_caption() == "本文\n\n#タグ"
