"""Web UI.

Server-rendered with the same Jinja setup the reports use, so the filters and
the override mechanism behave identically. Read-only by design: the CLI owns
mutation, and a browser button that silently spends money on image generation
or posts to a live account is not a good default.

Media (renders, storyboard stills) is served through explicit id-keyed routes
that resolve paths from the database rather than from user input. Serving the
workspace as a static directory would let a crafted path escape it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.collect import summarize_publication
from ..config import Settings, get_settings
from ..db import get_engine, init_db
from ..models import (
    PdcaCycle,
    PdcaStage,
    Project,
    Publication,
    Render,
    ResearchRun,
    Script,
    Shot,
    Storyboard,
)
from ..platforms import PostRecord, capability_matrix
from ..reporting.templates import _fmt_dt, _fmt_dur, _fmt_int, _fmt_pct
from ..research.keyword import W_ENGAGEMENT, W_REACH, W_VELOCITY, summarize_corpus

TEMPLATES = Path(__file__).parent / "templates"

REQUIREMENTS = {
    "youtube": "検索: APIキー / 投稿: OAuth2 (youtube.upload)",
    "x": "検索: Bearer (有料ティア) / 投稿: OAuth 1.0a",
    "instagram": "Business・Creator アカウント + Facebookアプリ審査",
    "tiktok": "Content Posting API。検索は公開APIなし",
}


def _environment_report(settings: Settings) -> list[dict]:
    from ..llm import build_client
    from ..media.ffmpeg import FFmpegError, ffmpeg_path
    from ..reporting.pdf import chromium_executable

    try:
        ffmpeg, ffmpeg_ok = ffmpeg_path(), True
    except FFmpegError as exc:
        ffmpeg, ffmpeg_ok = str(exc), False

    chromium = chromium_executable()
    llm = build_client(settings)

    return [
        {"name": "ffmpeg", "ok": ffmpeg_ok, "detail": Path(ffmpeg).name if ffmpeg_ok else "",
         "note": "動画の生成・カット検出に使用。PATH → FFMPEG_BINARY → imageio-ffmpeg の順で解決"},
        {"name": "Chromium", "ok": bool(chromium), "detail": Path(chromium).name if chromium else "",
         "note": "PDFレポート出力に使用。無い場合はHTMLのみ出力"},
        {"name": "LLM", "ok": llm is not None, "detail": settings.llm_model if llm else "",
         "note": "台本・絵コンテ・改善提案の生成。未設定でもヒューリスティックで動作"},
        {"name": "画像生成", "ok": settings.imagegen_provider != "placeholder",
         "detail": settings.imagegen_provider,
         "note": "placeholder はローカル生成。尺とテロップ可読性の検証に使えます"},
    ]


def _records(run: ResearchRun) -> list[PostRecord]:
    return [
        PostRecord(
            external_id=p.external_id, platform=p.platform, url=p.url, title=p.title,
            caption=p.caption, author=p.author, published_at=p.published_at,
            duration_sec=p.duration_sec, views=p.views, likes=p.likes,
            comments=p.comments, shares=p.shares,
        )
        for p in run.posts
    ]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    init_db()
    engine = get_engine()

    env = Environment(
        loader=ChoiceLoader([
            FileSystemLoader(str(settings.workspace / "templates")),  # user overrides
            FileSystemLoader(str(TEMPLATES)),
        ]),
        autoescape=select_autoescape(["html", "xml", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(int_=_fmt_int, pct=_fmt_pct, dur=_fmt_dur, dt=_fmt_dt)

    app = FastAPI(title="snsauto", docs_url="/api/docs", openapi_url="/api/openapi.json")

    def get_session():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    def render(template: str, session: Session, **ctx) -> HTMLResponse:
        ctx.setdefault("all_projects", list(session.scalars(select(Project).order_by(Project.name))))
        return HTMLResponse(env.get_template(template).render(**ctx))

    def _serve(path_str: str | None, download: bool, media_type: str, filename: str):
        if not path_str:
            raise HTTPException(404, "no file recorded")
        path = Path(path_str)
        if not path.is_file():
            raise HTTPException(404, f"file missing on disk: {path.name}")
        return FileResponse(
            path,
            media_type=media_type,
            filename=filename if download else None,
        )

    # ---------------- pages ----------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(session: Session = Depends(get_session)):
        projects = list(session.scalars(select(Project).order_by(Project.name)))
        renders = list(session.scalars(select(Render)))
        cycles = list(session.scalars(select(PdcaCycle)))

        rows = []
        for project in projects:
            script_ids = {s.id for s in project.scripts}
            board_ids = {
                b.id for b in session.scalars(
                    select(Storyboard).where(Storyboard.script_id.in_(script_ids or {-1}))
                )
            }
            rows.append({
                "project": project,
                "runs": len(project.research_runs),
                "scripts": len(project.scripts),
                "renders": len([r for r in renders if r.storyboard_id in board_ids]),
                "publications": session.query(Publication)
                                       .filter_by(project_id=project.id).count(),
            })

        totals = {
            "runs": session.query(ResearchRun).count(),
            "posts": sum(len(r.posts) for r in session.scalars(select(ResearchRun))),
            "scripts": session.query(Script).count(),
            "storyboards": session.query(Storyboard).count(),
            "renders": len(renders),
            "render_seconds": sum(r.duration_sec for r in renders),
            "publications": session.query(Publication).count(),
            "cycles": len(cycles),
            "open_cycles": len([c for c in cycles if c.stage != PdcaStage.ACT]),
        }
        recent = list(
            session.scalars(select(ResearchRun).order_by(ResearchRun.id.desc()).limit(8))
        )
        return render(
            "dashboard.html.j2", session, nav="dashboard", page_title="ダッシュボード",
            projects=rows, totals=totals, recent_runs=recent,
        )

    @app.get("/capabilities", response_class=HTMLResponse)
    def capabilities(session: Session = Depends(get_session)):
        return render(
            "capabilities.html.j2", session, nav="capabilities", page_title="接続状況",
            matrix=capability_matrix(settings), requirements=REQUIREMENTS,
            environment=_environment_report(settings),
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(project_id: int, session: Session = Depends(get_session)):
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")

        runs = []
        for run in sorted(project.research_runs, key=lambda r: r.id, reverse=True):
            summary = summarize_corpus(_records(run))
            runs.append({
                "run": run,
                "median_engagement": summary.get("engagement", {}).get("median", 0.0),
                "band": summary.get("duration_sec", {}).get("band_top"),
            })

        scripts = []
        for script in sorted(project.scripts, key=lambda s: s.id, reverse=True):
            board = script.storyboards[-1] if script.storyboards else None
            render_row = (
                session.scalars(
                    select(Render).where(Render.storyboard_id == board.id)
                                  .order_by(Render.id.desc())
                ).first()
                if board else None
            )
            scripts.append({
                "script": script,
                "shots": len(board.shots) if board else 0,
                "render": render_row,
            })

        publications = [
            {"publication": p, "summary": summarize_publication(p)}
            for p in session.scalars(
                select(Publication).where(Publication.project_id == project.id)
                                   .order_by(Publication.id.desc())
            )
        ]
        measured = [r["summary"] for r in publications if r["summary"].get("has_data")]
        perf = {
            "views": sum(m["views"] for m in measured),
            "engagement_rate": (
                sum(m["engagement_rate"] for m in measured) / len(measured) if measured else 0.0
            ),
            "measured": len(measured),
        }
        return render(
            "project.html.j2", session, nav="project", project=project,
            page_title=project.name, runs=runs, scripts=scripts,
            publications=publications, perf=perf,
            renders=[r["render"] for r in scripts if r["render"]],
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: int, session: Session = Depends(get_session)):
        from collections import Counter

        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        posts = sorted(run.posts, key=lambda p: p.rank)
        hooks = Counter(
            p.structure.hook_type for p in run.posts if p.structure and p.structure.hook_type
        )
        return render(
            "run.html.j2", session, nav="project", project=run.project, run=run,
            page_title=run.keyword, posts=posts, summary=summarize_corpus(_records(run)),
            hook_distribution=hooks.most_common(), analysed=sum(hooks.values()) or 1,
            weights={"engagement": W_ENGAGEMENT, "velocity": W_VELOCITY, "reach": W_REACH},
        )

    @app.get("/scripts/{script_id}", response_class=HTMLResponse)
    def script_page(script_id: int, session: Session = Depends(get_session)):
        script = session.get(Script, script_id)
        if script is None:
            raise HTTPException(404, "script not found")
        board = script.storyboards[-1] if script.storyboards else None
        render_row = (
            session.scalars(
                select(Render).where(Render.storyboard_id == board.id).order_by(Render.id.desc())
            ).first()
            if board else None
        )
        return render(
            "script.html.j2", session, nav="project", project=script.project,
            page_title=script.title, script=script, storyboard=board, render=render_row,
        )

    @app.get("/cycles/{cycle_id}", response_class=HTMLResponse)
    def cycle_page(cycle_id: int, session: Session = Depends(get_session)):
        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise HTTPException(404, "cycle not found")
        return render(
            "cycle.html.j2", session, nav="project", project=cycle.project,
            page_title=cycle.title, cycle=cycle,
        )

    # ---------------- reports ----------------

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    def run_report(run_id: int, session: Session = Depends(get_session)):
        from ..reporting.service import ReportService

        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        report = ReportService(session, settings).research_report(run, pdf=False)
        session.commit()
        return HTMLResponse(Path(report.html_path).read_text(encoding="utf-8"))

    @app.get("/projects/{project_id}/report", response_class=HTMLResponse)
    def project_report(project_id: int, session: Session = Depends(get_session)):
        from ..reporting.service import ReportService

        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        report = ReportService(session, settings).performance_report(project, pdf=False)
        session.commit()
        return HTMLResponse(Path(report.html_path).read_text(encoding="utf-8"))

    @app.get("/cycles/{cycle_id}/report", response_class=HTMLResponse)
    def cycle_report(cycle_id: int, session: Session = Depends(get_session)):
        from ..reporting.service import ReportService

        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise HTTPException(404, "cycle not found")
        report = ReportService(session, settings).pdca_report(cycle, pdf=False)
        session.commit()
        return HTMLResponse(Path(report.html_path).read_text(encoding="utf-8"))

    # ---------------- media ----------------

    @app.get("/media/render/{render_id}")
    def media_render(
        render_id: int,
        download: bool = Query(False),
        session: Session = Depends(get_session),
    ):
        row = session.get(Render, render_id)
        if row is None:
            raise HTTPException(404, "render not found")
        return _serve(row.path, download, "video/mp4", f"snsauto-{render_id}.mp4")

    @app.get("/media/shot/{shot_id}")
    def media_shot(shot_id: int, session: Session = Depends(get_session)):
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "shot not found")
        return _serve(shot.image_path, False, "image/png", f"shot-{shot_id}.png")

    # ---------------- JSON ----------------

    @app.get("/api/capabilities")
    def api_capabilities():
        return capability_matrix(settings)

    @app.get("/api/projects")
    def api_projects(session: Session = Depends(get_session)):
        return [
            {"id": p.id, "name": p.name, "description": p.description,
             "runs": len(p.research_runs), "scripts": len(p.scripts)}
            for p in session.scalars(select(Project).order_by(Project.name))
        ]

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: int, session: Session = Depends(get_session)):
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return {
            "id": run.id, "keyword": run.keyword, "platform": run.platform.value,
            "source": run.source, "summary": summarize_corpus(_records(run)),
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/favicon.ico")
    def favicon():
        return RedirectResponse("/", status_code=302)

    return app
