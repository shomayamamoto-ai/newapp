"""OAuth connect flows.

Automated posting is allowed by every one of these platforms *through their own
API* - what makes it compliant is using the official endpoints with a token the
account owner granted. That grant is what this module obtains.

Three of the four are OAuth 2.0 with refresh tokens and differing lifetimes:

    YouTube    refresh token does not expire; access token ~1h
    TikTok     access token 24h, refresh token 365 days
    Instagram  long-lived token 60 days, refreshed in place (no refresh token)

X is OAuth 1.0a three-legged; its access token does not expire, so it is
obtained once and never refreshed.

Everything here deals only in authorization - posting lives in the adapters.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode

import httpx

from ..models import Platform, SocialAccount, utcnow
from ..utils.oauth1 import sign

log = logging.getLogger(__name__)

# --- YouTube (Google) ---
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # Retention (averageViewPercentage / averageViewDuration) comes from the
    # Analytics API, which the two scopes above do not reach. An account
    # connected before this scope existed keeps working and simply reports no
    # retention until it is reconnected.
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# --- TikTok ---
TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_SCOPES = ["user.info.basic", "video.publish", "video.list"]

# --- Instagram (via Facebook Login, which is what the publishing API needs) ---
FACEBOOK_VERSION = "v21.0"
FACEBOOK_AUTH = f"https://www.facebook.com/{FACEBOOK_VERSION}/dialog/oauth"
FACEBOOK_TOKEN = f"https://graph.facebook.com/{FACEBOOK_VERSION}/oauth/access_token"
FACEBOOK_GRAPH = f"https://graph.facebook.com/{FACEBOOK_VERSION}"
INSTAGRAM_SCOPES = [
    "instagram_basic",
    "instagram_content_publish",
    # Every number that makes a post worth analysing - reach, saves, shares,
    # average watch time, skip rate - comes from the insights edge, and the
    # insights edge is gated behind this one permission. Without it Instagram
    # still publishes and still returns likes and comments, so the connection
    # looks healthy while every retention figure comes back empty.
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
    "business_management",
]


# What each grant buys, for the verification report. An account connected
# before a scope was added keeps working and silently loses whatever that
# scope unlocked, so the check has to compare against this rather than assume
# the stored token is current.
REQUIRED_SCOPES: dict[Platform, dict[str, str]] = {
    Platform.YOUTUBE: {
        "https://www.googleapis.com/auth/youtube.upload": "動画の投稿",
        "https://www.googleapis.com/auth/youtube.readonly": "再生数・高評価などの取得",
        "https://www.googleapis.com/auth/yt-analytics.readonly":
            "視聴維持率と離脱カーブの取得",
    },
    Platform.TIKTOK: {
        "user.info.basic": "アカウント情報の取得",
        "video.publish": "動画の投稿",
        "video.list": "投稿した動画の実績取得",
    },
    Platform.INSTAGRAM: {
        "instagram_basic": "アカウントと投稿の参照",
        "instagram_content_publish": "リールの投稿",
        "instagram_manage_insights":
            "リーチ・保存・シェア・平均視聴時間・スキップ率の取得",
        "pages_show_list": "連携先Facebookページの特定",
        "pages_read_engagement": "ハッシュタグ検索と競合アカウントの参照",
        "business_management": "ビジネスアカウントとしての操作",
    },
}


def missing_scopes(platform: Platform, granted) -> dict[str, str]:
    """Required grants the token does not carry, with what each one unlocks.

    An empty ``granted`` means the account predates scope recording rather than
    that it holds nothing, so it is reported as unknown (empty) instead of as
    every scope missing.
    """
    required = REQUIRED_SCOPES.get(platform, {})
    if not granted:
        return {}
    held = set(granted)
    return {scope: why for scope, why in required.items() if scope not in held}

# --- X (OAuth 1.0a) ---
X_REQUEST_TOKEN = "https://api.x.com/oauth/request_token"
X_AUTHORIZE = "https://api.x.com/oauth/authorize"
X_ACCESS_TOKEN = "https://api.x.com/oauth/access_token"


class OAuthError(RuntimeError):
    pass


class OAuthNotConfigured(OAuthError):
    """The app's own client id/secret for this platform is missing."""


