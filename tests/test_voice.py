"""Narration synthesis and timing realignment."""

import pytest

from snsauto.config import Settings
from snsauto.creative.script import ScriptService
from snsauto.creative.storyboard import StoryboardService
from snsauto.creative.voice import (
    VoiceService, build_tts_provider, estimate_duration, suffix_for,
)
from snsauto.media.ffmpeg import probe, run_ffmpeg
from snsauto.models import Platform


class ToneTts:
    """Real audio whose length tracks the text, like a real synthesiser."""

    def __init__(self, chars_per_sec=6.5):
        self.chars_per_sec = chars_per_sec
        self.calls = 0

    def synthesize(self, out_stem, text):
        from pathlib import Path

        self.calls += 1
        seconds = max(0.6, len("".join(text.split())) / self.chars_per_sec)
        dest = Path(out_stem).with_suffix(".m4a")
        run_ffmpeg(["-y", "-f", "lavfi", "-t", f"{seconds:.3f}",
                    "-i", "sine=frequency=320:sample_rate=44100",
                    "-c:a", "aac", str(dest)])
        return dest


@pytest.fixture
def board(session, project):
    script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 18.0)
    return StoryboardService(session).generate(script)


class TestEstimate:
    def test_longer_text_takes_longer(self):
        assert estimate_duration("短い") < estimate_duration("こちらはずっと長い文章です")

    def test_empty_is_zero(self):
        assert estimate_duration("") == 0.0

    def test_whitespace_is_not_counted(self):
        assert estimate_duration("a b c") == estimate_duration("abc")

    def test_speed_shortens_it(self):
        # Long enough to clear the MIN_LINE_SEC floor, which would otherwise
        # make both readings identical.
        text = "これは速度の効果を確認するための十分に長いナレーション文章です"
        assert estimate_duration(text, speed=2.0) < estimate_duration(text)

    def test_never_returns_an_unusably_short_beat(self):
        """A 0.2s cut is not watchable, so very short lines get a floor."""
        assert estimate_duration("あ", speed=4.0) >= 0.8


@pytest.mark.parametrize("content_type,expected", [
    ("audio/mpeg", ".mp3"), ("audio/wav; codecs=1", ".wav"),
    ("audio/mp4", ".m4a"), ("audio/ogg", ".ogg"), ("application/json", ".mp3"),
])
def test_suffix_follows_the_response_not_a_guess(content_type, expected):
    assert suffix_for(content_type) == expected


def test_provider_is_none_when_unconfigured():
    assert build_tts_provider(Settings(_env_file=None)) is None


class TestRealignment:
    def test_runs_without_a_provider(self, session, board):
        """Timings improve from the estimate even with no API key."""
        service = VoiceService(session, Settings(_env_file=None), provider=None)
        track = service.narrate(board, "/tmp/does-not-matter")
        assert not track.synthesized
        assert track.path is None
        assert all(s.end > s.start for s in board.shots)

    def test_shots_stay_contiguous(self, session, board, tmp_path):
        VoiceService(session, Settings(_env_file=None), provider=ToneTts()).narrate(board, tmp_path)
        for a, b in zip(board.shots, board.shots[1:]):
            assert a.end == pytest.approx(b.start)
        assert board.shots[0].start == 0.0

    def test_no_shot_is_shorter_than_its_speech(self, session, board, tmp_path):
        track = VoiceService(session, Settings(_env_file=None), provider=ToneTts()).narrate(
            board, tmp_path
        )
        spoken = {line.index: line.duration for line in track.lines}
        for shot in board.shots:
            assert shot.end - shot.start >= spoken.get(shot.index, 0.0)

    def test_slow_speech_stretches_the_video(self, session, board, tmp_path):
        original = board.shots[-1].end
        # A deliberately slow voice must push the timeline out, not clip itself.
        VoiceService(session, Settings(_env_file=None), provider=ToneTts(chars_per_sec=2.0)).narrate(
            board, tmp_path
        )
        assert board.shots[-1].end > original


@pytest.mark.slow
class TestTrack:
    def test_audio_length_matches_the_timeline(self, session, board, tmp_path):
        """The classic desync bug: speech concatenated end-to-end drifts early."""
        track = VoiceService(session, Settings(_env_file=None), provider=ToneTts()).narrate(
            board, tmp_path
        )
        assert track.path is not None
        assert probe(track.path)["duration"] == pytest.approx(board.shots[-1].end, abs=0.15)

    def test_one_file_per_narrated_shot(self, session, board, tmp_path):
        tts = ToneTts()
        track = VoiceService(session, Settings(_env_file=None), provider=tts).narrate(
            board, tmp_path
        )
        narrated = [s for s in board.shots if (s.narration or "").strip()]
        assert tts.calls == len(narrated)
        assert len([line for line in track.lines if line.synthesized]) == len(narrated)

    def test_synthesis_failure_falls_back_to_the_estimate(self, session, board, tmp_path):
        class Broken:
            def synthesize(self, out_stem, text):
                raise OSError("provider down")

        track = VoiceService(session, Settings(_env_file=None), provider=Broken()).narrate(
            board, tmp_path
        )
        assert not track.synthesized
        assert all(line.duration >= 0 for line in track.lines)
        assert all(s.end > s.start for s in board.shots)
