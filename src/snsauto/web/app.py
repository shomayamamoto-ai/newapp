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

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..analytics.collect import summarize_publication
from ..config import Settings, get_settings
from ..db import get_engine, init_db
from ..models import (
    Experiment,
    Job,
    JobStatus,
    PdcaCycle,
    PdcaStage,
    Platform,
    Project,
    Publication,
    Render,
    ResearchRun,
    Script,
    Shot,
    Storyboard,
    User,
    VisualMode,
)
from . import auth as authlib
from ..platforms import PostRecord, capability_matrix
from ..reporting.templates import _fmt_dt, _fmt_dur, _fmt_int, _fmt_pct
from ..research.keyword import W_ENGAGEMENT, W_REACH, W_VELOCITY, summarize_corpus

TEMPLATES = Path(__file__).parent / "templates"


class _LocalAdmin:
    """Stands in for a signed-in admin when auth is off (localhost only)."""

    id = 0
    email = "local"
    name = "local"
    role = "admin"
    is_active = True
    can_write = True
    can_publish = True


LOCAL_ADMIN = _LocalAdmin()

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
    secret = authlib.resolve_secret(settings)

    def get_session():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    def current_user(request: Request, session: Session = Depends(get_session)):
        """The signed-in user, or None. With auth off everyone is a local admin."""
        if not settings.auth_enabled:
            return LOCAL_ADMIN
        uid = authlib.read_session(request.cookies.get(authlib.SESSION_COOKIE), secret)
        if uid is None:
            return None
        user = session.get(User, uid)
        return user if (user and user.is_active) else None

    def require_login(user=Depends(current_user)):
        if user is None:
            raise HTTPException(401, "login required")
        return user

    def require_write(user=Depends(require_login)):
        if not user.can_write:
            raise HTTPException(403, "この操作には編集権限が必要です")
        return user

    def require_publish(user=Depends(require_login)):
        if not user.can_publish:
            raise HTTPException(403, "公開・投稿は管理者のみ実行できます")
        return user

    def check_csrf(request: Request, token: str | None):
        if not authlib.check_csrf(request.cookies.get(authlib.CSRF_COOKIE), token):
            raise HTTPException(400, "セッションが期限切れです。再読み込みしてください。")

    def render(template: str, session: Session, request: Request | None = None,
               user=None, **ctx) -> HTMLResponse:
        ctx.setdefault("all_projects", list(session.scalars(select(Project).order_by(Project.name))))
        ctx.setdefault("user", user)
        ctx.setdefault("auth_enabled", settings.auth_enabled)
        ctx.setdefault("visual_modes", [m.value for m in VisualMode])
        ctx.setdefault("platforms", [p.value for p in Platform])

        csrf = (request.cookies.get(authlib.CSRF_COOKIE) if request else None) or authlib.issue_csrf()
        ctx.setdefault("csrf_token", csrf)
        response = HTMLResponse(env.get_template(template).render(**ctx))
        response.set_cookie(
            authlib.CSRF_COOKIE, csrf, httponly=False, samesite="strict",
            secure=settings.cookie_secure, max_age=settings.session_hours * 3600,
        )
        return response

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
    def dashboard(request: Request, session: Session = Depends(get_session),
                  user=Depends(require_login)):
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
            "dashboard.html.j2", session, request, user, nav="dashboard", page_title="ダッシュボード",
            projects=rows, totals=totals, recent_runs=recent,
        )

    @app.get("/capabilities", response_class=HTMLResponse)
    def capabilities(request: Request, session: Session = Depends(get_session),
                     user=Depends(require_login)):
        return render(
            "capabilities.html.j2", session, request, user, nav="capabilities", page_title="接続状況",
            matrix=capability_matrix(settings), requirements=REQUIREMENTS,
            environment=_environment_report(settings),
        )

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_page(project_id: int, request: Request, job: int | None = None,
                     session: Session = Depends(get_session), user=Depends(require_login)):
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
            "project.html.j2", session, request, user, nav="project", project=project,
            page_title=project.name, runs=runs, scripts=scripts, job_id=job,
            publications=publications, perf=perf,
            renders=[r["render"] for r in scripts if r["render"]],
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: int, request: Request,
                 session: Session = Depends(get_session), user=Depends(require_login)):
        from collections import Counter

        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        posts = sorted(run.posts, key=lambda p: p.rank)
        hooks = Counter(
            p.structure.hook_type for p in run.posts if p.structure and p.structure.hook_type
        )
        return render(
            "run.html.j2", session, request, user, nav="project", project=run.project, run=run,
            page_title=run.keyword, posts=posts, summary=summarize_corpus(_records(run)),
            hook_distribution=hooks.most_common(), analysed=sum(hooks.values()) or 1,
            weights={"engagement": W_ENGAGEMENT, "velocity": W_VELOCITY, "reach": W_REACH},
        )

    @app.get("/scripts/{script_id}", response_class=HTMLResponse)
    def script_page(script_id: int, request: Request, job: int | None = None,
                    session: Session = Depends(get_session), user=Depends(require_login)):
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
            "script.html.j2", session, request, user, nav="project", project=script.project,
            page_title=script.title, script=script, storyboard=board,
            render=render_row, job_id=job,
        )

    @app.get("/cycles/{cycle_id}", response_class=HTMLResponse)
    def cycle_page(cycle_id: int, request: Request,
                   session: Session = Depends(get_session), user=Depends(require_login)):
        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise HTTPException(404, "cycle not found")
        return render(
            "cycle.html.j2", session, request, user, nav="project", project=cycle.project,
            page_title=cycle.title, cycle=cycle,
        )

    # ---------------- reports ----------------

    @app.get("/runs/{run_id}/report", response_class=HTMLResponse)
    def run_report(run_id: int, session: Session = Depends(get_session),
                   user=Depends(require_login)):
        from ..reporting.service import ReportService

        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        report = ReportService(session, settings).research_report(run, pdf=False)
        session.commit()
        return HTMLResponse(Path(report.html_path).read_text(encoding="utf-8"))

    @app.get("/projects/{project_id}/report", response_class=HTMLResponse)
    def project_report(project_id: int, session: Session = Depends(get_session),
                       user=Depends(require_login)):
        from ..reporting.service import ReportService

        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "project not found")
        report = ReportService(session, settings).performance_report(project, pdf=False)
        session.commit()
        return HTMLResponse(Path(report.html_path).read_text(encoding="utf-8"))

    @app.get("/cycles/{cycle_id}/report", response_class=HTMLResponse)
    def cycle_report(cycle_id: int, session: Session = Depends(get_session),
                     user=Depends(require_login)):
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
        user=Depends(require_login),
    ):
        row = session.get(Render, render_id)
        if row is None:
            raise HTTPException(404, "render not found")
        return _serve(row.path, download, "video/mp4", f"snsauto-{render_id}.mp4")

    @app.get("/media/shot/{shot_id}")
    def media_shot(shot_id: int, session: Session = Depends(get_session),
                   user=Depends(require_login)):
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise HTTPException(404, "shot not found")
        return _serve(shot.image_path, False, "image/png", f"shot-{shot_id}.png")

    # ---------------- JSON ----------------

    @app.get("/api/capabilities")
    def api_capabilities(user=Depends(require_login)):
        return capability_matrix(settings)

    @app.get("/api/projects")
    def api_projects(session: Session = Depends(get_session), user=Depends(require_login)):
        return [
            {"id": p.id, "name": p.name, "description": p.description,
             "runs": len(p.research_runs), "scripts": len(p.scripts)}
            for p in session.scalars(select(Project).order_by(Project.name))
        ]

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: int, session: Session = Depends(get_session), user=Depends(require_login)):
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise HTTPException(404, "run not found")
        return {
            "id": run.id, "keyword": run.keyword, "platform": run.platform.value,
            "source": run.source, "summary": summarize_corpus(_records(run)),
        }

    @app.exception_handler(HTTPException)
    def unauthorised(request: Request, exc: HTTPException):
        """Send a signed-out browser to the login page, not to raw JSON.

        API callers still get the status code, so scripts can tell the
        difference between "not logged in" and "no such thing".
        """
        from fastapi.responses import JSONResponse

        wants_json = request.url.path.startswith("/api") or "application/json" in (
            request.headers.get("accept", "")
        )
        if exc.status_code == 401 and not wants_json:
            return RedirectResponse("/login", status_code=303)
        if wants_json or request.method != "GET":
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        return HTMLResponse(
            f"<!doctype html><meta charset=utf-8>"
            f"<style>body{{font:14px system-ui;padding:40px;max-width:520px;margin:auto}}"
            f"a{{color:#3b5bdb}}</style>"
            f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>"
            f"<p><a href='/'>ダッシュボードへ戻る</a></p>",
            status_code=exc.status_code,
        )

    # ---------------- login ----------------

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, session: Session = Depends(get_session),
                   error: str | None = None):
        if not settings.auth_enabled:
            return RedirectResponse("/", status_code=302)
        return render("login.html.j2", session, request, None,
                      page_title="ログイン", error=error, all_projects=[])

    @app.post("/login")
    def login_submit(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(""),
        session: Session = Depends(get_session),
    ):
        check_csrf(request, csrf_token)
        user = authlib.authenticate(session, email, password)
        if user is None:
            session.commit()
            # Never say which half was wrong - that enumerates accounts.
            return RedirectResponse(
                "/login?error=メールアドレスまたはパスワードが違います", status_code=303
            )
        session.commit()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            authlib.SESSION_COOKIE,
            authlib.issue_session(user.id, secret, settings.session_hours),
            httponly=True, samesite="lax", secure=settings.cookie_secure,
            max_age=settings.session_hours * 3600,
        )
        return response

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        check_csrf(request, csrf_token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(authlib.SESSION_COOKIE)
        return response

    # ---------------- actions ----------------

    def _enqueue(session, kind: str, params: dict, project_id: int | None,
                 back: str) -> RedirectResponse:
        from ..scheduling.jobs import JobRunner

        job = JobRunner.enqueue(session, kind, params, project_id)
        session.commit()
        _spawn(job.id)
        separator = "&" if "?" in back else "?"
        return RedirectResponse(f"{back}{separator}job={job.id}", status_code=303)

    def _spawn(job_id: int) -> None:
        """Run the job off the request thread so the browser is not held open."""
        import threading

        from ..scheduling.jobs import JobRunner

        def target():
            factory = lambda: Session(engine, expire_on_commit=False)  # noqa: E731
            JobRunner(factory, settings).run_job(job_id)

        threading.Thread(target=target, daemon=True, name=f"job-{job_id}").start()

    @app.post("/projects/{project_id}/research")
    def action_research(
        project_id: int, request: Request,
        keyword: str = Form(...), platform: str = Form("youtube"),
        limit: int = Form(50), csrf_token: str = Form(""),
        session: Session = Depends(get_session), user=Depends(require_write),
    ):
        check_csrf(request, csrf_token)
        return _enqueue(session, "research", {
            "project_id": project_id, "keyword": keyword,
            "platform": platform, "limit": limit,
        }, project_id, f"/projects/{project_id}")

    @app.post("/projects/{project_id}/script")
    def action_script(
        project_id: int, request: Request,
        keyword: str = Form(...), platform: str = Form("youtube"),
        duration: float = Form(30.0), run_id: int | None = Form(None),
        csrf_token: str = Form(""),
        session: Session = Depends(get_session), user=Depends(require_write),
    ):
        check_csrf(request, csrf_token)
        return _enqueue(session, "script", {
            "project_id": project_id, "keyword": keyword, "platform": platform,
            "duration": duration, "run_id": run_id,
        }, project_id, f"/projects/{project_id}")

    @app.post("/scripts/{script_id}/video")
    def action_video(
        script_id: int, request: Request,
        visual_mode: str = Form("still"), narrate: bool = Form(False),
        style: str = Form(""), csrf_token: str = Form(""),
        session: Session = Depends(get_session), user=Depends(require_write),
    ):
        check_csrf(request, csrf_token)
        script = session.get(Script, script_id)
        if script is None:
            raise HTTPException(404, "script not found")
        return _enqueue(session, "video", {
            "script_id": script_id, "visual_mode": visual_mode,
            "narrate": narrate, "style": style or None,
        }, script.project_id, f"/scripts/{script_id}")

    @app.post("/scripts/{script_id}/experiment")
    def action_experiment(
        script_id: int, request: Request,
        dimension: str = Form("hook"), arms: int = Form(2),
        name: str = Form(""), csrf_token: str = Form(""),
        session: Session = Depends(get_session), user=Depends(require_write),
    ):
        check_csrf(request, csrf_token)
        script = session.get(Script, script_id)
        if script is None:
            raise HTTPException(404, "script not found")
        return _enqueue(session, "experiment", {
            "project_id": script.project_id, "script_id": script_id,
            "dimension": dimension, "arms": arms, "name": name,
        }, script.project_id, f"/scripts/{script_id}")

    @app.post("/renders/{render_id}/publish")
    def action_publish(
        render_id: int, request: Request,
        platforms: list[str] = Form([]), scheduled_for: str = Form(""),
        confirm: str = Form(""), csrf_token: str = Form(""),
        session: Session = Depends(get_session), user=Depends(require_publish),
    ):
        check_csrf(request, csrf_token)
        render_row = session.get(Render, render_id)
        if render_row is None:
            raise HTTPException(404, "render not found")
        # Posting publicly is irreversible, so it takes an explicit confirmation
        # rather than a single click.
        if confirm != "PUBLISH":
            raise HTTPException(400, "確認欄に PUBLISH と入力してください")
        if not platforms:
            raise HTTPException(400, "投稿先を1つ以上選んでください")

        board = session.get(Storyboard, render_row.storyboard_id)
        script = session.get(Script, board.script_id) if board else None
        return _enqueue(session, "publish", {
            "project_id": script.project_id if script else None,
            "render_id": render_id,
            "script_id": script.id if script else None,
            "platforms": platforms,
            "scheduled_for": scheduled_for or None,
            "dry_run": False,
        }, script.project_id if script else None,
           f"/scripts/{script.id}" if script else "/")

    @app.post("/projects/{project_id}/metrics")
    def action_metrics(
        project_id: int, request: Request, csrf_token: str = Form(""),
        session: Session = Depends(get_session), user=Depends(require_write),
    ):
        check_csrf(request, csrf_token)
        return _enqueue(session, "metrics", {"project_id": project_id},
                        project_id, f"/projects/{project_id}")

    # ---------------- jobs & experiments ----------------

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, session: Session = Depends(get_session),
                  user=Depends(require_login)):
        rows = list(session.scalars(select(Job).order_by(Job.id.desc()).limit(60)))
        return render("jobs.html.j2", session, request, user,
                      nav="jobs", page_title="実行履歴", jobs=rows)

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int, session: Session = Depends(get_session),
                user=Depends(require_login)):
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {
            "id": job.id, "kind": job.kind, "status": job.status.value,
            "result": job.result, "error": job.error,
            "finished": job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED),
        }

    @app.get("/experiments/{experiment_id}", response_class=HTMLResponse)
    def experiment_page(experiment_id: int, request: Request,
                        session: Session = Depends(get_session),
                        user=Depends(require_login)):
        from ..experiments import ExperimentService

        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise HTTPException(404, "experiment not found")
        ExperimentService(session).evaluate(experiment)
        session.commit()
        return render("experiment.html.j2", session, request, user, nav="project",
                      project=experiment.project, page_title=experiment.name,
                      experiment=experiment)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/favicon.ico")
    def favicon():
        return RedirectResponse("/", status_code=302)

    return app
