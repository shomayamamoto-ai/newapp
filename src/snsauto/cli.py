"""Command line interface."""

from __future__ import annotations

import json
from pathlib import Path

import click
import typer
from rich.console import Console
from rich.table import Table

from .config import get_settings
from .db import init_db, session_scope
from .models import (
    Experiment, PdcaCycle, Platform, Project, ResearchRun, Script, User,
)
from .pipeline import Pipeline
from .research.comments import summarize_comments
from .research.keyword import build_options
from .research.watch import diff_runs
from .platforms import capability_matrix
from .platforms.tiktok import TikTokAdapter
from .reporting.templates import TemplateRegistry, list_templates

app = typer.Typer(
    help="SNS運用自動化ツール — 競合調査から台本・動画・投稿・PDCAまで。",
    no_args_is_help=True,
    # Rich's traceback is for the people who wrote this, not the people who
    # run it. Failures are explained by `main()` below instead.
    pretty_exceptions_enable=False,
)
project_app = typer.Typer(help="プロジェクト（クライアント／ブランド）の管理。", no_args_is_help=True)
research_app = typer.Typer(help="キーワード調査と競合分析。", no_args_is_help=True)
create_app = typer.Typer(help="台本・絵コンテ・動画の生成。", no_args_is_help=True)
metrics_app = typer.Typer(help="実績の収集と確認。", no_args_is_help=True)
pdca_app = typer.Typer(help="PDCAサイクルの管理。", no_args_is_help=True)
report_app = typer.Typer(help="HTML / PDF レポートの出力。", no_args_is_help=True)
template_app = typer.Typer(help="レポートのテンプレート管理。", no_args_is_help=True)
footage_app = typer.Typer(help="実写・ストック素材のライブラリ。", no_args_is_help=True)
worker_app = typer.Typer(help="予約投稿と実績収集の常駐ワーカー。", no_args_is_help=True)
ab_app = typer.Typer(help="A/Bテスト。", no_args_is_help=True)
user_app = typer.Typer(help="Web UI のログインユーザー管理。", no_args_is_help=True)
workspace_app = typer.Typer(help="ワークスペースの容量確認と手動削除。", no_args_is_help=True)
watch_app = typer.Typer(help="競合アカウントの定点ウォッチと差分。", no_args_is_help=True)

app.add_typer(project_app, name="project")
app.add_typer(research_app, name="research")
app.add_typer(watch_app, name="watch")
app.add_typer(create_app, name="create")
app.add_typer(metrics_app, name="metrics")
app.add_typer(pdca_app, name="pdca")
app.add_typer(report_app, name="report")
app.add_typer(template_app, name="template")
app.add_typer(footage_app, name="footage")
app.add_typer(worker_app, name="worker")
app.add_typer(ab_app, name="ab")
app.add_typer(user_app, name="user")
app.add_typer(workspace_app, name="workspace")

console = Console()


def _get_project(session, name: str) -> Project:
    project = session.query(Project).filter_by(name=name).one_or_none()
    if project is None:
        raise typer.BadParameter(f"「{name}」というプロジェクトがありません。`snsauto project create {name}` で作成してください")
    return project


@app.command()
def init():
    """データベースとワークスペースを作成する（最初に一度だけ）。"""
    settings = get_settings()
    settings.ensure_workspace()
    init_db()
    console.print("[green]初期化しました。[/green]")
    console.print(f"  ワークスペース: {settings.workspace}")
    console.print(f"  データベース  : {settings.db_url}")
    console.print("\n[bold]次にやること[/bold]")
    console.print("  1. `snsauto project create <ブランド名>` でプロジェクトを作る")
    console.print("  2. `snsauto doctor` で不足している接続情報を確認する")
    console.print("  3. `snsauto serve` で Web UI を開く（http://127.0.0.1:8000）")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
):
    """Web UI を起動する。"""
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter(
            "Web UI の依存関係が未インストールです。`pip install 'snsauto[web]'` を実行してください"
        ) from exc

    settings = get_settings()
    settings.ensure_workspace()
    init_db()
    console.print(f"[green]起動しました:[/green] http://{host}:{port}")
    uvicorn.run(
        "snsauto.web.app:create_app",
        host=host, port=port, reload=reload, factory=True,
    )


