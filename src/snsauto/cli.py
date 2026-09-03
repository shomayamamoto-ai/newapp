"""Command line interface."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import get_settings
from .db import init_db, session_scope
from .models import (
    Experiment, PdcaCycle, Platform, Project, ResearchRun, Script, User,
)
from .pipeline import Pipeline
from .platforms import capability_matrix
from .platforms.tiktok import TikTokAdapter
from .reporting.templates import TemplateRegistry, list_templates

app = typer.Typer(help="SNS operations automation: research to PDCA, one pipeline.", no_args_is_help=True)
project_app = typer.Typer(help="Manage projects.", no_args_is_help=True)
research_app = typer.Typer(help="Keyword research and competitor analysis.", no_args_is_help=True)
create_app = typer.Typer(help="Scripts, storyboards and video.", no_args_is_help=True)
metrics_app = typer.Typer(help="Collect and inspect performance.", no_args_is_help=True)
pdca_app = typer.Typer(help="Plan-Do-Check-Act cycles.", no_args_is_help=True)
report_app = typer.Typer(help="HTML / PDF reports.", no_args_is_help=True)
template_app = typer.Typer(help="HTML/CSS report templates.", no_args_is_help=True)
footage_app = typer.Typer(help="Real / stock footage library.", no_args_is_help=True)
worker_app = typer.Typer(help="Scheduled publishing and metrics collection.", no_args_is_help=True)
ab_app = typer.Typer(help="A/B experiments.", no_args_is_help=True)
user_app = typer.Typer(help="Web UI accounts.", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(research_app, name="research")
app.add_typer(create_app, name="create")
app.add_typer(metrics_app, name="metrics")
app.add_typer(pdca_app, name="pdca")
app.add_typer(report_app, name="report")
app.add_typer(template_app, name="template")
app.add_typer(footage_app, name="footage")
app.add_typer(worker_app, name="worker")
app.add_typer(ab_app, name="ab")
app.add_typer(user_app, name="user")

console = Console()


def _get_project(session, name: str) -> Project:
    project = session.query(Project).filter_by(name=name).one_or_none()
    if project is None:
        raise typer.BadParameter(f"no project named {name!r}. Run: snsauto project create {name}")
    return project


@app.command()
def init():
    """Create the database and workspace directories."""
    settings = get_settings()
    settings.ensure_workspace()
    init_db()
    console.print(f"[green]Initialised.[/green] workspace={settings.workspace} db={settings.db_url}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Start the web UI."""
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter(
            "web extra not installed. Run: pip install 'snsauto[web]'"
        ) from exc

    settings = get_settings()
    settings.ensure_workspace()
    init_db()
    console.print(f"[green]snsauto[/green] http://{host}:{port}")
    uvicorn.run(
        "snsauto.web.app:create_app",
        host=host, port=port, reload=reload, factory=True,
    )


@app.command()
def doctor():
    """Show what this installation can actually do right now."""
    from .llm import build_client
    from .media.ffmpeg import ffmpeg_path
    from .reporting.pdf import chromium_executable

    settings = get_settings()
    table = Table(title="Platform capabilities", header_style="bold")
    table.add_column("Platform")
    for cap in ("search", "publish", "insights"):
        table.add_column(cap, justify="center")
    for platform, caps in capability_matrix(settings).items():
        table.add_row(
            platform,
            *["[green]yes[/green]" if caps[c] else "[dim]no[/dim]"
              for c in ("search", "publish", "insights")],
        )
    console.print(table)

    try:
        ffmpeg = ffmpeg_path()
    except Exception as exc:
        ffmpeg = f"[red]missing ({exc})[/red]"
    console.print(f"ffmpeg   : {ffmpeg}")
    console.print(f"chromium : {chromium_executable() or '[yellow]not found - PDF export unavailable[/yellow]'}")
    console.print(f"LLM      : {'[green]' + settings.llm_model + '[/green]' if build_client(settings) else '[yellow]no ANTHROPIC_API_KEY - heuristic fallbacks active[/yellow]'}")
    console.print(f"images   : {settings.imagegen_provider}")
    console.print("\n[dim]TikTok search is never available: there is no public keyword-search API.\n"
                  "Use `snsauto research import` with a CSV instead.[/dim]")


# ---------------- project ----------------

