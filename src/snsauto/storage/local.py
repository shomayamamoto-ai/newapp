"""Serve public assets from the application's own server.

Files are copied into a directory the web app serves at ``/public/<key>``.
That avoids an extra service, but it only works when the app is reachable over
public HTTPS: Instagram fetches the URL from Meta's servers, so localhost or a
self-signed certificate will fail there even though the link opens in your own
browser. ``SNSAUTO_PUBLIC_BASE_URL`` is therefore required, not inferred.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .base import PublicAsset, StorageError, content_type_for, key_for

log = logging.getLogger(__name__)


class LocalStorage:
    name = "local"

    def __init__(self, directory: str | Path, public_base_url: str):
        if not public_base_url:
            raise StorageError(
                "SNSAUTO_PUBLIC_BASE_URL is required for the local backend - "
                "Instagram fetches the video from its own servers, so the URL "
                "must be reachable from the public internet."
            )
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")

    def path_for(self, key: str) -> Path:
        """Resolve a key inside the public directory, refusing to escape it."""
        candidate = (self.directory / key).resolve()
        root = self.directory.resolve()
        if not candidate.is_relative_to(root):
            raise StorageError(f"key escapes the public directory: {key!r}")
        return candidate

    def upload(self, path: str | Path, prefix: str = "renders") -> PublicAsset:
        path = Path(path)
        if not path.is_file():
            raise StorageError(f"file not found: {path}")

        key = key_for(path, prefix)
        dest = self.path_for(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Content-addressed, so an identical file is already the right bytes.
        if not dest.exists():
            shutil.copy2(path, dest)

        return PublicAsset(
            key=key, url=f"{self.public_base_url}/public/{key}",
            size=dest.stat().st_size, content_type=content_type_for(dest),
            backend=self.name, expires_in=None,
        )

    def delete(self, key: str) -> None:
        target = self.path_for(key)
        if target.exists():
            target.unlink()

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "directory": str(self.directory),
            "public_base_url": self.public_base_url,
            "links": "permanent",
        }