@app.command()
def verify(
    platform: Platform = typer.Option(None, "--platform", "-p", help="1つだけ検証"),
    project: str = typer.Option(None, "--project"),
    keyword: str = typer.Option("news", "--keyword", help="検索の確認に使う語"),
):
    """各プラットフォームへの実接続を読み取り専用で確認する。投稿はしません。"""
    init_db()
    from .platforms.verify import ConnectionVerifier

    with session_scope() as session:
        project_id = _get_project(session, project).id if project else None
        verifier = ConnectionVerifier(session, get_settings())
        reports = (
            [verifier.verify(platform, project_id, keyword=keyword)] if platform
            else verifier.verify_all(project_id, keyword=keyword)
        )

        marks = {"ok": "[green]OK[/green]", "fail": "[red]NG[/red]",
                 "skip": "[dim]--[/dim]"}
        problems = 0
        for report in reports:
            table = Table(title=report.platform.upper(), header_style="bold",
                          title_justify="left")
            table.add_column("項目", width=12)
            table.add_column("", width=4)
            table.add_column("結果", max_width=60)
            for check in report.checks:
                table.add_row(check.name, marks[check.status], check.detail)
                if check.fix:
                    table.add_row("", "", f"[yellow]→ {check.fix}[/yellow]")
            console.print(table)
            problems += len(report.failed)

        if problems:
            console.print(f"\n[red]{problems}件の問題[/red]があります。"
                          "上の → の指示を確認してください。")
            raise typer.Exit(1)
        console.print("\n[green]接続に問題は見つかりませんでした。[/green]")


@app.command()
def doctor():
    """いま何ができて、何が足りないかを表示する。"""
    from .errors import PLATFORM_SETUP
    from .llm import build_client
    from .media.ffmpeg import ffmpeg_path
    from .reporting.pdf import chromium_executable
    from .workspace import warning as workspace_warning

    settings = get_settings()
    matrix = capability_matrix(settings)

    table = Table(title="プラットフォーム", header_style="bold", title_justify="left")
    table.add_column("媒体"); table.add_column("検索", justify="center")
    table.add_column("投稿", justify="center"); table.add_column("実績", justify="center")
    table.add_column("足りないもの", max_width=46)
    todo: list[str] = []
    for platform, caps in matrix.items():
        missing = ""
        if not any(caps[c] for c in ("search", "publish", "insights")):
            needs, _ = PLATFORM_SETUP.get(platform, ("", ""))
            missing = f"[yellow]{needs}[/yellow]"
            todo.append(platform)
        table.add_row(
            platform.upper(),
            *["[green]可[/green]" if caps[c] else "[dim]—[/dim]"
              for c in ("search", "publish", "insights")],
            missing,
        )
    console.print(table)

    # Local tooling. Each line says what stops working without it, because
    # "chromium: not found" does not tell anyone whether that matters.
    console.print("\n[bold]ローカル環境[/bold]")
    try:
        console.print(f"  ffmpeg      [green]可[/green]  {Path(ffmpeg_path()).name}")
    except Exception as exc:
        console.print(f"  ffmpeg      [red]不可[/red]  {exc}")
        console.print("              [yellow]動画の生成とカット検出ができません[/yellow]")

    chromium = chromium_executable()
    console.print(
        f"  Chromium    [green]可[/green]  {Path(chromium).name}" if chromium else
        "  Chromium    [yellow]未[/yellow]  PDF出力は不可。HTMLレポートは出せます"
    )

    if build_client(settings):
        console.print(f"  生成AI      [green]可[/green]  {settings.llm_model}")
    else:
        console.print("  生成AI      [yellow]未[/yellow]  ANTHROPIC_API_KEY 未設定")
        console.print("              [yellow]台本は簡易版で生成されます（動作はします）[/yellow]")

    ok, detail = _ocr_state()
    console.print(
        f"  テロップOCR [green]可[/green]  {detail}" if ok else
        f"  テロップOCR [yellow]未[/yellow]  {detail or 'tesseract 未インストール'}"
    )
    if not ok:
        console.print("              [yellow]apt install tesseract-ocr tesseract-ocr-jpn[/yellow]")

    note = workspace_warning(settings.workspace)
    if note:
        console.print(f"\n[yellow]{note}[/yellow]")

    console.print("\n[bold]次にやること[/bold]")
    if todo:
        console.print(f"  1. 未設定の媒体（{', '.join(t.upper() for t in todo)}）の接続情報を "
                      ".env に設定するか、Web UI の /accounts から連携する")
        console.print("  2. `snsauto verify` で実際に繋がるか確認する（読み取り専用）")
        console.print("  3. `snsauto research run <プロジェクト> \"キーワード\"` で調査を開始する")
    else:
        console.print("  `snsauto verify` で実接続を確認し、`snsauto research run` から始められます。")

    console.print("\n[dim]TikTok の検索は原理的に使えません（公開キーワード検索APIが"
                  "存在しないため）。`snsauto research import` でCSVを取り込んでください。[/dim]")


def _ocr_state() -> tuple[bool, str]:
    from .web.app import _ocr_status

    return _ocr_status()


# ---------------- project ----------------