@dataclass(slots=True)
class AuthStart:
    url: str
    state: str
    # OAuth 1.0a hands back a secret at request-token time that the callback
    # needs; OAuth 2.0 leaves this empty.
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class ConnectedAccount:
    platform: Platform
    external_id: str
    access_token: str
    display_name: str | None = None
    username: str | None = None
    refresh_token: str | None = None
    token_secret: str | None = None
    expires_in: int | None = None
    refresh_expires_in: int | None = None
    scopes: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def apply_to(self, account: SocialAccount) -> SocialAccount:
        now = utcnow()
        account.platform = self.platform
        account.external_id = self.external_id
        account.display_name = self.display_name
        account.username = self.username
        account.access_token = self.access_token
        account.refresh_token = self.refresh_token
        account.token_secret = self.token_secret
        account.scopes = self.scopes
        account.expires_at = (
            now + timedelta(seconds=self.expires_in) if self.expires_in else None
        )
        account.refresh_expires_at = (
            now + timedelta(seconds=self.refresh_expires_in)
            if self.refresh_expires_in else None
        )
        account.last_refreshed_at = now
        account.refresh_error = None
        account.is_active = True
        account.meta = {**(account.meta or {}), **self.meta}
        return account


class OAuthProvider:
    """Base: subclasses implement start, finish and (where possible) refresh."""

    platform: Platform
    supports_refresh = True

    def __init__(self, settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client or httpx.Client(timeout=30.0)

    def redirect_uri(self) -> str:
        base = (self.settings.public_base_url or "").rstrip("/")
        if not base:
            raise OAuthNotConfigured(
                "SNSAUTO_PUBLIC_BASE_URL を設定してください。承認後にブラウザが"
                "戻ってくる先として、各社に登録する必要があります。"
            )
        return f"{base}/connect/{self.platform.value}/callback"

    def start(self) -> AuthStart:
        raise NotImplementedError

    def finish(self, params: dict, pending: dict) -> ConnectedAccount:
        raise NotImplementedError

    def refresh(self, account: SocialAccount) -> ConnectedAccount:
        raise OAuthError(f"{self.platform.value} tokens cannot be refreshed")

    def _post_form(self, url: str, data: dict, headers: dict | None = None) -> dict:
        response = self._client.post(url, data=data, headers=headers or {})
        if response.status_code >= 400:
            raise OAuthError(f"{url} returned {response.status_code}: {response.text[:400]}")
        return response.json()


class YouTubeOAuth(OAuthProvider):
    platform = Platform.YOUTUBE

    def _credentials(self) -> tuple[str, str]:
        if not (self.settings.youtube_client_id and self.settings.youtube_client_secret):
            raise OAuthNotConfigured(
                "YouTube を接続するには YOUTUBE_CLIENT_ID と YOUTUBE_CLIENT_SECRET "
                "が必要です（Google Cloud で OAuth クライアントを作成してください）。"
            )
        return self.settings.youtube_client_id, self.settings.youtube_client_secret

    def start(self) -> AuthStart:
        client_id, _ = self._credentials()
        state = secrets.token_urlsafe(24)
        query = {
            "client_id": client_id,
            "redirect_uri": self.redirect_uri(),
            "response_type": "code",
            "scope": " ".join(YOUTUBE_SCOPES),
            # Without both of these Google returns no refresh token on
            # re-authorisation, and the connection silently dies in an hour.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return AuthStart(url=f"{GOOGLE_AUTH}?{urlencode(query)}", state=state)

    def finish(self, params: dict, pending: dict) -> ConnectedAccount:
        client_id, client_secret = self._credentials()
        token = self._post_form(GOOGLE_TOKEN, {
            "code": params.get("code", ""),
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": self.redirect_uri(),
            "grant_type": "authorization_code",
        })
        access = token.get("access_token")
        if not access:
            raise OAuthError(f"no access token in Google response: {list(token)}")

        channel = self._channel(access)
        return ConnectedAccount(
            platform=self.platform,
            external_id=channel.get("id") or "me",
            display_name=channel.get("title"),
            username=channel.get("customUrl"),
            access_token=access,
            refresh_token=token.get("refresh_token"),
            expires_in=token.get("expires_in"),
            scopes=(token.get("scope") or "").split(),
            meta={"channel": channel},
        )

    def _channel(self, access_token: str) -> dict:
        response = self._client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code >= 400:
            return {}
        items = response.json().get("items") or []
        return {"id": items[0]["id"], **items[0].get("snippet", {})} if items else {}

    def refresh(self, account: SocialAccount) -> ConnectedAccount:
        client_id, client_secret = self._credentials()
        if not account.refresh_token:
            raise OAuthError("no refresh token stored; reconnect the account")
        token = self._post_form(GOOGLE_TOKEN, {
            "refresh_token": account.refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        })
        return ConnectedAccount(
            platform=self.platform,
            external_id=account.external_id,
            display_name=account.display_name,
            username=account.username,
            access_token=token["access_token"],
            # Google does not return the refresh token again; keep the stored one.
            refresh_token=token.get("refresh_token") or account.refresh_token,
            expires_in=token.get("expires_in"),
            scopes=account.scopes,
            meta=account.meta or {},
        )


class TikTokOAuth(OAuthProvider):
    platform = Platform.TIKTOK

    def _credentials(self) -> tuple[str, str]:
        if not (self.settings.tiktok_client_key and self.settings.tiktok_client_secret):
            raise OAuthNotConfigured(
                "TikTok を接続するには TIKTOK_CLIENT_KEY と TIKTOK_CLIENT_SECRET "
                "が必要です（TikTok for Developers でアプリを作成してください）。"
            )
        return self.settings.tiktok_client_key, self.settings.tiktok_client_secret

    def start(self) -> AuthStart:
        client_key, _ = self._credentials()
        state = secrets.token_urlsafe(24)
        query = {
            "client_key": client_key,
            "response_type": "code",
            "scope": ",".join(TIKTOK_SCOPES),
            "redirect_uri": self.redirect_uri(),
            "state": state,
        }
        return AuthStart(url=f"{TIKTOK_AUTH}?{urlencode(query)}", state=state)

    def finish(self, params: dict, pending: dict) -> ConnectedAccount:
        client_key, client_secret = self._credentials()
        token = self._post_form(
            TIKTOK_TOKEN,
            {
                "client_key": client_key,
                "client_secret": client_secret,
                "code": params.get("code", ""),
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token.get("error"):
            raise OAuthError(f"TikTok: {token.get('error_description') or token['error']}")

        access = token.get("access_token")
        if not access:
            raise OAuthError(f"no access token in TikTok response: {list(token)}")

        profile = self._profile(access)
        return ConnectedAccount(
            platform=self.platform,
            external_id=token.get("open_id") or profile.get("open_id") or "me",
            display_name=profile.get("display_name"),
            username=profile.get("username"),
            access_token=access,
            refresh_token=token.get("refresh_token"),
            expires_in=token.get("expires_in"),
            refresh_expires_in=token.get("refresh_expires_in"),
            scopes=(token.get("scope") or "").split(","),
            meta={"profile": profile},
        )

    def _profile(self, access_token: str) -> dict:
        response = self._client.get(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "open_id,display_name,username,avatar_url"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code >= 400:
            return {}
        return (response.json().get("data") or {}).get("user") or {}

    def refresh(self, account: SocialAccount) -> ConnectedAccount:
        client_key, client_secret = self._credentials()
        if not account.refresh_token:
            raise OAuthError("no refresh token stored; reconnect the account")
        token = self._post_form(
            TIKTOK_TOKEN,
            {
                "client_key": client_key,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": account.refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token.get("error"):
            raise OAuthError(f"TikTok refresh: {token.get('error_description')}")
        return ConnectedAccount(
            platform=self.platform,
            external_id=account.external_id,
            display_name=account.display_name,
            username=account.username,
            access_token=token["access_token"],
            refresh_token=token.get("refresh_token") or account.refresh_token,
            expires_in=token.get("expires_in"),
            refresh_expires_in=token.get("refresh_expires_in"),
            scopes=account.scopes,
            meta=account.meta or {},
        )


class InstagramOAuth(OAuthProvider):
    """Instagram publishing runs through Facebook Login and a Page token.

    The chain is: user token -> long-lived user token -> the Page the creator
    manages -> the Instagram Business account attached to that Page. All four
    steps are required before a single post can be made, which is why
    connecting Instagram fails more often than the others - and why the errors
    here name the step that failed.
    """

    platform = Platform.INSTAGRAM

    def _credentials(self) -> tuple[str, str]:
        if not (self.settings.facebook_app_id and self.settings.facebook_app_secret):
            raise OAuthNotConfigured(
                "Instagram を接続するには FACEBOOK_APP_ID と FACEBOOK_APP_SECRET "
                "が必要です。投稿APIは Facebook アプリ経由でしか使えず、Instagram は"
                "ビジネスアカウントで Facebook ページに接続されている必要があります。"
            )
        return self.settings.facebook_app_id, self.settings.facebook_app_secret

    def start(self) -> AuthStart:
        app_id, _ = self._credentials()
        state = secrets.token_urlsafe(24)
        query = {
            "client_id": app_id,
            "redirect_uri": self.redirect_uri(),
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
            "state": state,
        }
        return AuthStart(url=f"{FACEBOOK_AUTH}?{urlencode(query)}", state=state)

    def finish(self, params: dict, pending: dict) -> ConnectedAccount:
        app_id, app_secret = self._credentials()

        short = self._get(FACEBOOK_TOKEN, {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": self.redirect_uri(),
            "code": params.get("code", ""),
        })
        if not short.get("access_token"):
            raise OAuthError(f"Facebook code exchange failed: {short}")

        long_lived = self._get(FACEBOOK_TOKEN, {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short["access_token"],
        })
        user_token = long_lived.get("access_token") or short["access_token"]

        pages = self._get(f"{FACEBOOK_GRAPH}/me/accounts", {
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account{id,username,name,profile_picture_url}",
        }).get("data") or []

        for page in pages:
            ig = page.get("instagram_business_account")
            if not ig:
                continue
            return ConnectedAccount(
                platform=self.platform,
                external_id=ig["id"],
                display_name=ig.get("name") or page.get("name"),
                username=ig.get("username"),
                # A Page token derived from a long-lived user token is itself
                # long-lived; this is the token the publishing API wants.
                access_token=page.get("access_token") or user_token,
                expires_in=long_lived.get("expires_in"),
                scopes=INSTAGRAM_SCOPES,
                meta={
                    "page_id": page.get("id"),
                    "page_name": page.get("name"),
                    "avatar_url": ig.get("profile_picture_url"),
                    "user_token": user_token,
                },
            )

        if not pages:
            raise OAuthError(
                "この Facebook アカウントで管理しているページが見つかりません。"
                "Instagram をビジネスアカウントにして Facebook ページに接続してください。"
            )
        raise OAuthError(
            "Facebook ページは見つかりましたが、Instagram ビジネスアカウントが "
            "接続されていません。ページ設定から接続してください。"
        )

    def refresh(self, account: SocialAccount) -> ConnectedAccount:
        """Extend the long-lived token in place.

        Instagram has no refresh token: the current token is exchanged for a
        fresh 60-day one, so this must run before it expires or the account
        has to be reconnected by hand.
        """
        app_id, app_secret = self._credentials()
        meta = account.meta or {}
        current = meta.get("user_token") or account.access_token

        extended = self._get(FACEBOOK_TOKEN, {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": current,
        })
        user_token = extended.get("access_token")
        if not user_token:
            raise OAuthError(f"Instagram token refresh failed: {extended}")

        page_token = account.access_token
        page_id = meta.get("page_id")
        if page_id:
            page = self._get(f"{FACEBOOK_GRAPH}/{page_id}", {
                "fields": "access_token", "access_token": user_token,
            })
            page_token = page.get("access_token") or page_token

        return ConnectedAccount(
            platform=self.platform,
            external_id=account.external_id,
            display_name=account.display_name,
            username=account.username,
            access_token=page_token,
            expires_in=extended.get("expires_in") or 60 * 24 * 3600,
            scopes=account.scopes,
            meta={**meta, "user_token": user_token},
        )

    def _get(self, url: str, params: dict) -> dict:
        response = self._client.get(url, params=params)
        try:
            body = response.json()
        except ValueError:
            raise OAuthError(f"{url} returned non-JSON: {response.text[:300]}") from None
        if response.status_code >= 400:
            error = body.get("error", {})
            raise OAuthError(
                f"Facebook API {response.status_code}: "
                f"{error.get('message') or body}"
            )
        return body


class XOAuth(OAuthProvider):
    """X uses OAuth 1.0a three-legged. The resulting token does not expire."""

    platform = Platform.X
    supports_refresh = False

    def _credentials(self) -> tuple[str, str]:
        if not (self.settings.x_api_key and self.settings.x_api_secret):
            raise OAuthNotConfigured(
                "X を接続するには X_API_KEY と X_API_SECRET が必要です"
                "（X Developer Portal でアプリを作成してください）。"
            )
        return self.settings.x_api_key, self.settings.x_api_secret

    def start(self) -> AuthStart:
        key, secret = self._credentials()
        callback = self.redirect_uri()
        header = sign(
            "POST", X_REQUEST_TOKEN,
            consumer_key=key, consumer_secret=secret,
            token="", token_secret="",
            params={"oauth_callback": callback},
        )
        # oauth_callback travels in the header, not the body, at this step.
        header = header.replace("OAuth ", f'OAuth oauth_callback="{_q(callback)}", ', 1)
        response = self._client.post(X_REQUEST_TOKEN, headers={"Authorization": header})
        if response.status_code >= 400:
            raise OAuthError(f"X request_token failed: {response.text[:300]}")

        payload = dict(parse_qsl(response.text))
        token = payload.get("oauth_token")
        if not token:
            raise OAuthError(f"X returned no request token: {response.text[:200]}")
        return AuthStart(
            url=f"{X_AUTHORIZE}?{urlencode({'oauth_token': token})}",
            state=token,
            extra={"request_token_secret": payload.get("oauth_token_secret", "")},
        )

    def finish(self, params: dict, pending: dict) -> ConnectedAccount:
        key, secret = self._credentials()
        verifier = params.get("oauth_verifier")
        request_token = params.get("oauth_token") or pending.get("state")
        if not verifier or not request_token:
            raise OAuthError("X callback is missing oauth_verifier")

        header = sign(
            "POST", X_ACCESS_TOKEN,
            consumer_key=key, consumer_secret=secret,
            token=request_token,
            token_secret=pending.get("request_token_secret", ""),
            params={"oauth_verifier": verifier},
        )
        response = self._client.post(
            X_ACCESS_TOKEN,
            data={"oauth_verifier": verifier},
            headers={"Authorization": header},
        )
        if response.status_code >= 400:
            raise OAuthError(f"X access_token failed: {response.text[:300]}")

        payload = dict(parse_qsl(response.text))
        if "oauth_token" not in payload:
            raise OAuthError(f"X returned no access token: {response.text[:200]}")
        return ConnectedAccount(
            platform=self.platform,
            external_id=payload.get("user_id") or "me",
            display_name=payload.get("screen_name"),
            username=payload.get("screen_name"),
            access_token=payload["oauth_token"],
            token_secret=payload.get("oauth_token_secret"),
            scopes=["tweet.write", "media.upload"],
        )


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="~")


PROVIDERS = {
    Platform.YOUTUBE: YouTubeOAuth,
    Platform.TIKTOK: TikTokOAuth,
    Platform.INSTAGRAM: InstagramOAuth,
    Platform.X: XOAuth,
}


def get_provider(platform: Platform | str, settings) -> OAuthProvider:
    if isinstance(platform, str):
        platform = Platform(platform)
    return PROVIDERS[platform](settings)


def oauth_readiness(settings) -> dict[str, dict]:
    """Which platforms this install can start a connect flow for."""
    out = {}
    for platform in Platform:
        provider = get_provider(platform, settings)
        try:
            provider.redirect_uri()
            provider._credentials()
            out[platform.value] = {"ready": True, "reason": None,
                                   "refreshable": provider.supports_refresh}
        except OAuthError as exc:
            out[platform.value] = {"ready": False, "reason": str(exc),
                                   "refreshable": provider.supports_refresh}
    return out
