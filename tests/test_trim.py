"""無音カットの判定。

合成音声を毎回その場で作って検証している。実素材を置くと差し替えたときに
何が変わったのか分からなくなるのに対し、合成なら**正解の時刻が既知**で、
「0.12秒の促音を残したか」を秒単位で確かめられる。

ここで固定している定数は、いずれも測って決めたもの。trim.py の docstring に
測定値の表がある。
"""

import subprocess

import pytest

from snsauto.media.ffmpeg import ffmpeg_path, probe
from snsauto.media.trim import (
    CUT, KEEP, apply_trim, detect_gaps, plan_trim, retake_candidates,
    speech_segments,
)

VOICE = "sine=frequency=220:sample_rate=48000:duration="
SILENCE = "anullsrc=r=48000:cl=mono:duration="
# 4.5kHz で4段切った白色雑音。speech帯には残らず高域だけ鳴るので、
# 無声摩擦音(/s/ /ʃ/)と同じ形になる。
FRICATIVE_FILTER = "highpass=f=4500,highpass=f=4500,highpass=f=4500,highpass=f=4500"


def _build(path, segments):
    """segments は ("voice"|"silence"|"fricative", 秒) の並び。"""
    inputs, filters = [], []
    for index, (kind, seconds) in enumerate(segments):
        if kind == "voice":
            source = VOICE + f"{seconds}"
        elif kind == "fricative":
            source = (f"anoisesrc=color=white:sample_rate=48000:"
                      f"amplitude=0.010:duration={seconds}")
        else:
            source = SILENCE + f"{seconds}"
        inputs += ["-f", "lavfi", "-i", source]
        chain = FRICATIVE_FILTER if kind == "fricative" else "anull"
        filters.append(f"[{index}:a]{chain}[s{index}]")
    graph = ";".join(filters) + ";" + "".join(f"[s{i}]" for i in range(len(segments)))
    graph += f"concat=n={len(segments)}:v=0:a=1[out]"
    subprocess.run(
        [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", graph, "-map", "[out]", str(path)],
        check=True,
    )
    return str(path)


@pytest.fixture
def utterance(tmp_path):
    """0.80 発話 / 0.12 促音 / 0.58 発話 / 0.70 間 / 0.80 発話 / 0.25 短い間 / 0.85 発話"""
    return _build(tmp_path / "u.wav", [
        ("voice", 0.80), ("silence", 0.12), ("voice", 0.58),
        ("silence", 0.70), ("voice", 0.80), ("silence", 0.25), ("voice", 0.85),
    ])


@pytest.fixture
def with_fricative(tmp_path):
    """0.90 発話 / 0.70 間 / 0.90 発話 / 0.70 子音入りの間 / 0.90 発話"""
    return _build(tmp_path / "f.wav", [
        ("voice", 0.90), ("silence", 0.70), ("voice", 0.90),
        ("fricative", 0.70), ("voice", 0.90),
    ])


class TestWhatIsNeverCut:
    """指示文の「単語の途中はカットせず、促音・子音・語頭・語尾を必ず残す」。"""

    def test_a_geminate_consonant_survives(self, utterance):
        """0.12秒の「ッ」。文の間と音量では区別がつかず、長さでしか守れない。"""
        plan = plan_trim(utterance)
        geminate = next(g for g in plan.gaps if abs(g.start - 0.80) < 0.05)
        assert geminate.decision == KEEP
        assert "促音" in geminate.reason

    def test_a_pause_under_the_floor_survives(self, utterance):
        short = next(g for g in plan_trim(utterance).gaps if abs(g.duration - 0.25) < 0.05)
        assert short.decision == KEEP

    def test_an_unvoiced_consonant_survives_a_long_gap(self, with_fricative):
        """長さだけでは切ってしまう箇所。

        同じ0.70秒でも、片方は子音が続いている。「音量だけで機械的に判断
        せず」がここで効く - speech帯はどちらも無音に見える。
        """
        plan = plan_trim(with_fricative)
        gaps = sorted(plan.gaps, key=lambda g: g.start)
        assert [g.duration for g in gaps] == pytest.approx([0.70, 0.70], abs=0.05)
        assert gaps[0].decision == CUT           # ただの間
        assert gaps[1].decision == KEEP          # 子音が鳴っている
        assert "子音" in gaps[1].reason

    def test_the_head_and_tail_are_not_phrase_gaps(self, tmp_path):
        """素材の頭と尻は「文やフレーズの間」ではないので、既定では触らない。"""
        path = _build(tmp_path / "h.wav", [
            ("silence", 0.80), ("voice", 1.00), ("silence", 0.80),
        ])
        plan = plan_trim(path)
        assert [g.decision for g in plan.gaps] == [KEEP, KEEP]
        assert plan.removed_sec == 0.0

    def test_trim_ends_is_opt_in(self, tmp_path):
        path = _build(tmp_path / "h.wav", [
            ("silence", 0.80), ("voice", 1.00), ("silence", 0.80),
        ])
        assert plan_trim(path, keep_head_tail=False).removed_sec > 0


class TestTheHandles:
    """「発話の前後には0.1秒程度の余白を残してください」。"""

    def test_a_cut_leaves_a_handle_on_each_side(self, utterance):
        plan = plan_trim(utterance)
        cut = plan.cuts[0]
        assert cut.cut_start == pytest.approx(cut.start + 0.10, abs=0.001)
        assert cut.cut_end == pytest.approx(cut.end - 0.10, abs=0.001)

    def test_only_the_middle_of_the_gap_is_removed(self, utterance):
        cut = plan_trim(utterance).cuts[0]
        assert cut.removed == pytest.approx(cut.duration - 0.20, abs=0.001)

    def test_a_gap_that_cannot_spare_two_handles_is_kept(self, tmp_path):
        """余白を引いて何も残らないなら切らない。テンポより明瞭さを優先する。"""
        path = _build(tmp_path / "n.wav", [("voice", 0.9), ("silence", 0.35), ("voice", 0.9)])
        plan = plan_trim(path, min_gap=0.30, handle=0.20)
        assert plan.gaps[0].decision == KEEP
        assert "余白" in plan.gaps[0].reason


class TestThePlan:
    def test_kept_segments_tile_the_result(self, utterance):
        plan = plan_trim(utterance)
        total = sum(end - start for start, end in plan.keep)
        assert total == pytest.approx(plan.result_sec, abs=0.001)

    def test_the_report_names_every_gap_it_kept(self, utterance):
        plan = plan_trim(utterance)
        report = plan.report_ja()
        for gap in plan.kept:
            assert f"{gap.start:.2f}" in report

    def test_a_clip_with_no_audio_is_refused_rather_than_passed_through(self, tmp_path):
        """音声が無ければ判断材料が無い。無言で素通しするのが一番まずい。"""
        from snsauto.media.ffmpeg import FFmpegError

        silent = tmp_path / "mute.mp4"
        subprocess.run(
            [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=s=320x240:r=15:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent)],
            check=True,
        )
        with pytest.raises(FFmpegError) as caught:
            plan_trim(silent)
        assert "音声" in str(caught.value)


