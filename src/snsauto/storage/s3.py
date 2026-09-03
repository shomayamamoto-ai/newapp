"""S3-compatible storage (AWS S3, Cloudflare R2, MinIO, Wasabi, ...).

All of these speak the same API, so one backend covers them; only the endpoint
and the public URL pattern differ, and both are configuration.

Public URLs come in two flavours and the choice matters. A bucket that serves
objects publicly (or sits behind a CDN) gives a permanent URL. A private bucket
gives a presigned URL that expires - which is fine for Instagram, because the
Graph API fetches the file within minutes, but is not a link to store and share.
``PublicAsset.expires_in`` records which one you got.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import PublicAsset, StorageError, content_type_for, key_for

log = logging.getLogger(__name__)

# Long enough for a slow Reels transcode to fetch the file, short enough that a
# leaked URL stops working the same day.
DEFAULT_EXPIRY = 6 * 3600


class S3Storage:
    name = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        public_base_url: str | None = None,
        expires_in: int = DEFAULT_EXPIRY,
        client=None,
    ):
        if not bucket:
            raise StorageError("S3_BUCKET is required for the s3 backend")
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region = region
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.expires_in = expires_in
        self._client = client or self._build_client(access_key, secret_key)

    def _build_client(self, access_key, secret_key):
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise StorageError(
                "boto3 not installed. Run: pip install 'snsauto[storage]'"
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

    def upload(self, path: str | Path, prefix: str = "renders") -> PublicAsset:
        path = Path(path)
        if not path.is_file():
            raise StorageError(f"file not found: {path}")

        key = key_for(path, prefix)
        content_type = content_type_for(path)
        try:
            self._client.upload_file(
                str(path), self.bucket, key,
                ExtraArgs={"ContentType": content_type},
            )
        except Exception as exc:
            raise StorageError(f"upload to s3://{self.bucket}/{key} failed: {exc}") from exc

        if self.public_base_url:
            # A bucket fronted by a CDN or public policy: the link is permanent.
            return PublicAsset(
                key=key, url=f"{self.public_base_url}/{key}", size=path.stat().st_size,
                content_type=content_type, backend=self.name, expires_in=None,
            )

        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.expires_in,
            )
        except Exception as exc:
            raise StorageError(f"presigning failed for {key}: {exc}") from exc

        return PublicAsset(
            key=key, url=url, size=path.stat().st_size, content_type=content_type,
            backend=self.name, expires_in=self.expires_in,
        )

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"delete of {key} failed: {exc}") from exc

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "bucket": self.bucket,
            "endpoint": self.endpoint_url or "aws",
            "public_base_url": self.public_base_url,
            "links": "permanent" if self.public_base_url else f"presigned ({self.expires_in}s)",
        }
