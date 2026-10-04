"""Public asset hosting.

Instagram's Graph API will not accept video bytes - it fetches the file from a
URL you supply, so a render has to be reachable over public HTTPS before it can
be posted. That makes storage a required link in the publishing chain, not an
optional convenience.

Two backends implement the same contract: an S3-compatible bucket (AWS, R2,
MinIO, Wasabi) and the application's own server. The pipeline does not care
which is configured.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    pass


@dataclass(slots=True)
class PublicAsset:
    key: str
    url: str
    size: int
    content_type: str
    backend: str
    # Set when the URL stops working after a while, so callers can tell a
    # permanent link from a temporary one.
    expires_in: int | None = None


def content_type_for(path: str | Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def key_for(path: str | Path, prefix: str = "renders") -> str:
    """A stable, collision-resistant key derived from the file's contents.

    Content-addressing means re-uploading the same render reuses the same
    object instead of filling the bucket with duplicates.
    """
    path = Path(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"{prefix}/{digest.hexdigest()[:32]}{path.suffix.lower()}"


@runtime_checkable
class StorageBackend(Protocol):
    name: str

    def upload(self, path: str | Path, prefix: str = "renders") -> PublicAsset: ...

    def delete(self, key: str) -> None: ...

    def describe(self) -> dict: ...