@project_app.command("create")
def project_create(
    name: str,
    description: str = typer.Option("", "--description", "-d"),
    brand_profile: Path = typer.Option(None, "--brand-profile", help="JSON file: tone, persona, banned words"),
):
    """プロジェクト（クライアント／ブランド）を作る。"""
    init_db()
    profile = json.loads(brand_profile.read_text(encoding="utf-8")) if brand_profile else {}
    with session_scope() as session:
        if session.query(Project).filter_by(name=name).one_or_none():
            raise typer.BadParameter(f"プロジェクト「{name}」は既に存在します")
        project = Project(name=name, description=description, brand_profile=profile)
        session.add(project)
        session.flush()
        console.print(f"[green]プロジェクトを作成しました:[/green] {project.name}（id={project.id}）")


@project_app.command("list")
def project_list():
    """プロジェクト一覧。"""
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


@metrics_app.command("retention")
def metrics_retention(publication_id: int):
    """視聴維持カーブを、その動画のカット割りと重ねて表示する。"""
    init_db()
    from .analytics.collect import retention_report
    from .models import Publication

    with session_scope() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            raise typer.BadParameter(f"投稿ID {publication_id} が見つかりません")
        report = retention_report(publication)
        if not report.get("measured"):
            console.print(f"[yellow]{report['reason']}[/yellow]")
            raise typer.Exit(0)

        console.print(f"[bold]{report['summary']}[/bold]\n")
        table = Table(title="離脱点", header_style="bold", title_justify="left")
        table.add_column("時刻", justify="right"); table.add_column("離脱", justify="right")
        table.add_column("残存", justify="right"); table.add_column("その時の画面", max_width=52)
        for fall in report["drop_offs"]:
            shot = fall.get("on_screen") or {}
            telop = (shot.get("telop") or "").replace("\n", " ")
            table.add_row(
                f"{fall['from_sec']:.1f}s", f"-{fall['lost']:.0%}",
                f"{fall['remaining']:.0%}",
                f"{shot.get('index', 0) + 1}カット目 「{telop[:24]}」"
                f"{shot.get('telop_chars', 0)}字/{shot.get('hold_sec', 0)}秒"
                if shot else "-",
            )
        console.print(table)
        if report.get("relative_performance") is not None:
            console.print(f"同尺動画との比較: {report['relative_performance']:.2f} "
                          "[dim](1.0で平均並み)[/dim]")
        for note in report.get("notes") or []:
            console.print(f"  [yellow]・{note}[/yellow]")


# ---------------- workspace ----------------

@workspace_app.command("usage")
def workspace_usage():
    """ワークスペースが何をどれだけ抱えているかを表示する。"""
    from .workspace import free_bytes, usage, warning

    settings = get_settings()
    rows = usage(settings.workspace)
    table = Table(title=str(settings.workspace), header_style="bold", title_justify="left")
    table.add_column("種別"); table.add_column("内容", max_width=34)
    table.add_column("件数", justify="right"); table.add_column("容量", justify="right")
    table.add_column("最古", justify="right"); table.add_column("削除可")
    for row in rows:
        table.add_row(
            row.category, row.label, str(row.files), f"{row.megabytes:,.1f}MB",
            f"{row.oldest_days:.0f}日" if row.oldest_days else "-",
            "[green]可[/green]" if row.disposable else "[yellow]要注意[/yellow]",
        )
    console.print(table)
    free = free_bytes(settings.workspace)
    if free is not None:
        console.print(f"ディスク空き: {free / 1073741824:.1f}GB")
    note = warning(settings.workspace)
    if note:
        console.print(f"[yellow]{note}[/yellow]")
    console.print("[dim]自動削除は行いません。削除は `snsauto workspace clean` で明示的に実行してください。[/dim]")


@workspace_app.command("clean")
def workspace_clean(
    older_than: float = typer.Option(0.0, "--older-than", help="この日数より古いファイルのみ"),
    category: list[str] = typer.Option(None, "--category", "-c",
                                       help="既定はキャッシュ系のみ。完成動画やレポートは対象外"),
    apply: bool = typer.Option(False, "--apply", help="実際に削除する（既定は確認のみ）"),
):
    """キャッシュを手動で削除する。既定は削除内容の確認だけ。"""
    from .workspace import apply_clean, plan_clean

    settings = get_settings()
    removals = plan_clean(settings.workspace, list(category) if category else None, older_than)
    if not removals:
        console.print("削除対象はありません。")
        raise typer.Exit(0)

    total = sum(r.bytes for r in removals)
    console.print(f"対象 {len(removals)}件 / {total / 1048576:,.1f}MB")
    for removal in removals[:10]:
        console.print(f"  {removal.bytes / 1048576:>8,.1f}MB  {removal.age_days:>5.0f}日前  "
                      f"{removal.path.name}")
    if len(removals) > 10:
        console.print(f"  [dim]... 他 {len(removals) - 10}件[/dim]")

    if not apply:
        console.print("\n[dim]確認のみです。実行するには --apply を付けてください。[/dim]")
        raise typer.Exit(0)

    count, freed = apply_clean(removals)
    console.print(f"[green]{count}件 / {freed / 1048576:,.1f}MB を削除しました。[/green]")