@project_app.command("create")
def project_create(
    name: str,
    description: str = typer.Option("", "--description", "-d"),
    brand_profile: Path = typer.Option(None, "--brand-profile", help="JSON file: tone, persona, banned words"),
):
    """Create a project."""
    init_db()
    profile = json.loads(brand_profile.read_text(encoding="utf-8")) if brand_profile else {}
    with session_scope() as session:
        if session.query(Project).filter_by(name=name).one_or_none():
            raise typer.BadParameter(f"project {name!r} already exists")
        project = Project(name=name, description=description, brand_profile=profile)
        session.add(project)
        session.flush()
        console.print(f"[green]Created project[/green] {project.name} (id={project.id})")


@project_app.command("list")
def project_list():
    """List projects."""
    init_db()
    with session_scope() as session:
        table = Table(header_style="bold")
        table.add_column("id", justify="right")
        table.add_column("name")
        table.add_column("runs", justify="right")
        table.add_column("scripts", justify="right")
        table.add_column("cycles", justify="right")
        for p in session.query(Project).order_by(Project.id):
            table.add_row(str(p.id), p.name, str(len(p.research_runs)),
                          str(len(p.scripts)), str(len(p.pdca_cycles)))
        console.print(table)


# ---------------- research ----------------

@research_app.command("run")
def research_run(
    project: str,
    keyword: str,
    platform: Platform = typer.Option(Platform.YOUTUBE, "--platform", "-p"),
    limit: int = typer.Option(50, "--limit", "-n"),
    analyze: bool = typer.Option(True, "--analyze/--no-analyze", help="Break down the top performers"),
):
    """Collect and rank the top N competing posts for a keyword."""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        pipeline = Pipeline(session)
        run = pipeline.collect_research(proj, keyword, platform, limit)
        console.print(f"[green]Collected[/green] {len(run.posts)} posts (run id={run.id})")
        if analyze:
            n = pipeline.analyze_structures(run)
            console.print(f"Analysed structure of top {n} posts")
        _print_top(run)


@research_app.command("import")
def research_import(
    project: str,
    keyword: str,
    csv_path: Path,
    platform: Platform = typer.Option(Platform.TIKTOK, "--platform", "-p"),
):
    """Import competitor posts from a CSV (for platforms with no search API)."""
    init_db()
    records = TikTokAdapter.ingest_manual(csv_path)
    for r in records:
        r.platform = platform
    with session_scope() as session:
        proj = _get_project(session, project)
        run = Pipeline(session).collect_research(
            proj, keyword, platform, len(records), records=records
        )
        console.print(f"[green]Imported[/green] {len(run.posts)} posts (run id={run.id})")
        _print_top(run)


