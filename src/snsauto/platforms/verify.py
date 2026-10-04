"""A read-only live check of every platform connection.

The adapters in this package have never spoken to a real API: there are no app
credentials here, so every request they build is verified against the
documented contract (tests/test_api_contract.py) and no further. The first
person to add real credentials is therefore the first person to find out
whether any of it works, and the worst way to find out is halfway through a
scheduled publish.

This module is the good way. It exercises each capability with the smallest
harmless call that proves it, reports exactly which field came back empty, and
says what to change when something fails. It **never writes**: no publish, no
edit, no delete. Running it against a production account is safe.

What each check proves:

* ``credentials`` - the adapter found a key or token at all.
* ``search``      - the endpoint answered and the response parsed into records.
* ``account``     - we can read the connected account's own profile.
* ``insights``    - metrics come back for a post we published, and *which*
                    fields are actually populated, because the difference
                    between "retention supported" and "retention returns null
                    for this account" is invisible until you look.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..models import Platform, Publication, PublicationStatus
from .base import Capability, CapabilityUnavailable, CredentialsMissing, PlatformError

log = logging.getLogger(__name__)

# A query that returns results on every platform without being a real
# marketing term, so a verification run cannot be mistaken for research.
PROBE_KEYWORD = "news"

# Fields worth reporting individually: an adapter that returns a MetricRecord
# with every retention field None is "working" and useless, and only naming
# them separately makes that visible.
METRIC_FIELDS = (
    "views", "likes", "comments", "shares", "saves",
    "avg_watch_sec", "retention_rate", "skip_rate", "reach",
)

OK, FAIL, SKIP = "ok", "fail", "skip"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class PlatformReport:
    platform: str
    checks: list[Check] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return any(c.status == OK for c in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]


def _describe(exc: Exception) -> tuple[str, str]:
    """The error, and what to do about it."""
    text = str(exc)
    if isinstance(exc, CredentialsMissing):
        return text, "接続情報がありません。/accounts で連携するか .env を設定してください。"
    if isinstance(exc, CapabilityUnavailable):
        return text, "このプラットフォームのAPIでは提供されていません。"
    lowered = text.lower()
    if "401" in text or "unauthorized" in lowered or "invalid_grant" in lowered:
        return text, "トークンが無効か期限切れです。/accounts で再連携してください。"
    if "403" in text or "forbidden" in lowered:
        return text, "アプリに必要な権限（スコープ）がありません。審査状況とスコープ設定を確認してください。"
    if "429" in text or "rate" in lowered:
        return text, "レート制限に当たりました。時間をおいて再実行してください。"
    if "404" in text or "not found" in lowered:
        return text, "対象が見つかりません。IDとアカウントの対応を確認してください。"
    if "quota" in lowered:
        return text, "APIの割り当てを使い切っています。翌日以降に再実行してください。"
    return text, "エラー本文を確認してください。"


class ConnectionVerifier:
    """Runs the read-only checks. Nothing here publishes."""

    def __init__(self, session, settings=None):
        self.session = session
        self.settings = settings

    def _adapter(self, platform: Platform, project_id=None, account_id=None):
        from . import adapter_for_account

        return adapter_for_account(
            platform, self.session, self.settings, project_id, account_id
        )

    def verify(
        self,
        platform: Platform,
        project_id: int | None = None,
        account_id: int | None = None,
        keyword: str = PROBE_KEYWORD,
    ) -> PlatformReport:
        report = PlatformReport(platform=platform.value)
        try:
            adapter, credentials = self._adapter(platform, project_id, account_id)
        except Exception as exc:
            detail, fix = _describe(exc)
            report.checks.append(Check("credentials", FAIL, detail, fix))
            return report

        caps = adapter.capabilities()
        if not caps:
            report.checks.append(Check(
                "credentials", FAIL, "利用可能な機能がありません",
                "APIキーまたはアクセストークンが未設定です。"
                "`snsauto doctor` で何が足りないか確認できます。",
            ))
            return report

        report.checks.append(Check(
            "credentials", OK,
            f"利用可能: {', '.join(sorted(c.value for c in caps))}"
            + (f" / 連携アカウント経由 ({credentials.source})" if credentials else " / 環境変数"),
        ))

        report.checks.append(self._check_scopes(platform, credentials))
        report.checks.append(self._check_search(adapter, caps, keyword))
        report.checks.append(self._check_account(adapter, caps, credentials))
        report.checks.append(self._check_insights(adapter, caps, platform, project_id))
        extra = self._check_quota(adapter, caps)
        if extra:
            report.checks.append(extra)
        return report

    # ---------- individual checks ----------

    def _check_search(self, adapter, caps, keyword: str) -> Check:
        if Capability.SEARCH not in caps:
            return Check("search", SKIP, "このプラットフォームでは検索できません")
        try:
            records = adapter.search(keyword, limit=3)
        except (PlatformError, Exception) as exc:
            detail, fix = _describe(exc)
            return Check("search", FAIL, detail, fix)

        if not records:
            return Check(
                "search", FAIL, "0件が返りました",
                "認証は通っていますが結果が空です。検索語を変えて再実行してください。",
            )
        sample = records[0]
        # Which fields the platform actually populated. A record that parses
        # but carries no views is a different problem from a failed call.
        populated = [
            name for name in ("title", "caption", "author", "published_at",
                              "duration_sec", "views", "likes", "comments", "shares")
            if getattr(sample, name, None)
        ]
        empty = [
            name for name in ("published_at", "duration_sec", "views", "shares")
            if not getattr(sample, name, None)
        ]
        return Check(
            "search", OK, f"{len(records)}件取得",
            fix="" if not empty else f"未取得の項目: {', '.join(empty)}（このAPIの仕様です）",
            data={"populated": populated, "empty": empty,
                  "sample_id": sample.external_id},
        )

    def _check_account(self, adapter, caps, credentials) -> Check:
        handle = getattr(credentials, "display_name", None) or getattr(
            credentials, "external_id", None
        )
        if not hasattr(adapter, "fetch_account") or Capability.SEARCH not in caps:
            return Check("account", SKIP, "アカウント参照に対応していません")
        if not handle:
            return Check("account", SKIP, "連携アカウントがありません")
        try:
            profile, posts = adapter.fetch_account(str(handle), limit=3)
        except (PlatformError, Exception) as exc:
            detail, fix = _describe(exc)
            return Check("account", FAIL, detail, fix)
        return Check(
            "account", OK,
            f"{profile.name or profile.handle}"
            + (f" / フォロワー {profile.followers:,}" if profile.followers else "")
            + f" / 直近{len(posts)}件",
            data={"followers": profile.followers},
        )

    def _check_scopes(self, platform: Platform, credentials) -> Check:
        """Whether the stored token carries every grant the adapter needs.

        This runs before anything is posted, which is the point: the insights
        check below needs a published post to have something to read, so an
        account connected today would not find out it is missing a permission
        until after its first post - and then only as an empty metric.
        """
        from ..models import SocialAccount
        from .oauth import REQUIRED_SCOPES, missing_scopes

        if not REQUIRED_SCOPES.get(platform):
            return Check("scopes", SKIP, "このプラットフォームは権限を個別に要求しません")
        account_id = getattr(credentials, "account_id", None)
        if not account_id:
            return Check(
                "scopes", SKIP, "環境変数のトークンのため権限を確認できません",
                "/accounts から連携すると、付与された権限まで検証できます。",
            )
        account = self.session.get(SocialAccount, account_id)
        granted = list(getattr(account, "scopes", None) or [])
        if not granted:
            return Check(
                "scopes", SKIP, "権限の記録がありません",
                "権限を記録する前に連携されたアカウントです。"
                "/accounts から再連携すると検証できるようになります。",
            )

        lacking = missing_scopes(platform, granted)
        if lacking:
            return Check(
                "scopes", FAIL,
                "不足している権限: " + ", ".join(lacking),
                "/accounts から再連携してください。これが無いと次が取得できません: "
                + " / ".join(lacking.values()),
                data={"granted": granted, "missing": sorted(lacking)},
            )
        return Check(
            "scopes", OK, f"必要な権限は揃っています（{len(granted)}件）",
            data={"granted": granted, "missing": []},
        )

    def _check_insights(self, adapter, caps, platform, project_id) -> Check:
        if Capability.INSIGHTS not in caps:
            return Check("insights", SKIP, "実績取得に対応していません")

        from sqlalchemy import select

        stmt = select(Publication).where(
            Publication.platform == platform,
            Publication.status == PublicationStatus.PUBLISHED,
            Publication.external_id.is_not(None),
        )
        if project_id:
            stmt = stmt.where(Publication.project_id == project_id)
        publication = self.session.scalars(stmt.order_by(Publication.id.desc())).first()

        if publication is None:
            return Check(
                "insights", SKIP, "検証に使える自社の公開済み投稿がありません",
                "1本投稿してから再実行すると、どの指標が実際に返るか確認できます。",
            )
        try:
            record = adapter.fetch_metrics(publication.external_id)
        except (PlatformError, Exception) as exc:
            detail, fix = _describe(exc)
            return Check("insights", FAIL, detail, fix)

        present = [f for f in METRIC_FIELDS if getattr(record, f, None)]
        missing = [f for f in METRIC_FIELDS if not getattr(record, f, None)]
        fix = ""
        if not record.has_retention():
            fix = (
                "視聴維持率が返っていません。"
                + ("YouTube は yt-analytics.readonly スコープが必要です（再連携してください）。"
                   if platform is Platform.YOUTUBE else
                   "Instagram は Reels かつ投稿から一定時間が必要です。"
                   if platform is Platform.INSTAGRAM else
                   "このプラットフォームのAPIでは提供されていません。")
            )
        return Check(
            "insights", OK, f"取得できた指標: {', '.join(present) or 'なし'}",
            fix=fix, data={"present": present, "missing": missing,
                           "publication_id": publication.id},
        )

    def _check_quota(self, adapter, caps) -> Check | None:
        """Only Instagram reports its own remaining quota."""
        if not hasattr(adapter, "publishing_limit") or Capability.PUBLISH not in caps:
            return None
        try:
            limit = adapter.publishing_limit()
        except (PlatformError, Exception) as exc:
            detail, fix = _describe(exc)
            return Check("quota", FAIL, detail, fix)
        return Check(
            "quota", OK,
            f"{limit['window_hours']:.0f}時間あたり {limit['used']}"
            + (f" / {limit['cap']}" if limit.get("cap") else "")
            + (f"（残り {limit['remaining']}）" if limit.get("remaining") is not None else ""),
            data=limit,
        )

    def verify_all(
        self, project_id: int | None = None, keyword: str = PROBE_KEYWORD
    ) -> list[PlatformReport]:
        return [self.verify(p, project_id, keyword=keyword) for p in Platform]