# ---------------- watch ----------------

@watch_app.command("add")
def watch_add(
    project: str,
    handle: str,
    platform: Platform = typer.Option(Platform.YOUTUBE, "--platform", "-p"),
    label: str = typer.Option(None, "--label"),
):
    """定点ウォッチする競合を登録する。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        account = Pipeline(session).watch.add(proj, platform, handle, label)
        console.print(
            f"[green]定点ウォッチに追加しました:[/green] {account.display}"
            f"（{platform.value} / id={account.id}）"
        )


@watch_app.command("list")
def watch_list(project: str):
    """ウォッチ中の競合と、最終確認日時を表示する。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        table = Table(title=f"Watched competitors - {proj.name}", header_style="bold")
        for column in ("id", "platform", "handle", "label", "last checked"):
            table.add_column(column)
        for account in proj.competitor_accounts:
            table.add_row(
                str(account.id), account.platform.value, f"@{account.handle}",
                account.label or "-",
                account.last_checked_at.strftime("%Y-%m-%d %H:%M")
                if account.last_checked_at else "[dim]never[/dim]",
            )
        console.print(table)


@watch_app.command("sweep")
def watch_sweep(
    project: str,
    limit: int = typer.Option(25, "--limit", "-n"),
):
    """全競合をスイープし、前回からの変化を表示する。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        outcome = Pipeline(session).sweep_competitors(proj, limit=limit)
        console.print(f"[green]{len(outcome['runs'])}件の競合をスイープしました[/green]")
        for name, reason in (outcome.get("failed") or {}).items():
            console.print(f"  [yellow]{name}[/yellow]: {reason}")
        for name, trend in (outcome.get("trends") or {}).items():
            if not trend.get("comparable"):
                console.print(f"  [dim]{name}: {trend.get('reason')}[/dim]")
                continue
            console.print(f"  [bold]{name}[/bold]: {trend['headline']}")
        if not outcome.get("trends"):
            console.print(
                "[dim]初回スイープのため比較対象がありません。"
                "次回から差分が出ます。[/dim]"
            )


@watch_app.command("diff")
def watch_diff(run_a: int, run_b: int):
    """同じ対象の2回の調査を比較する。"""
    init_db()
    with session_scope() as session:
        first = session.get(ResearchRun, run_a)
        second = session.get(ResearchRun, run_b)
        if not first or not second:
            raise typer.BadParameter("調査が見つかりません")
        result = diff_runs(first, second)
        if not result.get("comparable"):
            console.print(f"[yellow]{result.get('reason')}[/yellow]")
            raise typer.Exit(1)
        console.print(f"[bold]{result['headline']}[/bold]")
        for key in ("engagement", "views", "duration", "followers"):
            change = result.get(key)
            if change:
                mark = "*" if change["material"] else " "
                console.print(
                    f" {mark} {key:<12} {change['before']} -> {change['after']} "
                    f"({change['change']:+.1%})"
                )
        console.print("[dim]* = ノイズ水準(±15%)を超えた変化[/dim]")


# ---------------- research ----------------

@research_app.command("run")
def research_run(
    project: str,
    keyword: str,
    platform: Platform = typer.Option(Platform.YOUTUBE, "--platform", "-p"),
    limit: int = typer.Option(50, "--limit", "-n"),
    analyze: bool = typer.Option(True, "--analyze/--no-analyze", help="Break down the top performers"),
    within_days: int = typer.Option(None, "--within-days", help="Only posts published in the last N days"),
    duration_band: str = typer.Option(None, "--duration", help="short | medium | long"),
    order: str = typer.Option(None, "--order", help="relevance | date | views"),
    comments: bool = typer.Option(False, "--comments", help="Also pull comment text on the top posts"),
):
    """キーワードの上位N件を集めて順位付けする。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        pipeline = Pipeline(session)
        options = build_options(pipeline.settings)
        if within_days is not None:
            options.published_within_days = within_days
        if duration_band:
            options.video_duration = duration_band
        if order:
            options.order = order

        run = pipeline.research.run(proj, keyword, platform, limit=limit,
                                    options=options)
        console.print(f"[green]{len(run.posts)}件を取得しました[/green]（調査ID={run.id}）")
        _print_filters(run)
        if analyze:
            n = pipeline.analyze_structures(run)
            console.print(f"上位{n}件の構成を分析しました")
        if comments:
            mined = pipeline.mine_comments(run)
            console.print(f"コメントを{mined}件取得しました")
        _print_top(run)


