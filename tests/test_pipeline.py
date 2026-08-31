"""Integration tests: the stages that make up the full chain."""

import pytest

from snsauto.creative.script import ScriptService
from snsauto.creative.storyboard import StoryboardService
from snsauto.models import Platform
from snsauto.platforms import PostRecord
from snsauto.research.keyword import ResearchService
from snsauto.research.structure import StructureService


def _records(n=12):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    titles = ["なぜ失敗するのか？", "5つのコツ", "実は知られていない方法", "プロが教える手順"]
    return [
        PostRecord(
            external_id=f"p{i}",
            platform=Platform.TIKTOK,
            title=f"{titles[i % len(titles)]}（{i}）",
            caption="本文 #副業 #節約 保存してね",
            published_at=now - timedelta(days=i + 1),
            duration_sec=20.0 + i,
            views=10_000 + i * 900,
            likes=800 + i * 40,
            comments=60,
            shares=30,
        )
        for i in range(n)
    ]


class TestResearchPersistence:
    def test_run_persists_ranked_posts(self, session, project):
        run = ResearchService(session).run(
            project, "副業", Platform.TIKTOK, limit=12, records=_records(), source="manual"
        )
        assert len(run.posts) == 12
        ranks = sorted(p.rank for p in run.posts)
        assert ranks == list(range(1, 13))
        assert all(p.score > 0 for p in run.posts)

    def test_duplicate_external_ids_are_collapsed(self, session, project):
        """Paging repeats items across pages; they must not double-count."""
        records = _records(4) + _records(4)
        run = ResearchService(session).run(
            project, "k", Platform.TIKTOK, records=records, source="manual"
        )
        assert len(run.posts) == 4


class TestStructureAnalysis:
    def test_analysis_is_visible_through_the_parent(self, session, project):
        """Reports read post.structure; setting the FK alone leaves it stale."""
        run = ResearchService(session).run(
            project, "k", Platform.TIKTOK, records=_records(3), source="manual"
        )
        service = StructureService(session)
        for post in run.posts:
            service.analyze(post)
        assert all(p.structure is not None for p in run.posts)
        assert all(p.structure.hook_type for p in run.posts)

    def test_reanalysis_updates_in_place(self, session, project):
        run = ResearchService(session).run(
            project, "k", Platform.TIKTOK, records=_records(1), source="manual"
        )
        service = StructureService(session)
        post = run.posts[0]
        first = service.analyze(post)
        second = service.analyze(post)
        assert first.id == second.id


class TestScriptTiming:
    def test_beats_tile_target_duration(self, session, project):
        script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 24.0)
        lines = script.lines
        assert lines[0]["start"] == 0.0
        assert lines[-1]["end"] == pytest.approx(24.0)
        for a, b in zip(lines, lines[1:]):
            assert a["end"] == pytest.approx(b["start"])

    def test_repair_closes_gaps_and_overlaps(self):
        broken = [
            {"index": 0, "start": 0, "end": 3},
            {"index": 1, "start": 3.5, "end": 7},   # gap
            {"index": 2, "start": 6, "end": 12},    # overlap
        ]
        fixed = ScriptService._repair_timing(broken, 15.0)
        assert fixed[0]["start"] == 0.0
        assert fixed[-1]["end"] == 15.0
        for a, b in zip(fixed, fixed[1:]):
            assert a["end"] == pytest.approx(b["start"])

    def test_repair_reindexes(self):
        fixed = ScriptService._repair_timing(
            [{"index": 9, "start": 0, "end": 2}, {"index": 4, "start": 2, "end": 4}], 4.0
        )
        assert [l["index"] for l in fixed] == [0, 1]

    def test_repair_handles_empty(self):
        assert ScriptService._repair_timing([], 10.0) == []


class TestStoryboard:
    def test_one_shot_per_script_beat(self, session, project):
        script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 18.0)
        board = StoryboardService(session).generate(script)
        assert len(board.shots) == len(script.lines)
        assert [s.index for s in board.shots] == list(range(len(script.lines)))

    def test_shots_carry_prompts_and_share_one_style(self, session, project):
        script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 18.0)
        board = StoryboardService(session).generate(script, style_hint="cinematic noir")
        assert all(s.visual_prompt for s in board.shots)
        assert all("cinematic noir" in s.visual_prompt for s in board.shots)

    def test_prompts_forbid_baked_in_text(self, session, project):
        """Telop is burned in later; text in the image would double up."""
        script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 12.0)
        board = StoryboardService(session).generate(script)
        assert all("No text" in s.visual_prompt for s in board.shots)


@pytest.mark.slow
class TestRender:
    def test_renders_a_playable_video_with_telop(self, session, project, tmp_path):
        from snsauto.creative.imagegen import ImageGenerator, PlaceholderProvider
        from snsauto.media.assemble import ShotInput, assemble_video
        from snsauto.media.ffmpeg import probe

        script = ScriptService(session).generate(project, "副業", Platform.TIKTOK, 6.0)
        board = StoryboardService(session).generate(script)
        ImageGenerator(provider=PlaceholderProvider()).render_storyboard(
            board, tmp_path / "img", 540, 960
        )

        shots = [
            ShotInput(duration=s.duration, image_path=s.image_path, telop=s.telop)
            for s in board.shots
        ]
        out = tmp_path / "out.mp4"
        info = assemble_video(shots, out, platform=Platform.TIKTOK, ken_burns=False)

        assert out.exists() and out.stat().st_size > 0
        assert info["telop_cues"] == len([s for s in board.shots if s.telop])
        probed = probe(out)
        assert probed["duration"] == pytest.approx(6.0, abs=0.6)
        assert probed["has_audio"]  # silent track added so concat stays uniform
