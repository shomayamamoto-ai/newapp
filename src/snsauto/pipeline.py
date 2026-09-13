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
    SocialAccount,
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
    adapter_for_account,
)
from .platforms.accounts import AccountService, assert_account_scope
from .reporting.service import ReportService
from .storage import StorageError, build_storage
from .research.comments import CommentMiner
from .analytics.stats import median
from .research.keyword import ResearchService, summarize_corpus
from .research.watch import WatchService, diff_runs
from .research.structure import StructureService

log = logging.getLogger(__name__)


class QualityGateError(RuntimeError):
    """A finished video has a defect that will be visible to every viewer."""


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
                {"platform": p.platform.value, "account_id": p.account_id,
                 "status": p.status.value, "url": p.external_url, "error": p.error}
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
        self.structure = StructureService(session, self.llm, self.settings)
        self.scripts = ScriptService(session, self.llm)
        self.storyboards = StoryboardService(session, self.llm)
        self.images = ImageGenerator(settings=self.settings)
        self.visuals = VisualSourcer(session, self.settings, images=self.images)
        self.voice = VoiceService(session, self.settings)
        self.comments = CommentMiner(session, self.settings, self.llm)
        self.watch = WatchService(session, self.settings, self.research)
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

    def analyze_shared_audio(self, run: ResearchRun, top_n: int = 12) -> dict:
        """Which of this run's top posts use the same audio.

        Uses videos already fetched for telop analysis - nothing extra is
        downloaded. Posts whose video could not be obtained are simply absent
        from the comparison, and the count of what was analysed is reported so
        "no shared audio" is never confused with "nothing was checked".
        """
        from .research.fingerprint import recurring_sounds

        fetcher = self.structure.fetcher()
        if fetcher is None:
            return {"usable": False, "reason": "動画取得が設定されていません。"}

        videos = {}
        for post in sorted(run.posts, key=lambda p: p.rank)[:top_n]:
            path = fetcher.cached_path(post)
            if path.exists() and path.stat().st_size > 0:
                videos[post.external_id] = path

        result = recurring_sounds(videos)
        result["considered"] = min(top_n, len(run.posts))
        if not videos:
            result["reason"] = (
                "取得済みの動画がありません。先に構成分析を実行して動画を"
                "取得してください。"
            )
        return result

    def mine_comments(self, run: ResearchRun, top_n: int = 10) -> int:
        """Pull the comment text on the top posts. Never fatal."""
        try:
            return self.comments.mine_run(run, top_n=top_n)
        except Exception as exc:
            log.warning("comment mining failed for run %s: %s", run.id, exc)
            return 0

    def sweep_competitors(self, project: Project, limit: int = 25) -> dict:
        """Sweep every watched competitor and diff each against its last run."""
        outcome = self.watch.sweep_all(project, limit=limit)
        trends = {}
        for run in outcome["runs"]:
            self.analyze_structures(run, top_n=5)
            previous = self.watch.previous_run(run)
            if previous is not None:
                trends[run.account.display] = diff_runs(previous, run)
        outcome["trends"] = trends
        return outcome

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

        # Inspect before anyone can publish it. Doing this at render time
        # means the report is already there when someone opens the page, and
        # the defects are named while the storyboard is still fresh.
        render.meta = {**(render.meta or {}), "qc": self.inspect(render).as_dict()}
        self.session.flush()
        return render

    def inspect(self, render: Render, research: dict | None = None):
        """Quality report for a finished video.

        Calibrated against the research run the script came from and this
        account's own retention history, so the thresholds are measurements
        rather than opinions. Never raises: a video that cannot be inspected
        is reported as such, not treated as a failure to render.
        """
        from .media.qc import Report, check_render

        board = render.storyboard
        script = getattr(board, "script", None)
        shots = sorted(getattr(board, "shots", []) or [], key=lambda s: s.index)
        platform = script.platform if script else Platform.TIKTOK

        if research is None:
            research = self._research_profile(script)
        try:
            retention = self._retention_profile(script)
            return check_render(render, shots, platform, research, retention)
        except Exception as exc:
            log.warning("quality check failed for render %s: %s", render.id, exc)
            return Report()

    def _research_profile(self, script) -> dict:
        """The corpus this script was written against, with telop and pacing.

        These are what the QC thresholds are calibrated on, so they come from
        the same run the script used - not from whatever was measured last.
        """
        run = getattr(script, "run", None) if script else None
        if run is None:
            return {}
        from .reporting.service import _records  # noqa: PLC0415

        try:
            summary = summarize_corpus(
                _records(run), timezone_name=self.settings.timezone
            )
        except Exception:
            return {}

        telop_stats, pacing_stats = [], []
        for post in run.posts:
            telop = (post.structure.telop if post.structure else None) or {}
            onscreen = telop.get("onscreen") or {}
            if onscreen.get("event_count"):
                telop_stats.append(onscreen)
            if telop.get("pacing", {}).get("avg_shot_sec"):
                pacing_stats.append(telop["pacing"])

        if telop_stats:
            summary["telop"] = {
                "chars_per_sec": median([t["chars_per_sec"] for t in telop_stats]),
                "avg_chars": median([t["avg_chars"] for t in telop_stats]),
            }
        if pacing_stats:
            summary["pacing"] = {
                "avg_shot_sec": median([p["avg_shot_sec"] for p in pacing_stats])
            }
        return summary

    def _retention_profile(self, script) -> dict:
        from .analytics.playbook import build as build_playbook  # noqa: PLC0415

        if script is None:
            return {}
        return build_playbook(
            self.session, script.project_id, script.platform
        ).retention

    def publish_targets(
        self,
        project: Project,
        platforms: list[Platform] | None = None,
        account_ids: list[int] | None = None,
    ) -> list[tuple[Platform, int | None]]:
        """Expand a request into concrete (platform, account) pairs.

        Naming accounts posts to exactly those. Naming platforms posts to every
        account connected for them - which is what "運用中の全アカウントに出す"
        means - and falls back to a single unbound target when an install has
        no connected accounts yet.
        """
        accounts = AccountService(self.session, self.settings)
        targets: list[tuple[Platform, int | None]] = []

        for account_id in account_ids or []:
            account = self.session.get(SocialAccount, account_id)
            if account is None or not account.is_active:
                continue
            # Checked here as well as in resolve(), because this is where a
            # stale form, a copied URL or a mis-click turns into a public post
            # on somebody else's account.
            assert_account_scope(account, project.id)
            targets.append((account.platform, account.id))

        for platform in platforms or []:
            connected = accounts.targets(platform, project.id)
            if connected:
                targets.extend(
                    (platform, a.id) for a in connected
                    if (platform, a.id) not in targets
                )
            elif (platform, None) not in targets:
                targets.append((platform, None))

        return targets

    def publish(
        self,
        project: Project,
        render: Render,
        script: Script,
        platforms: list[Platform] | None = None,
        scheduled_for: datetime | None = None,
        dry_run: bool = False,
        extra: dict | None = None,
        account_ids: list[int] | None = None,
        skip_quality_gate: bool = False,
    ) -> list[Publication]:
        if not skip_quality_gate:
            blocking = [
                issue for issue in self.inspect(render).issues
                if issue.severity == "block"
            ]
            if blocking:
                # These are defects that are visible on every device - a wrong
                # aspect ratio, a URL split across lines. Publishing is not
                # undoable, so the default is to stop and say what to fix.
                raise QualityGateError(
                    "投稿前の品質チェックで修正が必要な項目が見つかりました:\n"
                    + "\n".join(f"  ・{i.what}\n    → {i.fix}" for i in blocking)
                    + "\n修正して書き出し直すか、了承のうえで実行してください。"
                )

        published = []
        for platform, account_id in self.publish_targets(project, platforms, account_ids):
            publication = Publication(
                project_id=project.id, render_id=render.id, script_id=script.id,
                platform=platform, account_id=account_id,
                caption=script.hook or script.title,
                hashtags=script.hashtags or [], scheduled_for=scheduled_for,
                status=PublicationStatus.SCHEDULED if scheduled_for else PublicationStatus.DRAFT,
            )
            self.session.add(publication)
            self.session.flush()
            published.append(publication)

            if dry_run:
                continue

            adapter, credentials = adapter_for_account(
                platform, self.session, self.settings, project.id, account_id
            )
            if Capability.PUBLISH not in adapter.capabilities():
                publication.error = (
                    f"{platform.value} のアカウントが未連携です。"
                    "「アカウント連携」から接続してください。"
                )
                publication.status = PublicationStatus.DRAFT
                continue

            accounts = AccountService(self.session, self.settings)
            # Caps are per account, so two connected accounts each get a full
            # allowance rather than sharing one.
            rate = accounts.check_rate(
                platform, credentials.account_id if credentials else None
            )
            if not rate["allowed"]:
                # Stop before the platform rejects it: on some platforms a
                # refused post still consumes a slot in the window.
                publication.error = rate["reason"]
                publication.status = PublicationStatus.SCHEDULED
                log.warning("rate limit reached for %s: %s", platform.value, rate["reason"])
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
            account_id = credentials.account_id if credentials else None
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
                accounts.record_attempt(platform, account_id, publication.id, True)
            except PlatformError as exc:
                publication.error = str(exc)
                publication.status = PublicationStatus.FAILED
                accounts.record_attempt(
                    platform, account_id, publication.id, False, str(exc)
                )
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
        account_ids: list[int] | None = None,
        mine_comments: bool = True,
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

        # 2. structure analysis, then the comment text on the same top posts
        if result.run and result.run.posts:
            self.analyze_structures(result.run)
            if mine_comments:
                self.mine_comments(result.run)

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
        if result.render and (publish_to or account_ids):
            result.publications = self.publish(
                project, result.render, result.script, publish_to,
                dry_run=dry_run, account_ids=account_ids,
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