def _print_filters(run: ResearchRun) -> None:
    """Say which filters the platform honoured - and which it silently did not."""
    filters = run.filters or {}
    ignored = filters.get("ignored") or {}
    dropped = filters.get("dropped") or {}
    if filters.get("applied"):
        console.print(f"[dim]適用した絞り込み: {filters['applied']}[/dim]")
    if ignored:
        console.print(
            f"[yellow]{run.platform.value} で無視した条件:[/yellow] {ignored} "
            "[dim]（このAPIでは指定できない条件です）[/dim]"
        )
    if dropped:
        console.print(f"[dim]除外 {sum(dropped.values())}件: {dropped}[/dim]")


@research_app.command("import")
def research_import(
    project: str,
    keyword: str,
    csv_path: Path,
    platform: Platform = typer.Option(Platform.TIKTOK, "--platform", "-p"),
):
    """CSVから競合投稿を取り込む（検索APIが無い媒体向け）。"""
    init_db()
    records = TikTokAdapter.ingest_manual(csv_path)
    for r in records:
        r.platform = platform
    with session_scope() as session:
        proj = _get_project(session, project)
        run = Pipeline(session).collect_research(
            proj, keyword, platform, len(records), records=records
        )
        console.print(f"[green]{len(run.posts)}件を取り込みました[/green]（調査ID={run.id}）")
        _print_top(run)


@research_app.command("audio")
def research_audio(
    run_id: int,
    top_n: int = typer.Option(12, "--top"),
):
    """取得済みの動画から、同じ音源を使っている投稿を探す。"""
    init_db()
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            raise typer.BadParameter(f"調査ID {run_id} が見つかりません")
        result = Pipeline(session).analyze_shared_audio(run, top_n=top_n)
        if not result.get("usable"):
            console.print(f"[yellow]{result.get('reason')}[/yellow]")
            raise typer.Exit(0)

        console.print(f"{result['analysed']}本を解析（対象 {result['considered']}本）")
        if result.get("failed"):
            console.print(f"[dim]解析できず: {len(result['failed'])}本[/dim]")
        groups = result["shared_groups"]
        if not groups:
            console.print("同じ音源を使っている組み合わせは見つかりませんでした。")
            raise typer.Exit(0)
        for i, group in enumerate(groups, 1):
            console.print(f"  [bold]グループ{i}[/bold]（{len(group)}本）: "
                          + ", ".join(group))
        console.print(f"[dim]{result['note']}[/dim]")


