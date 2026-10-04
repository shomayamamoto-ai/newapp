"""What is still missing before this install can do anything useful.

A dashboard of four zeros is accurate and useless: it tells a new operator
nothing about whether the tool is broken, unconfigured, or simply unused yet.
This produces the checklist instead - what is done, what is not, and the one
action that moves each item forward.

Ordered by dependency, not by importance: connecting an account before a
project exists leaves the account attached to nothing.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Step:
    key: str
    label: str
    done: bool
    detail: str = ""
    action: str = ""
    href: str = ""
    optional: bool = False


def checklist(session, settings) -> list[Step]:
    from .llm import build_client
    from .models import Project, Publication, ResearchRun, SocialAccount
    from .platforms import capability_matrix

    projects = session.query(Project).count()
    runs = session.query(ResearchRun).count()
    accounts = session.query(SocialAccount).filter_by(is_active=True).count()
    published = session.query(Publication).count()

    matrix = capability_matrix(settings, session)
    searchable = [p for p, caps in matrix.items() if caps.get("search")]
    publishable = [p for p, caps in matrix.items() if caps.get("publish")]

    return [
        Step(
            "project", "プロジェクトを作る", projects > 0,
            f"{projects}件" if projects else "クライアントやブランドごとに1つ作ります",
            "ダッシュボードから作成", "/",
        ),
        Step(
            "search", "調査できる媒体をつなぐ", bool(searchable),
            "、".join(p.upper() for p in searchable) if searchable
            else "YouTube は APIキーのみで調査できます。ここが最短です",
            "接続状況を見る", "/capabilities",
        ),
        Step(
            "research", "競合を調査する", runs > 0,
            f"{runs}回実行済み" if runs else "上位50件を集めて、勝ちパターンを出します",
            "プロジェクトから実行", "/",
        ),
        Step(
            "llm", "生成AIをつなぐ", build_client(settings) is not None,
            settings.llm_model if build_client(settings)
            else "未設定でも動きますが、台本は簡易版になります",
            "ANTHROPIC_API_KEY を設定", "", optional=True,
        ),
        Step(
            "accounts", "投稿先アカウントを連携する", accounts > 0,
            f"{accounts}件連携済み" if accounts
            else "、".join(p.upper() for p in publishable) or "投稿にはOAuth連携が必要です",
            "アカウント連携へ", "/accounts",
        ),
        # Deliberately between connecting and publishing, because that is
        # where it bites: the connection succeeds, the first post succeeds,
        # and on YouTube and TikTok it is private until an audit nobody
        # mentioned has been passed.
        Step(
            "review", "各社の審査状況を把握する", False,
            "連携できても、審査を通るまで投稿が非公開に固定される媒体があります"
            "（YouTube・TikTok）。Instagram は他社アカウントの連携に審査が必要です",
            "`snsauto setup gates` で確認", "", optional=True,
        ),
        Step(
            "publish", "投稿する", published > 0,
            f"{published}件" if published else "台本 → 動画 → 投稿の順に進みます",
            "", "",
        ),
    ]


def progress(steps: list[Step]) -> dict:
    required = [s for s in steps if not s.optional]
    done = [s for s in required if s.done]
    pending = [s for s in required if not s.done]
    return {
        "done": len(done),
        "total": len(required),
        "complete": not pending,
        # The first unfinished step is the only one worth pointing at: the
        # later ones depend on it.
        "next": pending[0] if pending else None,
        "steps": steps,
    }
