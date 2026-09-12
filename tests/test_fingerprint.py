"""Audio fingerprinting: does the same track recur across posts?

The fixtures are generated melodies rather than real music, so these tests
prove the mechanism separates tracks and tolerates the distortions that matter
- a different start point, a different volume, narration over the bed. They do
not prove the threshold holds on a real music library, and the module says so.
"""

import itertools
import subprocess

import pytest

from snsauto.media.ffmpeg import ffmpeg_path
from snsauto.research.fingerprint import (
    MIN_OVERLAP_WINDOWS,
    SAME_SOUND_THRESHOLD,
    Fingerprint,
    cluster,
    fingerprint,
    recurring_sounds,
    similarity,
)

TRACK_A = [262, 330, 392, 523, 392, 330, 294, 349, 440, 392, 330, 262,
           294, 330, 392, 440, 523, 494, 440, 392, 349, 330, 294, 262,
           330, 392, 440, 523, 587, 523, 440, 392]
TRACK_B = [880, 784, 698, 659, 587, 523, 587, 659, 698, 784, 880, 988,
           880, 784, 698, 659, 523, 587, 659, 698, 784, 698, 659, 587,
           494, 523, 587, 659, 587, 523, 494, 440]


def _run(args):
    subprocess.run([ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True)


def _melody(path, notes, seg=1.25):
    """A time-varying signal. A steady tone would match anything."""
    inputs, chains, labels = [], [], []
    for i, freq in enumerate(notes):
        inputs += ["-f", "lavfi", "-i", f"sine=f={freq}:d={seg}"]
        chains.append(
            f"[{i}:a]afade=t=in:d=0.05,afade=t=out:st={seg - 0.15}:d=0.15[n{i}]"
        )
        labels.append(f"[n{i}]")
    graph = ";".join(chains) + ";" + "".join(labels) + f"concat=n={len(notes)}:v=0:a=1"
    _run(inputs + ["-filter_complex", graph, str(path)])


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    _melody(d / "trackA.wav", TRACK_A)
    _melody(d / "trackB.wav", TRACK_B)

    _run(["-ss", "0", "-i", str(d / "trackA.wav"), "-t", "20", str(d / "A_at0.wav")])
    _run(["-ss", "6", "-i", str(d / "trackA.wav"), "-t", "20", str(d / "A_at6.wav")])
    _run(["-i", str(d / "A_at6.wav"), "-af", "volume=-9dB", str(d / "A_quiet.wav")])
    _run(["-i", str(d / "A_at0.wav"), "-f", "lavfi", "-i", "anoisesrc=d=20:c=pink",
          "-filter_complex",
          "[1:a]highpass=f=300,lowpass=f=3400,volume=0.5[v];[0:a][v]amix=inputs=2",
          "-t", "20", str(d / "A_speech.wav")])
    _run(["-ss", "3", "-i", str(d / "trackB.wav"), "-t", "20", str(d / "B_at3.wav")])
    _run(["-ss", "9", "-i", str(d / "trackB.wav"), "-t", "20", str(d / "B_at9.wav")])

    names = ["A_at0", "A_at6", "A_quiet", "A_speech", "B_at3", "B_at9"]
    return d, {n: fingerprint(d / f"{n}.wav", key=n) for n in names}


class TestSameTrack:
    def test_a_different_start_point_still_matches(self, clips):
        _, fp = clips
        result = similarity(fp["A_at0"], fp["A_at6"])
        assert result["same_sound"]
        # Two creators rarely start a song at the same second, so the offset
        # has to be recovered rather than assumed away.
        assert abs(abs(result["offset_sec"]) - 6.0) < 1.0

    def test_a_volume_difference_does_not_break_it(self, clips):
        _, fp = clips
        assert similarity(fp["A_at6"], fp["A_quiet"])["score"] > 0.95

    def test_narration_over_the_bed_still_matches(self, clips):
        """The hardest same-track case, and the one that happens most."""
        _, fp = clips
        assert similarity(fp["A_at0"], fp["A_speech"])["same_sound"]


class TestDifferentTracks:
    def test_unrelated_tracks_do_not_match(self, clips):
        _, fp = clips
        for a, b in [("A_at0", "B_at3"), ("A_at6", "B_at9"), ("A_quiet", "B_at3")]:
            assert not similarity(fp[a], fp[b])["same_sound"]

    def test_a_negative_correlation_is_a_mismatch_not_a_failure(self, clips):
        """Cosine similarity is legitimately negative for unrelated audio."""
        _, fp = clips
        result = similarity(fp["A_at0"], fp["B_at3"])
        assert result["comparable"] is True
        assert result["same_sound"] is False


class TestSeparation:
    def test_the_two_populations_do_not_overlap(self, clips):
        _, fp = clips
        same, different = [], []
        for a, b in itertools.combinations(fp, 2):
            score = similarity(fp[a], fp[b])["score"]
            (same if a[0] == b[0] else different).append(score)
        # The threshold has to sit in a real gap, not be picked to fit.
        assert min(same) > max(different)
        assert max(different) < SAME_SOUND_THRESHOLD < min(same)


class TestClustering:
    def test_clips_group_by_the_track_they_use(self, clips):
        _, fp = clips
        groups = cluster(list(fp.values()))
        by_track = {frozenset(g) for g in groups}
        assert frozenset({"A_at0", "A_at6", "A_quiet", "A_speech"}) in by_track
        assert frozenset({"B_at3", "B_at9"}) in by_track

    def test_recurring_sounds_reports_the_shared_groups(self, clips):
        d, _ = clips
        result = recurring_sounds({
            n: d / f"{n}.wav"
            for n in ("A_at0", "A_at6", "B_at3", "B_at9")
        })
        assert result["usable"]
        assert result["posts_sharing_audio"] == 4
        assert len(result["shared_groups"]) == 2
        # It must never imply it can name the track.
        assert "音源名は特定できません" in result["note"]


class TestHonestRefusal:
    def test_one_video_cannot_show_a_recurrence(self, clips):
        d, _ = clips
        result = recurring_sounds({"A_at0": d / "A_at0.wav"})
        assert not result["usable"]
        assert "2本未満" in result["reason"]

    def test_a_missing_file_is_recorded_not_raised(self, clips):
        d, _ = clips
        result = recurring_sounds({
            "ok1": d / "A_at0.wav", "ok2": d / "A_at6.wav", "gone": d / "nope.wav",
        })
        assert result["usable"]
        assert "gone" in result["failed"]

    def test_audio_too_short_to_compare_says_so(self):
        short = Fingerprint(key="s", windows=[[0.0] * 8] * (MIN_OVERLAP_WINDOWS - 5))
        assert not similarity(short, short)["comparable"]
