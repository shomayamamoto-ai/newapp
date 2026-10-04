"""Everything between "I have the code" and "I can post for a client".

Connecting an account is not the hard part. The hard part is that each
platform gates *public* posting and *analytics* behind a review that takes
weeks, and none of those gates announce themselves: the OAuth flow succeeds,
the API answers, and what comes back is a private video, an empty metric, or a
list with one account missing from it. Operators discover them one at a time,
in production, each costing another week.

So they are written down here, in the order they bite, with what each one
blocks and what it costs to clear. Two rules for this file:

* **Nothing here is inferred from a successful call.** A platform's own
  documentation is the authority on its review terms, and those terms change.
  Every gate carries the URL where it is stated, so the operator confirms
  rather than trusts this file's copy.
* **What can be checked locally is checked**, and the rest is reported as
  unknown rather than assumed. "未確認" is a useful answer; a wrong "OK" is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Platform

DONE, TODO, UNKNOWN = "done", "todo", "unknown"


@dataclass
class Requirement:
    """One thing that has to be true, and where it stands."""

    key: str
    label: str
    state: str
    detail: str = ""
    action: str = ""


@dataclass
class Gate:
    """A platform review that stands between a working connection and real use.

    ``blocks`` is the honest part: what silently stops working while the gate
    is closed. It is never "nothing".
    """

    name: str
    applies_to: str
    blocks: str
    cost: str
    typical_wait: str
    source: str


# --- what an app needs before any connect flow can start -------------------

@dataclass
class AppSetup:
    console: str
    console_url: str
    env_vars: tuple[str, ...]
    note: str = ""


APP_SETUP: dict[Platform, AppSetup] = {
    Platform.INSTAGRAM: AppSetup(
        "Meta for Developers", "https://developers.facebook.com/apps/",
        ("FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"),
        "アプリに「Facebookログイン」と「Instagram Graph API」の2製品を追加します。"
        "Instagram 用の資格情報ではなく Facebook アプリの資格情報である点に注意してください。",
    ),
    Platform.YOUTUBE: AppSetup(
        "Google Cloud Console", "https://console.cloud.google.com/apis/credentials",
        ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET"),
        "YouTube Data API v3 と YouTube Analytics API の両方を有効化します。"
        "検索だけなら YOUTUBE_API_KEY のみで動きます。",
    ),
    Platform.TIKTOK: AppSetup(
        "TikTok for Developers", "https://developers.tiktok.com/apps",
        ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
        "Content Posting API の利用申請が別途必要です。",
    ),
    Platform.X: AppSetup(
        "X Developer Portal", "https://developer.x.com/en/portal/dashboard",
        ("X_API_KEY", "X_API_SECRET"),
        "検索には X_BEARER_TOKEN と有料ティアが必要です（直近7日のみ）。",
    ),
}


# --- conditions on the account itself, which no API call can check first ----

ACCOUNT_PREREQUISITES: dict[Platform, tuple[Requirement, ...]] = {
    Platform.INSTAGRAM: (
        Requirement(
            "ig_business", "ビジネス／クリエイターアカウントである", UNKNOWN,
            "個人アカウントは Graph API から一切見えません。投稿も実績取得もできません。",
            "Instagramアプリ → 設定 → アカウントの種類とツール",
        ),
        Requirement(
            "ig_page", "Facebookページと連携している", UNKNOWN,
            "Instagram Graph API はページ経由でしかアカウントに到達しません。",
            "Instagramアプリ → 設定 → ページとリンク",
        ),
        Requirement(
            "ig_page_role", "そのFacebookページの管理権限を持っている", UNKNOWN,
            "権限が無いと認可画面までは進めますが、連携先の一覧に出てきません。",
            "Facebookページ → 設定 → ページの役割",
        ),
    ),
    Platform.YOUTUBE: (
        Requirement(
            "yt_channel", "チャンネルが作成済みである", UNKNOWN,
            "Googleアカウントだけではチャンネルが無く、投稿先になりません。",
            "YouTube → 設定 → チャンネルを作成",
        ),
        Requirement(
            "yt_verified", "電話番号確認が済んでいる", UNKNOWN,
            "未確認だと15分を超える動画とカスタムサムネイルが使えません。",
            "youtube.com/verify",
        ),
    ),
    Platform.TIKTOK: (
        Requirement(
            "tt_account", "投稿先アカウントにログインできる", UNKNOWN,
            "連携時にそのアカウントで認可する必要があります。",
            "",
        ),
    ),
    Platform.X: (),
}


# --- the reviews, in the order they bite -----------------------------------

LAUNCH_GATES: dict[Platform, tuple[Gate, ...]] = {
    Platform.INSTAGRAM: (
        Gate(
            "アプリ審査（App Review）",
            "自分がアプリの管理者・開発者・テスターとして登録していないアカウント全て"
            "（＝クライアントのアカウント）",
            "他社アカウントを連携できません。認可画面で権限が一つも付与されず、"
            "連携先が0件になります。自分のアカウントは開発モードのまま使えます。",
            "権限ごとの用途説明と操作録画。`snsauto setup review-pack instagram` が"
            "申請文と録画手順を生成します。",
            "2〜4週間（差し戻されると都度やり直し）",
            "https://developers.facebook.com/docs/app-review",
        ),
        Gate(
            "ビジネス認証（Business Verification）",
            "アプリ審査と同時に要求されます",
            "審査そのものが進みません。",
            "法人の登記情報、または事業実態を示す書類。",
            "数日〜2週間",
            "https://www.facebook.com/business/help/1095661473946872",
        ),
    ),
    Platform.YOUTUBE: (
        Gate(
            "API利用コンプライアンス監査",
            "API経由で動画をアップロードする全てのプロジェクト",
            "**監査を通るまで、APIでアップロードした動画は全て「非公開」に固定されます。**"
            "投稿自体は成功し、動画IDも返るため、気づかないまま非公開の動画が"
            "溜まります。",
            "監査フォームの提出（アプリの用途、想定利用者、スコープの説明）。",
            "数週間",
            "https://developers.google.com/youtube/v3/guides/auth/installed-apps",
        ),
        Gate(
            "OAuth同意画面の審査",
            "本番モードで、組織外のユーザーに使わせる場合",
            "テストユーザー登録した人以外は認可できません（上限100人）。"
            "未審査のままだと警告画面が出ます。",
            "アプリ名・ロゴ・プライバシーポリシーURL・スコープの正当性説明。",
            "数日〜数週間",
            "https://support.google.com/cloud/answer/13463073",
        ),
    ),
    Platform.TIKTOK: (
        Gate(
            "Content Posting API の監査",
            "公開投稿を行う全てのアプリ",
            "**監査前は投稿が「自分のみ表示」に固定されます。**"
            "APIは成功を返すため、公開されていないことに気づきにくい状態です。",
            "アプリの用途説明とデモ動画の提出。",
            "数週間",
            "https://developers.tiktok.com/doc/content-posting-api-get-started",
        ),
    ),
    Platform.X: (
        Gate(
            "有料ティアの契約",
            "キーワード検索と、まとまった量の投稿",
            "無料ティアでは検索APIが使えず、競合調査ができません。"
            "投稿も月あたりの上限が小さく設定されています。",
            "月額課金。上限と価格は改定されるため、契約前に必ず確認してください。",
            "即時",
            "https://developer.x.com/en/portal/products",
        ),
    ),
}


# --- what this tool calls with each grant, for the review submission --------

@dataclass
class ScopeUse:
    """One permission, as an App Review submission needs it described."""

    scope: str
    purpose_ja: str
    purpose_en: str
    endpoints: tuple[str, ...]
    screencast: tuple[str, ...] = field(default_factory=tuple)


INSTAGRAM_SCOPE_USES: tuple[ScopeUse, ...] = (
    ScopeUse(
        "instagram_basic",
        "運用代行しているアカウントのプロフィールと過去投稿を読み、"
        "投稿実績の一覧と分析の土台にします。",
        "Read the profile and existing media of the Instagram Business account "
        "the operator manages, to build the account's own performance history. "
        "This is the baseline every other feature compares against.",
        ("GET /{ig-user-id}?fields=id,username,name",
         "GET /{ig-media-id}?fields=like_count,comments_count"),
        ("ツールにログインし、対象アカウントが未連携であることを映す",
         "「アカウント連携」からInstagramを選び、Facebookの認可画面を通す",
         "連携後の画面に、アカウント名と過去投稿の一覧が表示されるところを映す"),
    ),
    ScopeUse(
        "instagram_content_publish",
        "承認済みの動画を、運用代行先のアカウントのリールとして投稿します。"
        "投稿前に残りの投稿枠をAPIに問い合わせ、上限超過を避けます。",
        "Publish approved video content as a Reel on behalf of the managed "
        "account, and check the account's remaining publishing quota before "
        "each attempt so the app never exceeds the documented limit.",
        ("GET /{ig-user-id}/content_publishing_limit",
         "POST /{ig-user-id}/media",
         "GET /{ig-container-id}?fields=status_code",
         "POST /{ig-user-id}/media_publish"),
        ("生成済みの動画のプレビュー画面を映す",
         "投稿ボタンを押し、アップロード状況の表示を映す",
         "Instagramアプリ側で、その投稿が実際に公開されたところを映す"),
    ),
    ScopeUse(
        "instagram_manage_insights",
        "投稿後の実績（リーチ・保存・シェア・平均視聴時間・スキップ率）を取得し、"
        "次の企画の判断材料にします。この権限が無いと、いいねとコメント以外の"
        "全ての指標が取得できません。",
        "Read insights for the managed account's own media - reach, saves, "
        "shares, average watch time and skip rate - to report performance to "
        "the account owner and to decide what to produce next. Without this "
        "permission no metric beyond likes and comments is available.",
        ("GET /{ig-media-id}/insights"
         "?metric=views,reach,saved,shares,ig_reels_avg_watch_time,reels_skip_rate",),
        ("投稿済みの一覧から1件を開く",
         "実績画面に、リーチ・保存・平均視聴時間・スキップ率が表示されるところを映す",
         "離脱点の分析画面まで進み、その数値が何に使われるかを映す"),
    ),
    ScopeUse(
        "pages_show_list",
        "Instagramアカウントに紐づくFacebookページを特定し、"
        "連携先アカウントの一覧を表示します。",
        "List the Facebook Pages the person administers, in order to find the "
        "Instagram Business account linked to each one. This is how the app "
        "presents the choice of which account to connect.",
        ("GET /me/accounts?fields=id,name,access_token,instagram_business_account",),
        ("認可直後に、連携できるアカウントの候補一覧が出るところを映す",),
    ),
    ScopeUse(
        "pages_read_engagement",
        "同じジャンルのハッシュタグ上位投稿と、公開されている競合アカウントを参照し、"
        "何が伸びているかを調べます。",
        "Read public hashtag top media and public Business account profiles to "
        "research what performs well in the managed account's category, which "
        "is the input to the content plan.",
        ("GET /ig_hashtag_search?user_id=...&q=...",
         "GET /{ig-hashtag-id}/top_media",
         "GET /{ig-user-id}?fields=business_discovery.username(...)"),
        ("調査画面でキーワードを入力し、上位投稿が一覧されるところを映す",
         "その結果から、構成や尺の傾向が出力されるところを映す"),
    ),
    ScopeUse(
        "business_management",
        "ビジネスアカウントとして上記の操作を行うために必要です。",
        "Required to act on the Business assets (Page and Instagram Business "
        "account) that the operator manages on behalf of their client.",
        ("上記の各エンドポイントの前提として使用",),
        (),
    ),
)

YOUTUBE_SCOPE_USES: tuple[ScopeUse, ...] = (
    ScopeUse(
        "https://www.googleapis.com/auth/youtube.upload",
        "運用代行しているチャンネルに、承認済みの短尺動画を投稿します。"
        "投稿は依頼主の承認を得たものだけで、チャンネル管理者本人がこの認可を"
        "通して接続します。",
        "Upload approved short-form video to the YouTube channel the operator "
        "manages on the owner's behalf. Every upload is content the channel "
        "owner has approved, and the channel is connected by its own manager "
        "through this authorisation flow.",
        ("POST /upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",),
        ("ツールにログインし、対象チャンネルが未連携であることを映す",
         "チャンネルを連携し、Googleの認可画面を通す",
         "生成済みの動画を投稿し、YouTube Studio 側に現れるところまで映す"),
    ),
    ScopeUse(
        "https://www.googleapis.com/auth/youtube.readonly",
        "同じジャンルで伸びている動画を調べ、投稿後は自分の動画の再生数・"
        "高評価・コメント数を読みます。企画の判断材料と、依頼主への報告に"
        "使います。",
        "Read public video data to research what performs well in the "
        "channel's category, and read the channel's own statistics - views, "
        "likes, comments - to report performance to the channel owner and to "
        "decide what to produce next.",
        ("GET /youtube/v3/search?part=id",
         "GET /youtube/v3/videos?part=snippet,statistics,contentDetails",
         "GET /youtube/v3/channels?part=snippet,statistics",
         "GET /youtube/v3/commentThreads?part=snippet"),
        ("調査画面でキーワードを入力し、上位動画が一覧されるところを映す",
         "その結果から構成や尺の傾向が出力されるところを映す"),
    ),
    ScopeUse(
        "https://www.googleapis.com/auth/yt-analytics.readonly",
        "自分の動画の視聴維持率と離脱カーブを取得し、どこで離脱されたかを"
        "特定します。この権限が無いと、再生数は取れても「どこで離れたか」が"
        "分からず、改善の判断ができません。",
        "Read the channel's own audience retention report, to identify where "
        "viewers stop watching. Without this, view counts are available but "
        "the point of drop-off is not, and that is the measurement the "
        "editing decisions are based on.",
        ("GET /v2/reports?ids=channel==MINE&metrics=audienceWatchRatio"
         "&dimensions=elapsedVideoTimeRatio",),
        ("投稿済みの動画を開き、視聴維持カーブが表示されるところを映す",
         "離脱点が絵コンテのどのカットかまで表示されるところを映す"),
    ),
)

@dataclass
class ReviewForm:
    """申請先ごとに違う、審査の名前・提出物・差し戻しの典型。

    Meta の App Review と Google の API コンプライアンス監査は別物で、
    求められる書類も落とされ方も違う。共通の雛形を使い回すと、書いてある
    とおりに準備して落ちる。
    """

    review_name: str
    subject_ja: str
    subject_en: str
    prerequisites: tuple[tuple[str, str], ...]
    rejections: tuple[tuple[str, str], ...]


REVIEW_FORMS: dict[Platform, ReviewForm] = {
    Platform.INSTAGRAM: ReviewForm(
        review_name="アプリ審査（App Review）",
        subject_ja=(
            "代理店がクライアントのInstagramアカウントを運用代行するための"
            "ツールです。ジャンルごとの傾向調査 → 台本と動画の生成 → "
            "リールとしての投稿 → 実績のレポートまでを一貫して行います。"
            "操作対象は運用委託を受けたアカウントのみで、いずれも管理者本人が"
            "この認可フローを通して接続します。"
        ),
        subject_en=(
            "This app is an SNS operations tool used by a marketing agency to "
            "manage the Instagram accounts of its clients. For each managed "
            "account it researches what performs well in that account's "
            "category, produces short-form video from that research, publishes "
            "it as a Reel with the account owner's authorisation, and reports "
            "the resulting performance back to the owner. Every account it "
            "touches is one the operator has been engaged to manage, and is "
            "connected by that account's own administrator through this "
            "permission flow."
        ),
        prerequisites=(
            ("プライバシーポリシーURL", "公開されている必要があります（必須）"),
            ("利用規約URL", "同上"),
            ("アプリアイコン", "1024x1024"),
            ("ビジネス認証", "登記情報などの書類。審査と並行して進みます"),
        ),
        rejections=(
            ("認可フローが録画に含まれていない", "ログアウト状態から録り直す"),
            ("申請した権限のうち一部しか録画に出てこない", "権限ごとに該当場面を用意する"),
            ("用途説明が抽象的（「分析のため」等）", "どの画面のどの数値になるかまで書く"),
            ("実装より広い権限を申請している", "使っていない権限は申請から外す"),
            ("ビジネス認証が未完了", "審査と並行して先に着手する"),
        ),
    ),
    Platform.YOUTUBE: ReviewForm(
        review_name="API利用コンプライアンス監査",
        subject_ja=(
            "代理店がクライアントのYouTubeチャンネルを運用代行するための"
            "ツールです。ジャンルごとの傾向調査 → 台本と動画の生成 → "
            "ショート動画としての投稿 → 視聴維持率を含む実績のレポートまでを"
            "一貫して行います。操作対象は運用委託を受けたチャンネルのみで、"
            "いずれも管理者本人がこの認可フローを通して接続します。"
            "一般公開して不特定多数に使わせるものではありません。"
        ),
        subject_en=(
            "This app is an SNS operations tool used by a marketing agency to "
            "manage the YouTube channels of its clients. For each managed "
            "channel it researches what performs well in that channel's "
            "category, produces short-form video from that research, uploads it "
            "with the channel owner's authorisation, and reports the resulting "
            "performance - including audience retention - back to the owner. "
            "It is not a general-purpose consumer product: every channel it "
            "touches is one the operator has been engaged to manage, and is "
            "connected by that channel's own manager through this OAuth flow."
        ),
        prerequisites=(
            ("プライバシーポリシーURL", "公開されている必要があります（必須）"),
            ("利用規約URL", "同上"),
            ("OAuth同意画面", "アプリ名・ロゴ・スコープの正当性説明まで入力済みであること"),
            ("APIクォータ", "search.list は1回100ユニット（既定の上限は1日10,000）。"
                            "申請時に想定利用量を聞かれます"),
            ("監査フォーム", "Data API と Analytics API で別々に提出します"),
        ),
        rejections=(
            ("監査前に本番運用を始めてしまっている",
             "監査が通るまでAPIからの投稿は非公開に固定されます。先に監査を出す"),
            ("スコープが用途より広い", "使っていないスコープは要求から外す"),
            ("用途説明が一般消費者向けアプリのように読める",
             "運用代行であること、接続するのは委託先本人であることを明記する"),
            ("録画に認可画面が含まれていない", "ログアウト状態から録り直す"),
            ("クォータの想定が実装と合っていない",
             "1日の調査回数から逆算した数字を出す"),
        ),
    ),
}


SCOPE_USES: dict[Platform, tuple[ScopeUse, ...]] = {
    Platform.INSTAGRAM: INSTAGRAM_SCOPE_USES,
    Platform.YOUTUBE: YOUTUBE_SCOPE_USES,
}


# --- local checks ----------------------------------------------------------


def _app_credentials(platform: Platform, settings) -> Requirement:
    setup = APP_SETUP[platform]
    missing = [
        name for name in setup.env_vars
        if not getattr(settings, name.lower().replace("snsauto_", ""), None)
    ]
    if missing:
        return Requirement(
            "app", f"{setup.console} でアプリを作る", TODO,
            f"未設定: {', '.join(missing)}",
            f"{setup.console_url} で作成し .env に設定" + (f"。{setup.note}" if setup.note else ""),
        )
    return Requirement("app", f"{setup.console} でアプリを作る", DONE,
                       f"設定済み: {', '.join(setup.env_vars)}")


def _redirect_uri(platform: Platform, settings) -> Requirement:
    from .platforms.oauth import OAuthError, get_provider

    try:
        uri = get_provider(platform, settings).redirect_uri()
    except OAuthError as exc:
        return Requirement(
            "redirect", "戻り先URLをアプリに登録する", TODO, str(exc),
            "SNSAUTO_PUBLIC_BASE_URL を設定すると、登録すべきURLが表示されます",
        )
    return Requirement(
        "redirect", "戻り先URLをアプリに登録する", UNKNOWN,
        f"このURLを一字一句そのまま登録してください: {uri}",
        "登録漏れは認可画面で redirect_uri mismatch になります",
    )


def _connection(platform: Platform, session) -> tuple[Requirement, object | None]:
    from sqlalchemy import select

    from .models import SocialAccount

    if session is None:
        return Requirement("connect", "アカウントを連携する", UNKNOWN,
                           "データベースを参照していません"), None
    account = session.scalars(
        select(SocialAccount)
        .where(SocialAccount.platform == platform, SocialAccount.is_active.is_(True))
        .order_by(SocialAccount.id)
    ).first()
    if account is None:
        return Requirement(
            "connect", "アカウントを連携する", TODO,
            "未連携です", "`snsauto serve` → /accounts から連携",
        ), None
    label = account.display_name or account.username or account.external_id
    return Requirement("connect", "アカウントを連携する", DONE, f"連携済み: {label}"), account


def _scopes(platform: Platform, account) -> Requirement:
    from .platforms.oauth import missing_scopes

    if account is None:
        return Requirement("scopes", "必要な権限が揃っている", UNKNOWN,
                           "連携後に確認できます")
    granted = list(getattr(account, "scopes", None) or [])
    if not granted:
        return Requirement(
            "scopes", "必要な権限が揃っている", UNKNOWN,
            "権限の記録が無いアカウントです（記録前に連携されたもの）",
            "/accounts から再連携すると検証できます",
        )
    lacking = missing_scopes(platform, granted)
    if lacking:
        return Requirement(
            "scopes", "必要な権限が揃っている", TODO,
            "不足: " + ", ".join(lacking),
            "再連携が必要です。これが取得できません: " + " / ".join(lacking.values()),
        )
    return Requirement("scopes", "必要な権限が揃っている", DONE,
                       f"{len(granted)}件すべて付与済み")


def connect_plan(
    platform: Platform, settings, session=None
) -> tuple[list[Requirement], tuple[Gate, ...]]:
    """The ordered path to a usable connection, and the reviews after it.

    Dependency order, not importance: registering a redirect URL before the
    app exists is not a step, it is a dead end.
    """
    steps = [_app_credentials(platform, settings), _redirect_uri(platform, settings)]
    steps.extend(ACCOUNT_PREREQUISITES.get(platform, ()))
    connection, account = _connection(platform, session)
    steps.append(connection)
    steps.append(_scopes(platform, account))
    steps.append(Requirement(
        "verify", "読み取り専用で疎通確認する",
        DONE if connection.state == DONE else UNKNOWN,
        "`snsauto verify -p " + platform.value + "`",
        "投稿は行いません。どの指標が実際に返るかまで表示します",
    ))
    return steps, LAUNCH_GATES.get(platform, ())


# --- App Review submission material ----------------------------------------


def review_pack(platform: Platform, settings=None) -> str:
    """The App Review submission, assembled from what the code actually calls.

    Written out rather than summarised on screen because the endpoint list and
    the per-permission justification are pasted into a web form one field at a
    time, and the screencast has to be recorded against a script.
    """
    uses = SCOPE_USES.get(platform)
    if not uses:
        supported = "、".join(sorted(p.value for p in SCOPE_USES))
        raise ValueError(
            f"{platform.value} の申請パックはまだありません。"
            f"現在の対応は {supported} です。"
        )

    setup = APP_SETUP[platform]
    form = REVIEW_FORMS[platform]
    lines = [
        f"# {platform.value.upper()} {form.review_name} 申請パック",
        "",
        "このファイルは申請フォームに貼り付けるための下書きです。",
        "英文は審査担当者が読む欄に、日本語は社内確認用に使ってください。",
        "",
        "> 用途説明は、このツールが実際に呼んでいるエンドポイントから起こしています。",
        "> 申請内容と実装が食い違うことが差し戻しの最大の原因なので、",
        "> 機能を足したときはここも更新してください。",
        "",
        "## 0. 申請前に揃えるもの",
        "",
        "| 項目 | 内容 |",
        "|---|---|",
        f"| 開発者コンソール | {setup.console} — {setup.console_url} |",
    ]
    lines += [f"| {name} | {detail} |" for name, detail in form.prerequisites]
    lines += [
        "",
        "## 1. アプリの用途（Overview）",
        "",
        "**English (申請欄にそのまま貼れます)**",
        "",
        f"> {form.subject_en}",
        "",
        "**日本語**",
        "",
        f"> {form.subject_ja}",
        "",
        "## 2. 権限ごとの用途と操作録画",
        "",
    ]

    for index, use in enumerate(uses, start=1):
        lines += [
            f"### 2.{index} `{use.scope}`",
            "",
            "**用途（English / 申請欄）**",
            "",
            f"> {use.purpose_en}",
            "",
            "**用途（日本語）**",
            "",
            f"> {use.purpose_ja}",
            "",
            "**この権限で呼ぶエンドポイント**",
            "",
        ]
        lines += [f"- `{endpoint}`" for endpoint in use.endpoints]
        if use.screencast:
            lines += ["", "**録画する操作**", ""]
            lines += [f"{step_no}. {step}" for step_no, step in enumerate(use.screencast, 1)]
        lines.append("")

    lines += [
        "## 3. 録画全体の注意",
        "",
        "1本の動画に全権限をまとめて構いませんが、次を必ず含めてください。",
        "",
        "- **ログアウト状態から始める。** 認可画面を通過する場面が映っていない録画は"
        "差し戻されます。",
        "- **権限の同意画面を省略しない。** 早送りやカットをせず、"
        "どの権限に同意したかが読める速度で映してください。",
        "- **取得したデータが画面上で何に使われるかまで映す。** "
        "APIを呼んだ証拠ではなく、ユーザーにとっての用途が審査対象です。",
        "- **テスト用ではなく実際のアカウントで操作する。**",
        "",
        "## 4. よくある差し戻しの理由",
        "",
        "| 理由 | 対処 |",
        "|---|---|",
    ]
    lines += [f"| {reason} | {fix} |" for reason, fix in form.rejections]
    lines.append("")

    gates = LAUNCH_GATES.get(platform, ())
    if gates:
        lines += ["## 5. 審査中も動くもの / 止まるもの", ""]
        for gate in gates:
            lines += [
                f"### {gate.name}",
                "",
                f"- 対象: {gate.applies_to}",
                f"- 止まること: {gate.blocks}",
                f"- 必要なもの: {gate.cost}",
                f"- 目安期間: {gate.typical_wait}",
                f"- 一次情報: {gate.source}",
                "",
            ]

    return "\n".join(lines)
