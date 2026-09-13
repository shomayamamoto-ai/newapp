"""The gate between rendering a video and publishing it.

Every threshold here is calibrated on something measured - the competitor
corpus for the keyword, or the account's own retention. The tests check that
too: a check that fires on an invented number is worse than no check, because
it gets switched off and then protects nothing.
"""

from dataclasses import dataclass

import pytest

from snsauto.media.qc import BLOCK, INFO, WARN, check_render, summary
from snsauto.models import Platform


@dataclass
class Shot:
    index: int
    start: float
    end: float
    telop: str = ""


@dataclass
class Render:
    path: str = ""
    duration_sec: float = 0.0


# What the competing posts in this niche actually do.
CORPUS = {
    "count": 47,
    "duration_sec": {"spread_top": {"p25": 18.0, "p75": 32.0}, "band_top": "15-30s"},
    "telop": {"chars_per_sec": 3.4, "avg_chars": 11.0},
    "pacing": {"avg_shot_sec": 2.1},
}


def issues(report, check):
    return [i for i in report.issues if i.check == check]


def render_with(shots, duration=25.0, research=CORPUS, retention=None,
                platform=Platform.TIKTOK):
    """Content checks only.

    The fixtures carry no real file, which is itself a blocking defect - so it
    is dropped here and covered by TestFileChecks on its own.
    """
    report = check_render(Render("", duration), shots, platform, research, retention)
    report.issues = [i for i in report.issues if i.check != "file"]
    return report


class TestCalibration:
    def test_with_no_corpus_the_opinionated_checks_stay_quiet(self):
        """A gate that fails good videos on guessed numbers gets turned off."""
        shots = [Shot(0, 0.5, 1.2, "少し速いかもしれないテロップ"),
                 Shot(1, 1.2, 20.0, "長めの一枚")]
        report = render_with(shots, research=None)
        assert issues(report, "pacing") == []
        assert issues(report, "duration") == []

    def test_the_corpus_is_named_in_the_reason(self):
        shots = [Shot(0, 0.5, 1.0, "この一枚はかなり長めの文章になっています")]
        report = render_with(shots)
        readability = issues(report, "readability")
        assert readability
        assert "競合上位" in readability[0].why
        assert "3.4" in readability[0].why

    def test_the_calibration_source_is_reported(self):
        assert "47" in render_with([Shot(0, 0.0, 3.0, "本文")]).calibration["source"]


class TestHook:
    def test_a_late_first_telop_is_flagged(self):
        report = render_with([Shot(0, 3.2, 6.0, "フック")])
        assert issues(report, "hook")

    def test_the_account_s_own_drop_off_tightens_the_limit(self):
        """If these viewers leave at 2 seconds, 1.8 is late for them."""
        retention = {"usable": True, "mode": "pattern",
                     "recurring_drops": [{"second": 2}]}
        shots = [Shot(0, 1.8, 4.0, "フック")]
        assert issues(render_with(shots, retention=retention), "hook")
        # Without that history the same video is inside the general guideline.
        assert issues(render_with(shots), "hook") == []

    def test_no_telop_at_all_is_flagged(self):
        report = render_with([Shot(0, 0.0, 5.0, "")])
        assert issues(report, "hook")
        assert "無音" in issues(report, "hook")[0].why


class TestReadability:
    def test_text_far_faster_than_the_niche_is_flagged(self):
        report = render_with([Shot(0, 0.5, 1.1, "血糖値の急上昇が食欲を増やします")])
        assert issues(report, "readability")

    def test_a_card_far_longer_than_the_niche_is_flagged(self):
        long_card = "あ" * 30      # corpus median is 11
        report = render_with([Shot(0, 0.5, 12.0, long_card)])
        assert any("1枚のテロップ" in i.what for i in issues(report, "readability"))

    def test_a_normal_card_passes(self):
        assert issues(render_with([Shot(0, 0.5, 4.0, "朝食を抜くと太る")]),
                      "readability") == []


class TestSafeArea:
    def test_a_url_split_across_lines_blocks_publishing(self):
        """Japanese wraps anywhere, so a URL gets broken mid-path."""
        shots = [Shot(0, 0.5, 6.0, "詳細はこちら https://example.com/very/long/path/2026")]
        found = issues(render_with(shots), "safe_area")
        assert found and found[0].severity == BLOCK
        assert "入力することもできません" in found[0].why

    def test_a_short_url_that_fits_on_one_line_is_fine(self):
        shots = [Shot(0, 0.5, 6.0, "https://a.co")]
        assert issues(render_with(shots), "safe_area") == []

    def test_ordinary_japanese_is_never_blocked(self):
        shots = [Shot(0, 0.5, 4.0, "朝食を抜くと逆に太る理由")]
        assert issues(render_with(shots), "safe_area") == []


