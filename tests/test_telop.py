"""Telop reading: sampling, clustering, and the reader-selection rules."""

from pathlib import Path

import pytest

from snsauto.research.telop import (
    MIN_EVENT_CHARS,
    TelopRead,
    TesseractReader,
    _char_count,
    _position,
    _similar,
    build_events,
    confidence_band,
    merge_into_profile,
    sample_times,
    summarize_events,
)


def read(text, y0=0.4, y1=0.6, confidence=90.0):
    return TelopRead(text=text, bbox=(0.1, y0, 0.9, y1), confidence=confidence)


class TestSampling:
    def test_a_frame_is_taken_just_after_every_cut(self):
        times = sample_times(10.0, cuts=[3.0, 6.0], interval=2.0)
        # A telop card usually appears with the cut; a pure grid can straddle it.
        assert any(3.0 < t < 3.5 for t in times)
        assert any(6.0 < t < 6.5 for t in times)

    def test_the_frame_budget_thins_evenly_instead_of_truncating(self):
        times = sample_times(60.0, interval=0.5, max_frames=10)
        assert len(times) == 10
        # Late telop must survive the thinning, so the last sample stays late.
        assert times[-1] > 50.0

    def test_a_zero_length_video_samples_nothing(self):
        assert sample_times(0.0) == []


class TestCardIdentity:
    def test_a_partial_read_is_the_same_card(self):
        # OCR drops the trailing line on some frames of a held card.
        assert _similar("3ヶ月で10kg痩せた結果", "3ヶ月で10kg痩")

    def test_two_different_cards_are_not_merged(self):
        assert not _similar("朝食を抜くのはNG", "タンパク質を毎食20g")

    def test_whitespace_and_line_breaks_do_not_matter(self):
        assert _similar("保存して明日から\n試して", "保存して明日から試して")


class TestEventBuilding:
    def test_consecutive_frames_of_one_card_become_one_event(self):
        reads = [(0.0, read("同じテロップ")), (0.8, read("同じテロップ")),
                 (1.6, read("同じテロップ"))]
        events = build_events(reads, duration=3.0, interval=0.8)
        assert len(events) == 1
        assert events[0].start == 0.0
        assert events[0].end == pytest.approx(2.4)

    def test_one_unreadable_frame_does_not_split_a_card(self):
        # Without stitching this would report two 0.8s events instead of one
        # 2.4s event, halving avg_hold_sec and doubling events_per_min.
        reads = [(0.0, read("テロップ")), (0.8, None), (1.6, read("テロップ"))]
        events = build_events(reads, duration=3.0, interval=0.8)
        assert len(events) == 1
        assert events[0].hold_sec == pytest.approx(2.4)

    def test_the_fullest_read_of_a_card_is_the_one_kept(self):
        reads = [(0.0, read("3ヶ月で10kg")), (0.8, read("3ヶ月で10kg痩せた結果"))]
        events = build_events(reads, duration=2.0, interval=0.8)
        assert events[0].text == "3ヶ月で10kg痩せた結果"

    def test_events_never_run_past_the_end_of_the_video(self):
        events = build_events([(4.8, read("最後"))], duration=5.0, interval=0.8)
        assert events[0].end <= 5.0


class TestPosition:
    @pytest.mark.parametrize("y0,y1,expected", [
        (0.05, 0.15, "top"), (0.45, 0.55, "middle"), (0.80, 0.92, "bottom"),
    ])
    def test_position_comes_from_the_box_centre(self, y0, y1, expected):
        assert _position((0.1, y0, 0.9, y1)) == expected


class TestSummary:
    def test_the_measured_numbers_agree_with_each_other(self):
        reads = [(0.0, read("あいうえお")), (2.0, read("かきくけこさしすせそ"))]
        events = build_events(reads, duration=10.0, interval=2.0)
        summary = summarize_events(events, duration=10.0, reader="tesseract")

        assert summary["event_count"] == 2
        # coverage is the share of the video with telop up: 2 events x 2s / 10s
        assert summary["coverage_ratio"] == pytest.approx(0.4)
        assert summary["first_telop_sec"] == 0.0
        assert summary["chars_per_sec"] == pytest.approx(1.5)
        assert summary["avg_chars"] == pytest.approx(7.5)

    def test_no_telop_is_reported_as_zero_not_as_missing(self):
        summary = summarize_events([], duration=10.0, reader="tesseract")
        assert summary["event_count"] == 0
        assert summary["coverage_ratio"] == 0.0
        assert confidence_band(summary) == "no-telop-detected"

    def test_a_run_with_no_reader_is_never_reported_as_a_measurement(self):
        assert confidence_band({"reader": "none"}) == "unavailable"
        assert confidence_band({"reader": None}) == "unavailable"

    def test_low_ocr_confidence_is_surfaced_not_hidden(self):
        events = build_events([(0.0, read("あいう", confidence=60.0))], 5.0, 1.0)
        summary = summarize_events(events, duration=5.0, reader="tesseract")
        assert confidence_band(summary) == "low"


class TestProfileMerge:
    def test_caption_statistics_are_never_presented_as_telop(self):
        caption = {"line_count": 3, "avg_chars": 20.0, "max_chars": 40,
                   "chars_per_sec": 2.0, "hashtags": ["ダイエット"]}
        merged = merge_into_profile(caption, {"reader": "none"})

        # The old keys must not survive at the top level, where every existing
        # reader of this dict treats them as on-screen text.
        for key in ("line_count", "avg_chars", "max_chars", "chars_per_sec"):
            assert key not in merged
        assert merged["caption"]["avg_chars"] == 20.0
        assert merged["onscreen"]["reader"] == "none"
        assert merged["hashtags"] == ["ダイエット"]


class TestTesseractReaderSelection:
    """psm 6 leads; the fallback is chosen on length, never on confidence."""

    class FakeReads:
        def __init__(self, by_psm):
            self.by_psm = by_psm
            self.calls = []

    def _reader(self, by_psm):
        reader = TesseractReader()
        fake = self.FakeReads(by_psm)

        def _read_with(path, psm):
            fake.calls.append(psm)
            return fake.by_psm.get(psm)

        reader._read_with = _read_with
        return reader, fake

    def test_a_good_primary_read_short_circuits_the_fallback(self):
        reader, fake = self._reader({6: read("朝食を抜くのはNG")})
        assert reader.read(Path("x.png")).text == "朝食を抜くのはNG"
        assert fake.calls == [6]

    def test_a_confident_but_truncated_read_loses_to_a_longer_one(self):
        # Measured against a real frame: psm 11 returned のは at confidence 97
        # while psm 6 returned the whole card at 96. Picking on confidence
        # would choose the wrong one, so length decides.
        reader, _ = self._reader({
            6: read("のは", confidence=97.0),
            11: read("朝食を抜くのはNG", confidence=96.0),
        })
        assert reader.read(Path("x.png")).text == "朝食を抜くのはNG"

    def test_the_fallback_runs_when_the_primary_finds_almost_nothing(self):
        reader, fake = self._reader({6: None, 11: read("タンパク質を毎食20g")})
        assert reader.read(Path("x.png")).text == "タンパク質を毎食20g"
        assert fake.calls == [6, 11]

    def test_both_readers_failing_returns_nothing(self):
        reader, _ = self._reader({})
        assert reader.read(Path("x.png")) is None


def test_a_single_character_is_treated_as_noise():
    assert MIN_EVENT_CHARS == 2
    assert _char_count("4") < MIN_EVENT_CHARS
