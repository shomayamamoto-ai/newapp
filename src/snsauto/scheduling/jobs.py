"""Background jobs for actions started from the web UI.

Long work - a research sweep, image or video generation, a render - must not
run inside a request: the browser would time out and a refresh would start it
twice. So the UI enqueues a Job row and this runner executes it, writing
progress into the row that the UI polls.

Jobs are claimed with a compare-and-set before running, for the same reason
publications are: two workers must not run the same render twice.
"""

from __future__ import annotations

import logging
import traceback
from datetime import timedelta

from sqlalchemy import select, update

from ..config import get_settings
from ..models import Job, JobStatus, Platform, Project, Script, Storyboard, utcnow

log = logging.getLogger(__name__)

CLAIM_TIMEOUT = timedelta(hours=2)


class JobRunner:
    """Executes queued jobs. Every handler takes (session, job, params)."""

    def __init__(self, session_factory, settings=None, identity: str = "web"):
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.identity = identity

    # ---------- queueing ----------

    @staticmethod
    def enqueue(session, kind: str, params: dict, project_id: int | None = None) -> Job:
        if kind not in HANDLERS:
            raise ValueError(f"unknown job kind {kind!r}; known: {sorted(HANDLERS)}")
        job = Job(kind=kind, params=params, project_id=project_id, status=JobStatus.QUEUED)
        session.add(job)
        session.flush()
        return job

    # ---------- execution ----------

    def claim(self, session, job_id: int) -> bool:
        cutoff = utcnow() - CLAIM_TIMEOUT
        result = session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                (Job.claimed_by.is_(None)) | (Job.claimed_at < cutoff),
            )
            .values(
                claimed_by=self.identity, claimed_at=utcnow(),
                status=JobStatus.RUNNING, started_at=utcnow(),
            )
        )
        return result.rowcount == 1

    def run_job(self, job_id: int) -> dict:
        with self.session_factory() as session:
            if not self.claim(session, job_id):
                session.commit()
                return {"job": job_id, "status": "skipped", "reason": "already claimed"}
            session.commit()

        with self.session_factory() as session:
            job = session.get(Job, job_id)
            handler = HANDLERS[job.kind]
            try:
                job.result = handler(self, session, job, job.params or {}) or {}
                job.status = JobStatus.SUCCEEDED
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                job.log = (job.log or []) + [traceback.format_exc()[-2000:]]
                log.exception("job %s (%s) failed", job_id, job.kind)
                from ..notify import AlertService

                AlertService(session, self.settings).raise_alert(
                    source=f"job.{job.kind}",
                    title=f"{job.kind} ジョブが失敗しました",
                    detail=job.error,
                    project_id=job.project_id,
                    context={"job_id": job.id, "params": job.params},
                )
            finally:
                job.finished_at = utcnow()
                job.claimed_by = None
                session.commit()
            return {"job": job_id, "status": job.status.value, "error": job.error}

    def run_queued(self, limit: int = 5) -> list[dict]:
        with self.session_factory() as session:
            ids = [
                j.id for j in session.scalars(
                    select(Job).where(Job.status == JobStatus.QUEUED)
                             .order_by(Job.id).limit(limit)
                )
            ]
        return [self.run_job(job_id) for job_id in ids]


# ---------------- handlers ----------------


def _pipeline(runner: JobRunner, session):
    from ..pipeline import Pipeline

    return Pipeline(session, runner.settings)


def _research(runner, session, job, params) -> dict:
    project = session.get(Project, params["project_id"])
    pipeline = _pipeline(runner, session)
    run = pipeline.collect_research(
        project, params["keyword"], Platform(params["platform"]),
        limit=int(params.get("limit", 50)),
    )
    analysed = pipeline.analyze_structures(run)
    return {"run_id": run.id, "posts": len(run.posts), "analysed": analysed}


def _script(runner, session, job, params) -> dict:
    from ..models import ResearchRun

    project = session.get(Project, params["project_id"])
    run = session.get(ResearchRun, params["run_id"]) if params.get("run_id") else None
    script = _pipeline(runner, session).write_script(
        project, params["keyword"], Platform(params["platform"]),
        float(params.get("duration", 30.0)), run,
    )
    return {"script_id": script.id, "title": script.title, "beats": len(script.lines)}


def _video(runner, session, job, params) -> dict:
    script = session.get(Script, params["script_id"])
    pipeline = _pipeline(runner, session)

    board = (
        session.get(Storyboard, params["storyboard_id"])
        if params.get("storyboard_id")
        else pipeline.draw_storyboard(script, style_hint=params.get("style"))
    )

    voice_path = None
    voice_summary = None
    if params.get("narrate", True):
        track = pipeline.narrate(board)
        voice_path, voice_summary = track.path, track.summary()

    visuals = pipeline.generate_visuals(board, params.get("visual_mode"))
    render = pipeline.render_video(
        board, bgm=params.get("bgm"), voice=voice_path,
        ken_burns=params.get("ken_burns", True),
        visual_summary=visuals.summary(),
    )
    return {
        "storyboard_id": board.id, "render_id": render.id, "path": render.path,
        "duration_sec": render.duration_sec,
        "visuals": visuals.summary(), "voice": voice_summary,
    }


def _publish(runner, session, job, params) -> dict:
    from ..models import Render

    project = session.get(Project, params["project_id"])
    render = session.get(Render, params["render_id"])
    script = session.get(Script, params["script_id"]) if params.get("script_id") else None
    scheduled_for = params.get("scheduled_for")
    if isinstance(scheduled_for, str) and scheduled_for:
        from datetime import datetime

        scheduled_for = datetime.fromisoformat(scheduled_for)
    else:
        scheduled_for = None

    publications = _pipeline(runner, session).publish(
        project, render, script,
        [Platform(p) for p in params.get("platforms", [])],
        scheduled_for=scheduled_for,
        dry_run=bool(params.get("dry_run", True)),
    )
    return {
        "publications": [
            {"id": p.id, "platform": p.platform.value, "status": p.status.value,
             "url": p.external_url, "error": p.error}
            for p in publications
        ]
    }


def _metrics(runner, session, job, params) -> dict:
    from ..analytics.collect import MetricsCollector

    snapshots = MetricsCollector(session, runner.settings).collect_all(
        params.get("project_id")
    )
    return {"snapshots": len(snapshots)}


def _experiment(runner, session, job, params) -> dict:
    from ..experiments import ExperimentService
    from ..llm import build_client

    project = session.get(Project, params["project_id"])
    base = session.get(Script, params["script_id"])
    experiment = ExperimentService(session, build_client(runner.settings)).create(
        project, params.get("name") or f"{base.title} A/B",
        base, params.get("dimension", "hook"), int(params.get("arms", 2)),
    )
    return {
        "experiment_id": experiment.id,
        "variants": [
            {"label": v.label, "script_id": v.script_id, "treatment": v.treatment}
            for v in experiment.variants
        ],
    }


HANDLERS = {
    "research": _research,
    "script": _script,
    "video": _video,
    "publish": _publish,
    "metrics": _metrics,
    "experiment": _experiment,
}