def _print_top(run: ResearchRun, n: int = 10):
    table = Table(title=f"Top {n} - {run.keyword}", header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("title", max_width=48)
    table.add_column("views", justify="right")
    table.add_column("eng.", justify="right")
    table.add_column("score", justify="right")
    for post in sorted(run.posts, key=lambda p: p.rank)[:n]:
        table.add_row(str(post.rank), (post.title or post.external_id)[:48],
                      f"{post.views:,}", f"{post.engagement_rate:.2%}", f"{post.score:.3f}")
    console.print(table)


# ---------------- create ----------------

@create_app.command("all")
def create_all(
    project: str,
    keyword: str,
    platform: Platform = typer.Option(Platform.YOUTUBE, "--platform", "-p"),
    duration: float = typer.Option(30.0, "--duration", "-d"),
    limit: int = typer.Option(50, "--limit", "-n"),
    publish: list[Platform] = typer.Option([], "--publish", help="Platforms to post to"),
    bgm: Path = typer.Option(None, "--bgm"),
    live: bool = typer.Option(False, "--live", help="Actually publish (default is dry run)"),
    csv_path: Path = typer.Option(None, "--csv", help="Use a CSV instead of the search API"),
):
    """Run the whole chain: research to video to report."""
    init_db()
    get_settings().ensure_workspace()
    records = None
    if csv_path:
        records = TikTokAdapter.ingest_manual(csv_path)
        for r in records:
            r.platform = platform
    with session_scope() as session:
        proj = _get_project(session, project)
        result = Pipeline(session).run_all(
            proj, keyword, platform, publish_to=list(publish) or None,
            duration=duration, limit=limit, records=records,
            bgm=bgm, dry_run=not live,
        )
        console.print_json(json.dumps(result.summary(), ensure_ascii=False, default=str))


@create_app.command("script")
def create_script(
    project: str,
    keyword: str,
    platform: Platform = typer.Option(Platform.YOUTUBE, "--platform", "-p"),
    duration: float = typer.Option(30.0, "--duration", "-d"),
    run_id: int = typer.Option(None, "--run", help="Base the script on this research run"),
):
    """Write a script (台本)."""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        run = session.get(ResearchRun, run_id) if run_id else None
        script = Pipeline(session).write_script(proj, keyword, platform, duration, run)
        console.print(f"[green]Script {script.id}[/green]: {script.title}")
        table = Table(header_style="bold")
        table.add_column("#", justify="right")
        table.add_column("time")
        table.add_column("telop")
        table.add_column("narration", max_width=46)
        for line in script.lines:
            table.add_row(str(line["index"]), f"{line['start']:.1f}-{line['end']:.1f}",
                          line.get("telop", ""), line.get("narration", ""))
        console.print(table)


@create_app.command("video")
def create_video(
    script_id: int,
    bgm: Path = typer.Option(None, "--bgm"),
    style: str = typer.Option(None, "--style", help="Visual style hint"),
    ken_burns: bool = typer.Option(True, "--ken-burns/--static"),
    visual_mode: str = typer.Option(
        None, "--visual", help="still | animate | footage | auto"
    ),
    narrate: bool = typer.Option(False, "--narrate", help="Synthesise narration"),
):
    """Storyboard, source visuals, and render the video (ワンタッチ編集)."""
    init_db()
    get_settings().ensure_workspace()
    with session_scope() as session:
        script = session.get(Script, script_id)
        if script is None:
            raise typer.BadParameter(f"no script with id {script_id}")
        pipeline = Pipeline(session)
        board = pipeline.draw_storyboard(script, style_hint=style)
        console.print(f"Storyboard {board.id}: {len(board.shots)} shots")

        voice_path = None
        if narrate:
            track = pipeline.narrate(board)
            voice_path = track.path
            console.print(
                f"Narration: {'synthesised' if track.synthesized else 'estimated timings only'}"
                f" ({track.total_duration:.1f}s)"
            )

        visuals = pipeline.generate_visuals(board, visual_mode)
        console.print(f"Visuals: {visuals.counts}")
        for degraded in visuals.degraded:
            console.print(
                f"  [yellow]shot {degraded.shot_index}[/yellow] fell back to "
                f"{degraded.mode.value}: {degraded.note}"
            )

        render = pipeline.render_video(
            board, bgm=bgm, voice=voice_path, ken_burns=ken_burns,
            visual_summary=visuals.summary(),
        )
        console.print(
            f"[green]Rendered[/green] {render.path} "
            f"({render.duration_sec:.1f}s, {render.width}x{render.height}, "
            f"{render.meta.get('telop_cues')} telop cues)"
        )


# ---------------- metrics ----------------

@metrics_app.command("collect")
def metrics_collect(project: str = typer.Option(None, "--project", "-p")):
    """Poll every platform and append a metrics snapshot."""
    init_db()
    with session_scope() as session:
        project_id = _get_project(session, project).id if project else None
        snapshots = Pipeline(session).metrics.collect_all(project_id)
        console.print(f"[green]Collected[/green] {len(snapshots)} snapshots")


# ---------------- pdca ----------------

@pdca_app.command("plan")
def pdca_plan(
    project: str,
    title: str,
    hypothesis: str = typer.Option(..., "--hypothesis", "-h"),
    metric: str = typer.Option("engagement_rate", "--metric"),
    target: float = typer.Option(..., "--target"),
    action: list[str] = typer.Option([], "--action", "-a"),
):
    """Open a PDCA cycle."""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        cycle = Pipeline(session).pdca.plan(
            proj, title, hypothesis, {"metric": metric, "target": target}, list(action)
        )
        console.print(f"[green]Cycle {cycle.id}[/green] planned. baseline={cycle.target.get('baseline')}")


@pdca_app.command("attach")
def pdca_attach(cycle_id: int, publication_ids: list[int]):
    """Attach publications to a cycle (the Do stage)."""
    init_db()
    with session_scope() as session:
        cycle = session.get(PdcaCycle, cycle_id)
        Pipeline(session).pdca.do(cycle, list(publication_ids))
        console.print(f"Cycle {cycle.id}: {len(cycle.publication_ids)} publications attached")


@pdca_app.command("review")
def pdca_review(cycle_id: int):
    """Check the result and decide next actions (Check + Act)."""
    init_db()
    with session_scope() as session:
        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise typer.BadParameter(f"no cycle with id {cycle_id}")
        Pipeline(session).pdca.run_check_act(cycle)
        console.print(f"[bold]{cycle.title}[/bold] -> [green]{cycle.verdict}[/green]")
        console.print(cycle.learnings or "")
        for action in cycle.next_actions or []:
            # Escape the bracket: rich would read "[high]" as a style tag and
            # silently drop the priority.
            priority = action.get("priority", "")
            console.print(rf"  - \[{priority}] {action.get('action')}")


# ---------------- reports ----------------

@report_app.command("research")
def report_research(run_id: int, no_pdf: bool = typer.Option(False, "--no-pdf")):
    """Render a research report."""
    init_db()
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise typer.BadParameter(f"no run with id {run_id}")
        report = Pipeline(session).reports.research_report(run, pdf=not no_pdf)
        _print_report(report)


@report_app.command("performance")
def report_performance(project: str, no_pdf: bool = typer.Option(False, "--no-pdf")):
    """Render a performance report."""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        report = Pipeline(session).reports.performance_report(proj, pdf=not no_pdf)
        _print_report(report)


@report_app.command("pdca")
def report_pdca(cycle_id: int, no_pdf: bool = typer.Option(False, "--no-pdf")):
    """Render a PDCA report."""
    init_db()
    with session_scope() as session:
        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise typer.BadParameter(f"no cycle with id {cycle_id}")
        report = Pipeline(session).reports.pdca_report(cycle, pdf=not no_pdf)
        _print_report(report)


def _print_report(report):
    console.print(f"[green]HTML[/green] {report.html_path}")
    if report.pdf_path:
        console.print(f"[green]PDF [/green] {report.pdf_path}")
    elif (report.context or {}).get("pdf_error"):
        console.print(f"[yellow]PDF skipped:[/yellow] {report.context['pdf_error']}")


# ---------------- templates ----------------

@template_app.command("list")
def template_list():
    """List report templates and whether they are overridden."""
    table = Table(header_style="bold")
    table.add_column("template")
    table.add_column("source")
    for row in list_templates():
        table.add_row(row["name"], "[green]user override[/green]" if row["overridden"] else "built-in")
    console.print(table)
    console.print(f"[dim]User templates: {TemplateRegistry().user_dir}[/dim]")


@template_app.command("eject")
def template_eject(name: str):
    """Copy a built-in template out so you can restyle it."""
    dest = TemplateRegistry().eject(name)
    console.print(f"[green]Ejected[/green] {name} -> {dest}")


if __name__ == "__main__":
    app()


# ---------------- footage ----------------

@footage_app.command("index")
def footage_index(
    directory: Path = typer.Argument(None, help="Defaults to SNSAUTO_FOOTAGE_DIR"),
):
    """Index a folder of clips so shots can be matched to real footage."""
    init_db()
    from .creative.footage import FootageLibrary

    with session_scope() as session:
        assets = FootageLibrary(session).index(directory)
        console.print(f"[green]Indexed[/green] {len(assets)} clips")
        table = Table(header_style="bold")
        table.add_column("file"); table.add_column("dur", justify="right")
        table.add_column("size"); table.add_column("keywords")
        for asset in assets[:25]:
            table.add_row(
                Path(asset.path).name, f"{asset.duration_sec:.1f}s",
                f"{asset.width}x{asset.height}", ", ".join(asset.keywords[:6]),
            )
        console.print(table)


@footage_app.command("list")
def footage_list():
    """Show the indexed footage library."""
    init_db()
    from .models import ClipAsset

    with session_scope() as session:
        table = Table(header_style="bold")
        table.add_column("id", justify="right"); table.add_column("file")
        table.add_column("dur", justify="right"); table.add_column("vertical")
        table.add_column("keywords")
        for asset in session.query(ClipAsset).order_by(ClipAsset.id):
            table.add_row(
                str(asset.id), Path(asset.path).name, f"{asset.duration_sec:.1f}s",
                "yes" if asset.is_vertical else "no", ", ".join(asset.keywords[:6]),
            )
        console.print(table)


# ---------------- worker ----------------

@worker_app.command("run")
def worker_run(
    interval: float = typer.Option(None, "--interval", help="Seconds between ticks"),
    once: bool = typer.Option(False, "--once", help="Run a single tick and exit"),
):
    """Publish scheduled posts and collect metrics on a loop."""
    init_db()
    import logging

    from .db import get_engine
    from .scheduling.worker import Worker
    from sqlalchemy.orm import Session as SASession

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    engine = get_engine()
    worker = Worker(lambda: SASession(engine, expire_on_commit=False), settings)

    if once:
        console.print_json(json.dumps(worker.tick(), default=str))
        return
    console.print(f"[green]worker[/green] {worker.identity} started")
    worker.run(interval)


@worker_app.command("jobs")
def worker_jobs(limit: int = typer.Option(5, "--limit")):
    """Run queued web jobs (useful when the UI runs behind a process manager)."""
    init_db()
    from .db import get_engine
    from .scheduling.jobs import JobRunner
    from sqlalchemy.orm import Session as SASession

    engine = get_engine()
    runner = JobRunner(lambda: SASession(engine, expire_on_commit=False), get_settings())
    console.print_json(json.dumps(runner.run_queued(limit), default=str))


# ---------------- A/B ----------------

@ab_app.command("create")
def ab_create(
    project: str,
    script_id: int,
    dimension: str = typer.Option("hook", "--dimension", "-d"),
    arms: int = typer.Option(2, "--arms", "-n"),
    name: str = typer.Option(None, "--name"),
):
    """Create an A/B experiment from a base script."""
    init_db()
    from .experiments import DIMENSIONS, ExperimentService
    from .llm import build_client

    if dimension not in DIMENSIONS:
        raise typer.BadParameter(f"choose from {sorted(DIMENSIONS)}")

    with session_scope() as session:
        proj = _get_project(session, project)
        base = session.get(Script, script_id)
        if base is None:
            raise typer.BadParameter(f"no script with id {script_id}")
        experiment = ExperimentService(session, build_client()).create(
            proj, name or f"{base.title} A/B", base, dimension, arms
        )
        console.print(f"[green]Experiment {experiment.id}[/green]: {experiment.name}")
        table = Table(header_style="bold")
        table.add_column("arm"); table.add_column("script", justify="right")
        table.add_column("treatment")
        for variant in experiment.variants:
            detail = ", ".join(
                f"{k}={v}" for k, v in variant.treatment.items()
                if k not in ("dimension", "control")
            )
            table.add_row(variant.label, str(variant.script_id), detail or "control")
        console.print(table)


@ab_app.command("attach")
def ab_attach(experiment_id: int, label: str, publication_ids: list[int]):
    """Attach publications to one arm."""
    init_db()
    from .experiments import ExperimentService

    with session_scope() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise typer.BadParameter(f"no experiment with id {experiment_id}")
        variant = next(
            (v for v in experiment.variants if v.label.upper() == label.upper()), None
        )
        if variant is None:
            raise typer.BadParameter(f"no arm {label!r}")
        ExperimentService(session).attach(variant, list(publication_ids))
        console.print(f"arm {variant.label}: {len(variant.publication_ids)} publications")


@ab_app.command("review")
def ab_review(experiment_id: int):
    """Measure the arms and declare a winner - or say why there isn't one."""
    init_db()
    from .experiments import ExperimentService

    with session_scope() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise typer.BadParameter(f"no experiment with id {experiment_id}")
        service = ExperimentService(session)
        service.evaluate(experiment)
        conclusion = experiment.conclusion or {}
        colour = "green" if conclusion.get("verdict") == "winner" else "yellow"
        console.print(f"[{colour}]{conclusion.get('verdict')}[/{colour}] {conclusion.get('reason')}")
        console.print(service.learnings(experiment))


# ---------------- users ----------------

@user_app.command("create")
def user_create(
    email: str,
    password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True),
    name: str = typer.Option(None, "--name"),
    role: str = typer.Option("editor", "--role", help="admin | editor | viewer"),
):
    """Create a web UI account."""
    init_db()
    from .web.auth import AuthError, create_user

    with session_scope() as session:
        try:
            user = create_user(session, email, password, name, role)
        except AuthError as exc:
            raise typer.BadParameter(str(exc)) from exc
        console.print(f"[green]Created[/green] {user.email} ({user.role})")


@user_app.command("list")
def user_list():
    """List web UI accounts."""
    init_db()
    with session_scope() as session:
        table = Table(header_style="bold")
        table.add_column("id", justify="right"); table.add_column("email")
        table.add_column("role"); table.add_column("active"); table.add_column("last login")
        for user in session.query(User).order_by(User.id):
            table.add_row(
                str(user.id), user.email, user.role,
                "yes" if user.is_active else "no",
                user.last_login_at.strftime("%Y-%m-%d %H:%M") if user.last_login_at else "-",
            )
        console.print(table)


@user_app.command("secret")
def user_secret():
    """Generate a signing key for SNSAUTO_SECRET_KEY."""
    import secrets

    console.print(secrets.token_urlsafe(48))
