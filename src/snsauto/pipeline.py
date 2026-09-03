"""End-to-end orchestration.

The whole chain, in order:

    research -> structure analysis -> script -> storyboard -> images
    -> video -> publish -> metrics -> PDCA -> report

Every stage is separately callable and every stage persists its output, so a
run can be resumed from any point and nothing is recomputed silently. Stages
that need credentials the installation does not have are skipped and recorded
in ``PipelineResult.skipped`` rather than failing the run - a research pass
with no TikTok grant should still produce a script, a video and a report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .analytics.collect import MetricsCollector
from .analytics.pdca import PdcaService
from .config import get_settings
from .creative.imagegen import ImageGenerator
from .creative.script import ScriptService
from .creative.storyboard import StoryboardService
from .creative.visuals import VisualResult, VisualSourcer
from .creative.voice import VoiceService, VoiceTrack
from .llm import build_client
from .media.assemble import PLATFORM_SPECS, ShotInput, VideoSpec, assemble_video
from .models import (
    Platform,
    Project,
    Publication,
    PublicationStatus,
    Render,
    ResearchRun,
    Script,
    Storyboard,
    utcnow,
)
from .platforms import (
    Capability,
    PlatformError,
    PostRecord,
    PublishRequest,
    get_adapter,
)
from .reporting.service import ReportService
from .storage import StorageError, build_storage
from .research.keyword import ResearchService
from .research.structure import StructureService

log = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    project: Project
    run: ResearchRun | None = None
    script: Script | None = None
    storyboard: Storyboard | None = None
    render: Render | None = None
    visuals: VisualResult | None = None
    voice: VoiceTrack | None = None
    publications: list[Publication] = field(default_factory=list)
    report_paths: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "project": self.project.name,
            "research_run": self.run.id if self.run else None,
            "posts_collected": len(self.run.posts) if self.run else 0,
            "script": self.script.title if self.script else None,
            "shots": len(self.storyboard.shots) if self.storyboard else 0,
            "video": self.render.path if self.render else None,
            "duration_sec": self.render.duration_sec if self.render else None,
            "visuals": self.visuals.summary() if self.visuals else None,
            "voice": self.voice.summary() if self.voice else None,
            "publications": [
                {"platform": p.platform.value, "status": p.status.value,
                 "url": p.external_url, "error": p.error}
                for p in self.publications
            ],
            "reports": self.report_paths,
            "skipped": self.skipped,
            "errors": self.errors,
        }


class Pipeline:
    def __init__(self, session, settings=None, llm=None):
        self.session = session
        self.settings = settings or get_settings()
        self.llm = llm if llm is not None else build_client(self.settings)
        self.research = ResearchService(session, self.settings)
        self.structure = StructureService(session, self.llm)
        self.scripts = ScriptService(session, self.llm)
        self.storyboards = StoryboardService(session, self.llm)
        self.images = ImageGenerator(settings=self.settings)
        self.visuals = VisualSourcer(session, self.settings, images=self.images)
        self.voice = VoiceService(session, self.settings)
        self.metrics = MetricsCollector(session, self.settings)
        self.pdca = PdcaService(session, self.llm)
        self.reports = ReportService(session, self.settings)
        self.storage = build_storage(self.settings)

    # ---------- stages ----------

    def collect_research(
        self,
        project: Project,
        keyword: str,
        platform: Platform,
        limit: int = 50,
        records: list[PostRecord] | None = None,
    ) -> ResearchRun:
        source = "manual" if records is not None else "api"
        return self.research.run(
            project, keyword, platform, limit=limit, records=records, source=source
        )

    def analyze_structures(self, run: ResearchRun, top_n: int = 10) -> int:
        """Analyse only the top performers - the tail is not worth the calls."""
        analysed = 0
        for post in sorted(run.posts, key=lambda p: p.rank)[:top_n]:
            try:
                self.structure.analyze(post)
                analysed += 1
            except Exception as exc:
                log.warning("structure analysis failed for post %s: %s", post.id, exc)
        return analysed

    def write_script(
        self,
        project: Project,
        keyword: str,
        platform: Platform,
        duration: float = 30.0,
        run: ResearchRun | None = None,
    ) -> Script:
        return self.scripts.generate(project, keyword, platform, duration, run)

    def draw_storyboard(
        self,
        script: Script,
        style_hint: str | None = None,
        aspect_ratio: str | None = None,
    ) -> Storyboard:
        # Vertical by default on every platform - YouTube's target here is
        # Shorts, not landscape uploads. Override with aspect_ratio.
        return self.storyboards.generate(
            script, aspect_ratio=aspect_ratio or "9:16", style_hint=style_hint
        )

    def generate_visuals(
        self, storyboard: Storyboard, mode: str | None = None
    ) -> VisualResult:
        """Produce each shot's picture: still, animated clip or matched footage."""
        out_dir = self.settings.workspace / "assets" / f"storyboard-{storyboard.id}"
        spec = PLATFORM_SPECS.get(storyboard.script.platform, VideoSpec())
        return self.visuals.produce(storyboard, out_dir, spec.width, spec.height, mode)

    def generate_images(self, storyboard: Storyboard) -> list[Path]:
        """Stills only. Kept for callers that explicitly want the cheap path."""
        out_dir = self.settings.workspace / "assets" / f"storyboard-{storyboard.id}"
        spec = PLATFORM_SPECS.get(storyboard.script.platform, VideoSpec())
        paths = self.images.render_storyboard(storyboard, out_dir, spec.width, spec.height)
        self.session.flush()
        return paths

    def narrate(self, storyboard: Storyboard, realign: bool = True) -> VoiceTrack:
        """Synthesise narration and re-time the shots to the real speech."""
        out_dir = self.settings.workspace / "assets" / f"storyboard-{storyboard.id}" / "voice"
        return self.voice.narrate(storyboard, out_dir, realign=realign)

    def render_video(
        self,
        storyboard: Storyboard,
        bgm: str | Path | None = None,
        voice: str | Path | None = None,
        ken_burns: bool = True,
        visual_summary: dict | None = None,
    ) -> Render:
        platform = storyboard.script.platform
        spec = PLATFORM_SPECS.get(platform, VideoSpec())

        shots = [
            ShotInput(
                duration=shot.duration or 3.0,
                image_path=shot.image_path,
                clip_path=shot.clip_path,
                telop=shot.telop,
            )
            for shot in storyboard.shots
        ]
        if not shots:
            raise ValueError("storyboard has no shots")

        out_dir = self.settings.workspace / "renders"
        out_path = out_dir / f"storyboard-{storyboard.id}-{platform.value}.mp4"
        info = assemble_video(
            shots, out_path, platform=platform, spec=spec,
            bgm=bgm, voice=voice, ken_burns=ken_burns,
        )

        render = Render(
            storyboard_id=storyboard.id, path=info["path"],
            width=info["width"], height=info["height"], fps=info["fps"],
            duration_sec=info["duration_sec"], preset=platform.value,
            meta={
                "shots": info["shots"],
                "telop_cues": info["telop_cues"],
                "visuals": visual_summary or {},
                "narrated": bool(voice),
            },
        )
        self.session.add(render)
        self.session.flush()
        return render

    def publish(
        self,
        project: Project,
        render: Render,
        script: Script,
        platforms: list[Platform],
        scheduled_for: datetime | None = None,
        dry_run: bool = False,
        extra: dict | None = None,
    ) -> list[Publication]:
        published = []
        for platform in platforms:
            publication = Publication(
                project_id=project.id, render_id=render.id, script_id=script.id,
                platform=platform, caption=script.hook or script.title,
                hashtags=script.hashtags or [], scheduled_for=scheduled_for,
                status=PublicationStatus.SCHEDULED if scheduled_for else PublicationStatus.DRAFT,
            )
            self.session.add(publication)
            self.session.flush()
            published.append(publication)

            if dry_run:
                continue

            adapter = get_adapter(platform, settings=self.settings)
            if Capability.PUBLISH not in adapter.capabilities():
                publication.error = "publish capability unavailable (missing credentials)"
                publication.status = PublicationStatus.DRAFT
                continue

            request = PublishRequest(
                video_path=render.path,
                caption=script.hook or script.title,
                title=script.title,
                hashtags=script.hashtags or [],
                scheduled_for=scheduled_for,
                extra=dict(extra or {}),
            )
            if platform is Platform.INSTAGRAM and "video_url" not in request.extra:
                try:
                    request.extra["video_url"] = self.host_render(render).url
                except StorageError as exc:
                    publication.error = f"instagram needs a public video URL: {exc}"
                    publication.status = PublicationStatus.FAILED
                    log.error("instagram publish blocked: %s", exc)
                    continue
            try:
                result = adapter.publish(request)
                publication.external_id = result.external_id
                publication.external_url = result.url
                publication.status = (
                    PublicationStatus.SCHEDULED
                    if result.status == "scheduled"
                    else PublicationStatus.PUBLISHED
                )
                publication.published_at = utcnow()
            except PlatformError as exc:
                publication.error = str(exc)
                publication.status = PublicationStatus.FAILED
                log.error("publish to %s failed: %s", platform.value, exc)

        self.session.flush()
        return published

    def host_render(self, render: Render):
        """Put a render somewhere publicly fetchable and return the asset."""
        if self.storage is None:
            raise StorageError(
                "no public storage configured. Set STORAGE_BACKEND=s3 or "
                "STORAGE_BACKEND=local with SNSAUTO_PUBLIC_BASE_URL."
            )
        asset = self.storage.upload(render.path)
        render.meta = {
            **(render.meta or {}),
            "public": {
                "url": asset.url, "key": asset.key, "backend": asset.backend,
                "expires_in": asset.expires_in,
            },
        }
        self.session.flush()
        return asset

    # ---------- the whole chain ----------

    def run_all(
        self,
        project: Project,
        keyword: str,
        research_platform: Platform,
        publish_to: list[Platform] | None = None,
        duration: float = 30.0,
        limit: int = 50,
        records: list[PostRecord] | None = None,
        bgm: str | Path | None = None,
        dry_run: bool = True,
        make_reports: bool = True,
        visual_mode: str | None = None,
        narrate: bool = True,
    ) -> PipelineResult:
        result = PipelineResult(project=project)

        # 1. research
        try:
            result.run = self.collect_research(
                project, keyword, research_platform, limit, records
            )
        except PlatformError as exc:
            result.skipped.append(f"research({research_platform.value}): {exc}")
        except Exception as exc:
            result.errors.append(f"research: {exc}")

        # 2. structure analysis
        if result.run and result.run.posts:
            self.analyze_structures(result.run)

        # 3-4. script and storyboard
        try:
            result.script = self.write_script(
                project, keyword, research_platform, duration, result.run
            )
            result.storyboard = self.draw_storyboard(result.script)
        except Exception as exc:
            result.errors.append(f"creative: {exc}")
            return result

        # 5. narration first: it re-times the shots, so it must run before the
        #    visuals are cut to those timings.
        voice_path = None
        if narrate:
            try:
                track = self.narrate(result.storyboard)
                result.voice = track
                voice_path = track.path
            except Exception as exc:
                result.errors.append(f"voice: {exc}")

        # 6-7. visuals and video
        try:
            result.visuals = self.generate_visuals(result.storyboard, visual_mode)
            result.render = self.render_video(
                result.storyboard, bgm=bgm, voice=voice_path,
                visual_summary=result.visuals.summary(),
            )
        except Exception as exc:
            result.errors.append(f"render: {exc}")

        # 7. publish
        if result.render and publish_to:
            result.publications = self.publish(
                project, result.render, result.script, publish_to, dry_run=dry_run
            )

        # 8. reports
        if make_reports:
            try:
                if result.run:
                    report = self.reports.research_report(result.run)
                    result.report_paths.append(report.html_path)
                    if report.pdf_path:
                        result.report_paths.append(report.pdf_path)
            except Exception as exc:
                result.errors.append(f"report: {exc}")

        return result