class TestPacing:
    def test_a_shot_far_longer_than_the_niche_is_flagged(self):
        report = render_with([Shot(0, 0.5, 14.0, "一枚")])
        found = issues(report, "pacing")
        assert found and "2.1秒" in found[0].why

    def test_normal_pacing_passes(self):
        shots = [Shot(i, i * 2.0, (i + 1) * 2.0, "テロップ") for i in range(5)]
        assert issues(render_with(shots), "pacing") == []


class TestDuration:
    def test_far_outside_the_winning_band_is_advisory_only(self):
        report = render_with([Shot(0, 0.5, 4.0, "本文")], duration=90.0)
        found = issues(report, "duration")
        assert found and found[0].severity == INFO

    def test_inside_the_band_passes(self):
        assert issues(render_with([Shot(0, 0.5, 4.0, "本文")], duration=25.0),
                      "duration") == []


class TestReportShape:
    def test_blocking_issues_come_first(self):
        shots = [
            Shot(0, 3.5, 4.0, "遅いフック"),
            Shot(1, 4.0, 9.0, "詳細 https://example.com/very/long/path/2026"),
        ]
        report = render_with(shots)
        severities = [i.severity for i in report.issues]
        assert severities == sorted(severities, key=lambda s: {BLOCK: 0, WARN: 1, INFO: 2}[s])

    def test_a_blocking_issue_fails_the_report(self):
        shots = [Shot(0, 0.5, 6.0, "詳細 https://example.com/very/long/path/2026")]
        assert not render_with(shots).passed

    def test_warnings_alone_still_pass(self):
        assert render_with([Shot(0, 3.5, 6.0, "遅いフック")]).passed

    def test_every_issue_says_what_why_and_how_to_fix(self):
        shots = [Shot(0, 3.5, 3.9, "とても長い一枚のテロップになっています"),
                 Shot(1, 3.9, 20.0, "長い画")]
        for issue in render_with(shots).issues:
            assert issue.what and issue.why and issue.fix

    def test_a_clean_video_says_so(self):
        shots = [Shot(i, i * 2.0, (i + 1) * 2.0, "短いテロップ") for i in range(5)]
        assert "問題なし" in summary(render_with(shots))


class TestFileChecks:
    def test_a_missing_file_blocks(self):
        report = check_render(Render("", 25.0), [], Platform.TIKTOK, CORPUS)
        found = [i for i in report.issues if i.check == "file"]
        assert found and found[0].severity == BLOCK

    def test_an_unreadable_file_blocks_rather_than_raising(self, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a video")
        report = check_render(Render(str(broken), 25.0), [], Platform.TIKTOK, CORPUS)
        assert [i for i in report.issues if i.severity == BLOCK]


class TestPublishGate:
    def test_publishing_refuses_a_blocking_defect(self, session, project, tmp_path):
        """Publishing is not undoable, so the default is to stop."""
        import subprocess

        from snsauto.media.ffmpeg import ffmpeg_path
        from snsauto.models import Render as RenderRow
        from snsauto.models import Script as ScriptRow
        from snsauto.models import Shot as ShotRow
        from snsauto.models import Storyboard as StoryboardRow
        from snsauto.pipeline import Pipeline, QualityGateError

        video = tmp_path / "v.mp4"
        subprocess.run(
            [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1:r=30",
             "-t", "1", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(video)],
            check=True,
        )
        script = ScriptRow(project_id=project.id, title="t",
                           platform=Platform.TIKTOK, target_duration_sec=10.0,
                           lines=[])
        session.add(script)
        session.flush()
        board = StoryboardRow(script_id=script.id)
        session.add(board)
        session.flush()
        session.add(ShotRow(storyboard_id=board.id, index=0, start=0.0, end=5.0,
                            telop="本文"))
        render = RenderRow(storyboard_id=board.id, path=str(video), duration_sec=10.0)
        session.add(render)
        session.flush()

        pipeline = Pipeline(session)
        # 640x360 is landscape; TikTok expects vertical.
        with pytest.raises(QualityGateError) as exc:
            pipeline.publish(project, render, script, [Platform.TIKTOK])
        assert "アスペクト比" in str(exc.value)

        # Explicit acknowledgement gets past it.
        pipeline.publish(project, render, script, [Platform.TIKTOK],
                         skip_quality_gate=True, dry_run=True)