@research_app.command("comments")
def research_comments(
    run_id: int,
    top_n: int = typer.Option(10, "--top", help="Mine this many top posts"),
):
    """上位投稿のコメント本文を取得し、何を聞かれているかを集計する。"""
    init_db()
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if not run:
            raise typer.BadParameter(f"調査ID {run_id} が見つかりません")
        added = Pipeline(session).comments.mine_run(run, top_n=top_n)
        comments = [c for p in run.posts for c in p.comments_mined]
        summary = summarize_comments(comments)
        console.print(f"[green]新規コメントを{added}件取得しました[/green] "
                      f"（累計 {summary.get('count', 0)}件）")
        if not summary.get("count"):
            console.print(
                "[dim]このランのプラットフォームではコメント本文を取得できません"
                "（Instagram は自社投稿のみ、TikTok は検索自体が不可）。[/dim]"
            )
            return
        console.print(f"質問の割合: {summary['question_share']:.0%}  "
                      f"内訳: {summary['intent_mix']}")
        table = Table(title="よく聞かれていること", header_style="bold")
        table.add_column("likes", justify="right")
        table.add_column("comment", max_width=70)
        for row in summary["top_questions"]:
            table.add_row(str(row["likes"]), row["text"].replace("\n", " "))
        console.print(table)


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
    publish: list[Platform] = typer.Option([], "--publish", help="Post to every connected account on these platforms"),
    account: list[int] = typer.Option([], "--account", help="Post to these account ids only"),
    bgm: Path = typer.Option(None, "--bgm"),
    live: bool = typer.Option(False, "--live", help="Actually publish (default is dry run)"),
    csv_path: Path = typer.Option(None, "--csv", help="Use a CSV instead of the search API"),
):
    """調査から動画・レポートまで一気に実行する。"""
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
            bgm=bgm, dry_run=not live, account_ids=list(account) or None,
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
    """台本を書く。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        run = session.get(ResearchRun, run_id) if run_id else None
        script = Pipeline(session).write_script(proj, keyword, platform, duration, run)
        console.print(f"[green]台本 {script.id}[/green]: {script.title}")
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
    """絵コンテ・素材・書き出しまで一括で行う（ワンタッチ編集）。"""
    init_db()
    get_settings().ensure_workspace()
    with session_scope() as session:
        script = session.get(Script, script_id)
        if script is None:
            raise typer.BadParameter(f"台本ID {script_id} が見つかりません")
        pipeline = Pipeline(session)
        board = pipeline.draw_storyboard(script, style_hint=style)
        console.print(f"絵コンテ {board.id}: {len(board.shots)}カット")

        voice_path = None
        if narrate:
            track = pipeline.narrate(board)
            voice_path = track.path
            console.print(
                f"ナレーション: {'音声を合成しました' if track.synthesized else '尺の推定のみ（音声なし）'}"
                f" ({track.total_duration:.1f}s)"
            )

        visuals = pipeline.generate_visuals(board, visual_mode)
        console.print(f"素材: {visuals.counts}")
        for degraded in visuals.degraded:
            console.print(
                f"  [yellow]カット {degraded.shot_index}[/yellow] は次の方法に切り替えました: "
                f"{degraded.mode.value}: {degraded.note}"
            )

        render = pipeline.render_video(
            board, bgm=bgm, voice=voice_path, ken_burns=ken_burns,
            visual_summary=visuals.summary(),
        )
        console.print(
            f"[green]書き出しました:[/green] {render.path} "
            f"（{render.duration_sec:.1f}秒 / {render.width}x{render.height} / "
            f"テロップ {render.meta.get('telop_cues')}箇所）"
        )


# ---------------- metrics ----------------

@metrics_app.command("collect")
def metrics_collect(project: str = typer.Option(None, "--project", "-p")):
    """各媒体の実績を取得してスナップショットを追加する。"""
    init_db()
    with session_scope() as session:
        project_id = _get_project(session, project).id if project else None
        snapshots = Pipeline(session).metrics.collect_all(project_id)
        console.print(f"[green]実績を{len(snapshots)}件取得しました[/green]")


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
    """PDCAサイクルを開始する（Plan）。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        cycle = Pipeline(session).pdca.plan(
            proj, title, hypothesis, {"metric": metric, "target": target}, list(action)
        )
        console.print(f"[green]PDCAサイクル {cycle.id} を作成しました[/green]（ベースライン={cycle.target.get('baseline')}）")


@pdca_app.command("attach")
def pdca_attach(cycle_id: int, publication_ids: list[int]):
    """サイクルに投稿を紐づける（Do）。"""
    init_db()
    with session_scope() as session:
        cycle = session.get(PdcaCycle, cycle_id)
        Pipeline(session).pdca.do(cycle, list(publication_ids))
        console.print(f"サイクル {cycle.id}: 投稿を{len(cycle.publication_ids)}件紐づけました")


@pdca_app.command("review")
def pdca_review(cycle_id: int):
    """結果を判定し、次のアクションを決める（Check + Act）。"""
    init_db()
    with session_scope() as session:
        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise typer.BadParameter(f"PDCAサイクルID {cycle_id} が見つかりません")
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
    """調査レポートを出力する。"""
    init_db()
    with session_scope() as session:
        run = session.get(ResearchRun, run_id)
        if run is None:
            raise typer.BadParameter(f"調査ID {run_id} が見つかりません")
        report = Pipeline(session).reports.research_report(run, pdf=not no_pdf)
        _print_report(report)


@report_app.command("performance")
def report_performance(project: str, no_pdf: bool = typer.Option(False, "--no-pdf")):
    """実績レポートを出力する。"""
    init_db()
    with session_scope() as session:
        proj = _get_project(session, project)
        report = Pipeline(session).reports.performance_report(proj, pdf=not no_pdf)
        _print_report(report)


@report_app.command("pdca")
def report_pdca(cycle_id: int, no_pdf: bool = typer.Option(False, "--no-pdf")):
    """PDCAレポートを出力する。"""
    init_db()
    with session_scope() as session:
        cycle = session.get(PdcaCycle, cycle_id)
        if cycle is None:
            raise typer.BadParameter(f"PDCAサイクルID {cycle_id} が見つかりません")
        report = Pipeline(session).reports.pdca_report(cycle, pdf=not no_pdf)
        _print_report(report)


def _print_report(report):
    console.print(f"[green]HTML:[/green] {report.html_path}")
    if report.pdf_path:
        console.print(f"[green]PDF :[/green] {report.pdf_path}")
    elif (report.context or {}).get("pdf_error"):
        console.print(f"[yellow]PDF出力をスキップしました:[/yellow] {report.context['pdf_error']}")


# ---------------- templates ----------------

