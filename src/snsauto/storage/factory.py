"""Backend selection from configuration."""

from __future__ import annotations

import logging

from ..config import get_settings
from .base import StorageError
from .local import LocalStorage
from .s3 import S3Storage

log = logging.getLogger(__name__)


def build_storage(settings=None):
    """Return the configured backend, or None when hosting is switched off."""
    settings = settings or get_settings()
    backend = (settings.storage_backend or "none").lower()

    if backend in ("none", "", "off"):
        return None
    if backend == "s3":
        return S3Storage(
            settings.s3_bucket or "",
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            public_base_url=settings.s3_public_base_url,
            expires_in=settings.s3_expires_in,
        )
    if backend == "local":
        return LocalStorage(
            settings.workspace / "public", settings.public_base_url or ""
        )
    raise StorageError(f"unknown STORAGE_BACKEND {backend!r}; use s3, local or none")


def storage_status(settings=None) -> dict:
    """What the UI shows on the capabilities page."""
    settings = settings or get_settings()
    try:
        storage = build_storage(settings)
    except StorageError as exc:
        return {"configured": False, "error": str(exc)}
    if storage is None:
        return {
            "configured": False,
            "error": "STORAGE_BACKEND is not set - Instagram publishing is unavailable",
        }
    return {"configured": True, **storage.describe()}
