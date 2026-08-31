"""Minimal OAuth 1.0a HMAC-SHA1 request signing.

X's media-upload endpoints still require OAuth 1.0a user context, and pulling
in a full OAuth library for one signature is not worth the dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from base64 import b64encode
from urllib.parse import quote, urlencode, urlsplit


def _q(value: str) -> str:
    return quote(str(value), safe="~")


def sign(
    method: str,
    url: str,
    *,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    params: dict | None = None,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> str:
    """Return the value for an ``Authorization: OAuth ...`` header.

    ``params`` must include any query-string and form-encoded parameters, per
    RFC 5849 s3.4.1.3. Multipart/binary bodies are excluded from the signature.
    """
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }

    split = urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}{split.path}"

    collected: dict[str, str] = dict(params or {})
    collected.update(oauth)
    normalized = "&".join(
        f"{_q(k)}={_q(v)}" for k, v in sorted(collected.items(), key=lambda kv: kv[0])
    )
    base = "&".join([method.upper(), _q(base_url), _q(normalized)])
    key = f"{_q(consumer_secret)}&{_q(token_secret)}".encode()
    digest = hmac.new(key, base.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = b64encode(digest).decode()

    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))


def urlencode_params(params: dict) -> str:
    return urlencode(params, quote_via=quote)