class TestDetectionOnlyProposes:
    def test_ffmpeg_is_asked_for_gaps_below_our_own_floor(self, utterance):
        """ffmpeg の閾値をそのまま方針にすると、何を見送ったかが残らない。

        0.3秒未満の間も候補として出させ、こちら側で明示的に残している。
        """
        proposed = detect_gaps(utterance)
        assert any(end - start < 0.30 for start, end in proposed)
        assert any(gap.decision == KEEP for gap in plan_trim(utterance).gaps)


class TestRendering:
    def test_the_written_file_is_the_planned_length(self, tmp_path):
        source = tmp_path / "clip.mp4"
        audio = _build(tmp_path / "a.wav", [
            ("voice", 0.90), ("silence", 0.70), ("voice", 0.90),
        ])
        subprocess.run(
            [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=s=320x568:r=30:d=2.5",
             "-i", audio, "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(source)],
            check=True,
        )
        plan = plan_trim(source)
        written = apply_trim(plan, tmp_path / "out.mp4")
        info = probe(written)
        # カットはフレーム境界に落ちるので、1フレーム分の誤差は避けられない。
        assert info["duration"] == pytest.approx(plan.result_sec, abs=1 / 30 + 0.01)
        assert info["has_audio"]

    def test_nothing_to_cut_copies_rather_than_reencodes(self, tmp_path):
        source = tmp_path / "s.mp4"
        subprocess.run(
            [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=s=320x240:r=15:d=1",
             "-f", "lavfi", "-i", "sine=frequency=220:duration=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-shortest", str(source)],
            check=True,
        )
        plan = plan_trim(source)
        assert plan.cuts == []
        written = apply_trim(plan, tmp_path / "copy.mp4")
        assert probe(written)["duration"] == pytest.approx(1.0, abs=0.1)


class TestRetakesAreProposedNotCut:
    """「言い直し」は文字起こしが無いと確定できない。"""

    @pytest.fixture
    def false_start(self, tmp_path):
        """0.50秒 言いかけ / 0.60秒 間 / 1.60秒 言い直し。"""
        return _build(tmp_path / "r.wav", [
            ("voice", 0.50), ("silence", 0.60), ("voice", 1.60),
        ])

    def test_a_short_take_before_a_long_one_is_flagged(self, false_start):
        candidates = retake_candidates(plan_trim(false_start))
        assert len(candidates) == 1
        assert candidates[0].start == pytest.approx(0.0, abs=0.05)
        assert "可能性" in candidates[0].reason

    def test_an_even_pair_is_not_flagged(self, utterance):
        """似た長さが並んでいるだけでは言いかけの形にならない。

        誤検出したところを編集者が毎回確認するくらいなら、出さないほうがいい。
        """
        assert retake_candidates(plan_trim(utterance)) == []

    def test_flagging_never_changes_the_cut_plan(self, false_start):
        """候補が出ても、削除される尺は1秒たりとも変わらない。"""
        plan = plan_trim(false_start)
        before = plan.removed_sec
        assert retake_candidates(plan)
        assert plan.removed_sec == before

    def test_speech_segments_are_the_complement_of_the_gaps(self, utterance):
        plan = plan_trim(utterance)
        segments = speech_segments(plan)
        assert segments[0][0] == 0.0
        assert segments[-1][1] == pytest.approx(plan.duration, abs=0.05)