@template_app.command("list")
def template_list():
    """レポートのテンプレート一覧と、上書きの有無を表示する。"""
    table = Table(header_style="bold")
    table.add_column("template")
    table.add_column("source")
    for row in list_templates():
        table.add_row(row["name"], "[green]user override[/green]" if row["overridden"] else "built-in")
    console.print(table)
    console.print(f"[dim]ユーザーテンプレート: {TemplateRegistry().user_dir}[/dim]")


@template_app.command("eject")
def template_eject(name: str):
    """組み込みテンプレートを取り出して、デザインを差し替えられるようにする。"""
    dest = TemplateRegistry().eject(name)
    console.print(f"[green]テンプレートを取り出しました:[/green] {name} → {dest}")


if __name__ == "__main__":
    app()


# ---------------- footage ----------------

@footage_app.command("index")
def footage_index(
    directory: Path = typer.Argument(None, help="Defaults to SNSAUTO_FOOTAGE_DIR"),
):
    """実写素材のフォルダを索引化し、カットに割り当てられるようにする。"""
    init_db()
    from .creative.footage import FootageLibrary

    with session_scope() as session:
        assets = FootageLibrary(session).index(directory)
        console.print(f"[green]素材を{len(assets)}件索引しました[/green]")
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
    """索引済みの素材ライブラリを表示する。"""
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
    """予約投稿の実行と実績収集を繰り返す（常駐）。"""
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
    console.print(f"[green]ワーカー[/green] {worker.identity} を開始しました")
    worker.run(interval)


@worker_app.command("jobs")
def worker_jobs(limit: int = typer.Option(5, "--limit")):
    """Web UI から積まれたジョブを処理する（プロセス管理下で運用する場合）。"""
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
    """台本をもとにA/Bテストを作成する。"""
    init_db()
    from .experiments import DIMENSIONS, ExperimentService
    from .llm import build_client

    if dimension not in DIMENSIONS:
        raise typer.BadParameter(f"次のいずれかを指定してください: {sorted(DIMENSIONS)}")

    with session_scope() as session:
        proj = _get_project(session, project)
        base = session.get(Script, script_id)
        if base is None:
            raise typer.BadParameter(f"台本ID {script_id} が見つかりません")
        experiment = ExperimentService(session, build_client()).create(
            proj, name or f"{base.title} A/B", base, dimension, arms
        )
        console.print(f"[green]A/Bテスト {experiment.id} を作成しました[/green]: {experiment.name}")
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
    """片方の群に投稿を紐づける。"""
    init_db()
    from .experiments import ExperimentService

    with session_scope() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise typer.BadParameter(f"A/BテストID {experiment_id} が見つかりません")
        variant = next(
            (v for v in experiment.variants if v.label.upper() == label.upper()), None
        )
        if variant is None:
            raise typer.BadParameter(f"群「{label}」が見つかりません")
        ExperimentService(session).attach(variant, list(publication_ids))
        console.print(f"群 {variant.label}: 投稿{len(variant.publication_ids)}件")


@ab_app.command("review")
def ab_review(experiment_id: int):
    """両群を比較して勝者を判定する（判定できない場合はその理由を出す）。"""
    init_db()
    from .experiments import ExperimentService

    with session_scope() as session:
        experiment = session.get(Experiment, experiment_id)
        if experiment is None:
            raise typer.BadParameter(f"A/BテストID {experiment_id} が見つかりません")
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
    """Web UI のログインユーザーを作る。"""
    init_db()
    from .web.auth import AuthError, create_user

    with session_scope() as session:
        try:
            user = create_user(session, email, password, name, role)
        except AuthError as exc:
            raise typer.BadParameter(str(exc)) from exc
        console.print(f"[green]ユーザーを作成しました:[/green] {user.email}（{user.role}）")


@user_app.command("list")
def user_list():
    """Web UI のログインユーザー一覧。"""
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
    """SNSAUTO_SECRET_KEY 用の署名鍵を生成する。"""
    import secrets

    console.print(secrets.token_urlsafe(48))


# ---------------- database ----------------

db_app = typer.Typer(help="Schema migrations.", no_args_is_help=True)
app.add_typer(db_app, name="db")


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")):
    """データベースのマイグレーションを適用する。"""
    from alembic import command

    from .db import _alembic_config

    command.upgrade(_alembic_config(), revision)
    console.print(f"[green]{revision} まで適用しました[/green]")


@db_app.command("current")
def db_current():
    """適用済みのリビジョンを表示する。"""
    from alembic import command

    from .db import _alembic_config

    command.current(_alembic_config(), verbose=True)


@db_app.command("revision")
def db_revision(message: str = typer.Option(..., "--message", "-m")):
    """モデルの変更からマイグレーションを自動生成する。"""
    from alembic import command

    from .db import _alembic_config

    command.revision(_alembic_config(), message=message, autogenerate=True)


# ---------------- connected accounts ----------------

