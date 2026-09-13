"""Turning an exception into something the operator can act on.

The tool is run by people who operate social accounts, not by the people who
wrote it. A Python traceback tells them nothing they can use and looks like
the tool is broken - which, on the very first command a new install runs
(`research run` before any API key exists), is exactly the impression it gave.

Each known failure is mapped to three things: what happened, why, and the one
command or setting that fixes it. Anything unrecognised still prints cleanly
and says how to get the full trace, rather than dumping it unasked.
"""

from __future__ import annotations

from dataclasses import dataclass

# Credentials each platform needs, named the way the operator will find them.
PLATFORM_SETUP = {
    "youtube": (
        "YOUTUBE_API_KEY（検索・コメント取得用。Google Cloud コンソールで発行）",
        "投稿と視聴維持率は /accounts からの連携が必要です。",
    ),
    "instagram": (
        "IG_USER_ID と IG_ACCESS_TOKEN、または /accounts からの連携",
        "相手・自分ともビジネス／クリエイターアカウントである必要があります。",
    ),
    "x": (
        "X_BEARER_TOKEN（検索用）、投稿は /accounts からの連携",
        "検索は有料ティアが必要で、直近7日しか遡れません。",
    ),
    "tiktok": (
        "TIKTOK_ACCESS_TOKEN、または /accounts からの連携",
        "TikTok にキーワード検索APIは存在しません（`snsauto research import` を使います）。",
    ),
}


@dataclass
class Explained:
    title: str
    detail: str
    fix: str = ""


def _platform_of(message: str) -> str | None:
    lowered = message.lower()
    return next((name for name in PLATFORM_SETUP if name in lowered), None)


def explain(exc: BaseException) -> Explained:
    """What went wrong, and the next thing to do about it."""
    name = type(exc).__name__
    message = str(exc)

    if name == "CredentialsMissing":
        platform = _platform_of(message)
        if platform:
            needs, note = PLATFORM_SETUP[platform]
            return Explained(
                title=f"{platform.upper()} の接続情報がありません",
                detail=note,
                fix=(
                    f"{needs} を設定してください。\n"
                    "  設定例は .env.example にあります。"
                    "現在の状態は `snsauto doctor` で確認できます。"
                ),
            )
        return Explained("接続情報がありません", message,
                         "`snsauto doctor` で不足しているものを確認できます。")

    if name == "CapabilityUnavailable":
        return Explained(
            "このプラットフォームでは利用できない機能です", message,
            "実装の不足ではなく、公式APIが提供していない機能です。"
            "`snsauto doctor` に何が使えるかの一覧があります。",
        )

    if name == "AccountScopeError":
        return Explained(
            "投稿先が別のプロジェクトのアカウントです", message,
            "プロジェクトと投稿先の組み合わせを確認してください。"
            "取り違えを防ぐため、この操作は実行されていません。",
        )

    if name == "OAuthError":
        return Explained(
            "アカウント連携に失敗しました", message,
            "`snsauto verify` で接続状態を確認するか、/accounts から再連携してください。",
        )

    if name == "LLMUnavailable":
        return Explained(
            "生成AIが利用できません", message,
            "ANTHROPIC_API_KEY を設定してください。"
            "未設定でも、簡易版の台本生成で動作は続きます。",
        )

    if name == "QualityGateError":
        return Explained(
            "品質チェックで止めました", message,
            "いずれも視聴者全員に見える不具合です。"
            "修正して書き出し直すか、了承のうえで投稿してください。",
        )

    if name == "FFmpegError":
        return Explained(
            "動画処理に失敗しました", message,
            "ffmpeg の状態は `snsauto doctor` で確認できます。",
        )

    if name == "FetchError":
        return Explained(
            "競合動画を取得できませんでした", message,
            "公式APIがメディアURLを返すのは Instagram のみです。"
            "他は SNSAUTO_VIDEO_FETCH_CMD の設定が必要です（既定では無効）。",
        )

    if name == "StorageError":
        return Explained(
            "ストレージへの保存に失敗しました", message,
            "STORAGE_BACKEND の設定を確認してください。"
            "Instagram への投稿には公開HTTPS URLが必須です。",
        )

    if name == "PlatformError":
        lowered = message.lower()
        if "401" in message or "unauthorized" in lowered:
            fix = "トークンが無効か期限切れです。/accounts から再連携してください。"
        elif "403" in message or "forbidden" in lowered:
            fix = "アプリの権限（スコープ）が不足しています。審査状況と設定を確認してください。"
        elif "429" in message or "rate" in lowered:
            fix = "レート制限に当たりました。時間をおいて再実行してください。"
        elif "quota" in lowered:
            fix = "APIの割り当てを使い切っています。翌日以降に再実行してください。"
        else:
            fix = "`snsauto verify` で接続状態を確認できます。"
        return Explained("プラットフォームAPIがエラーを返しました", message, fix)

    if isinstance(exc, FileNotFoundError):
        return Explained("ファイルが見つかりません", str(exc),
                         "パスを確認してください。")

    if isinstance(exc, PermissionError):
        return Explained("ファイルにアクセスできません", str(exc),
                         "実行ユーザーの権限を確認してください。")

    if isinstance(exc, KeyboardInterrupt):
        return Explained("中断しました", "", "")

    return Explained(
        "予期しないエラーが発生しました", f"{name}: {message}",
        "詳細な内容が必要な場合は --debug を付けて再実行してください。",
    )