account_app = typer.Typer(help="Connected SNS accounts.", no_args_is_help=True)
app.add_typer(account_app, name="account")


@account_app.command("list")
def account_list():
    """連携済みアカウントと、トークンの残り期間を表示する。"""
    init_db()
    from .models import SocialAccount
    from .platforms.accounts import AccountService

    with session_scope() as session:
        service = AccountService(session)
        table = Table(header_style="bold")
        table.add_column("id", justify="right")
        table.add_column("platform")
        table.add_column("account")
        table.add_column("expires")
        table.add_column("24h posts", justify="right")
        table.add_column("state")

        accounts = session.query(SocialAccount).order_by(SocialAccount.id).all()
        if not accounts:
            console.print("[yellow]連携済みのアカウントがありません。[/yellow] "
                          "Connect them from the web UI: /accounts")
            return
        for account in accounts:
            remaining = account.seconds_until_expiry()
            expires = (
                "no expiry" if remaining is None
                else ("expired" if remaining <= 0 else f"{remaining / 3600:.1f}h")
            )
            state = (
                "[red]refresh failed[/red]" if account.refresh_error
                else ("[dim]disconnected[/dim]" if not account.is_active
                      else "[green]connected[/green]")
            )
            table.add_row(
                str(account.id), account.platform.value,
                account.display_name or account.external_id, expires,
                str(service.posts_in_window(account.platform, account.id)), state,
            )
        console.print(table)


@account_app.command("connect-url")
def account_connect_url(platform: Platform):
    """ブラウザで開く連携用URLを表示する。

    Web UI にまだ到達できない場合に使います。表示されたURLを開いて認可すると、
    コールバックがこのインストールに戻ってきて連携が完了します。
    """
    from .platforms.oauth import OAuthError, get_provider

    try:
        start = get_provider(platform, get_settings()).start()
    except OAuthError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(start.url)
    console.print(f"[dim]state（照合用の文字列）: {start.state}[/dim]")


@account_app.command("refresh")
def account_refresh(account_id: int = typer.Argument(None, help="Omit to refresh all due")):
    """期限が近いアクセストークンを更新する。"""
    init_db()
    from .models import SocialAccount
    from .platforms.accounts import AccountService
    from .platforms.oauth import OAuthError

    with session_scope() as session:
        service = AccountService(session)
        if account_id:
            account = session.get(SocialAccount, account_id)
            if account is None:
                raise typer.BadParameter(f"アカウントID {account_id} が見つかりません")
            try:
                service.refresh(account)
                console.print(f"[green]更新しました[/green]（有効期限 {account.expires_at}）")
            except OAuthError as exc:
                raise typer.BadParameter(str(exc)) from exc
            return
        results = service.refresh_due()
        console.print_json(json.dumps(results, default=str))


@account_app.command("limits")
def account_limits():
    """各媒体の投稿枠をどれだけ使ったかを表示する。"""
    init_db()
    from .platforms.accounts import AccountService

    with session_scope() as session:
        service = AccountService(session)
        table = Table(header_style="bold")
        table.add_column("platform")
        table.add_column("used (24h)", justify="right")
        table.add_column("cap", justify="right")
        table.add_column("can post")
        table.add_column("note")
        for platform in Platform:
            rate = service.check_rate(platform)
            table.add_row(
                platform.value, str(rate["used_24h"]),
                str(rate["cap"] or "-"),
                "[green]yes[/green]" if rate["allowed"] else "[red]no[/red]",
                rate["reason"] or "",
            )
        console.print(table)
        console.print(
            "[dim]Instagram は自身の残り枠を返します。"
            "投稿時にアダプタが毎回問い合わせるため、別途の確認コマンドは不要です。[/dim]"
        )


def main() -> None:
    """Entry point. Explains failures instead of printing a traceback.

    `--debug` anywhere on the command line re-raises, so the full trace is
    still one flag away when it is actually wanted.
    """
    import sys

    from .errors import explain

    debug = "--debug" in sys.argv
    if debug:
        sys.argv = [a for a in sys.argv if a != "--debug"]
    try:
        app()
    except (typer.Exit, typer.Abort, SystemExit, click.exceptions.ClickException):
        raise
    except BaseException as exc:  # noqa: BLE001 - this is the boundary
        if debug:
            raise
        explained = explain(exc)
        if isinstance(exc, KeyboardInterrupt):
            console.print("\n[dim]中断しました。[/dim]")
            raise SystemExit(130) from None
        console.print(f"\n[red]{explained.title}[/red]")
        if explained.detail:
            console.print(f"  {explained.detail}")
        if explained.fix:
            for line in explained.fix.split("\n"):
                console.print(f"  [yellow]{line}[/yellow]" if line.strip() else "")
        raise SystemExit(1) from None
