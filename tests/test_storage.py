"""Public asset hosting - the link Instagram publishing depends on."""

import pytest

from snsauto.config import Settings
from snsauto.storage import LocalStorage, S3Storage, StorageError, build_storage, storage_status
from snsauto.storage.base import content_type_for, key_for


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "render.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 512)
    return path


class TestKeys:
    def test_key_is_content_addressed(self, tmp_path, video):
        twin = tmp_path / "copy.mp4"
        twin.write_bytes(video.read_bytes())
        assert key_for(video) == key_for(twin)

    def test_different_content_gives_a_different_key(self, tmp_path, video):
        other = tmp_path / "other.mp4"
        other.write_bytes(b"different bytes entirely")
        assert key_for(video) != key_for(other)

    def test_key_keeps_the_extension(self, video):
        assert key_for(video).endswith(".mp4")

    def test_prefix_is_applied(self, video):
        assert key_for(video, "thumbs").startswith("thumbs/")

    def test_content_type_is_guessed(self, video):
        assert content_type_for(video) == "video/mp4"
        assert content_type_for("x.unknownext") == "application/octet-stream"


class TestLocal:
    def test_upload_returns_a_public_url(self, tmp_path, video):
        storage = LocalStorage(tmp_path / "public", "https://x.example.com")
        asset = storage.upload(video)
        assert asset.url.startswith("https://x.example.com/public/renders/")
        assert asset.expires_in is None
        assert (tmp_path / "public" / asset.key).is_file()

    def test_reupload_is_idempotent(self, tmp_path, video):
        storage = LocalStorage(tmp_path / "public", "https://x.example.com")
        assert storage.upload(video).url == storage.upload(video).url

    def test_a_public_base_url_is_required(self, tmp_path):
        """Meta fetches from its own servers, so a guessed URL would fail there."""
        with pytest.raises(StorageError):
            LocalStorage(tmp_path / "public", "")

    def test_missing_file_is_refused(self, tmp_path):
        storage = LocalStorage(tmp_path / "public", "https://x.example.com")
        with pytest.raises(StorageError):
            storage.upload(tmp_path / "nope.mp4")

    @pytest.mark.parametrize("key", ["../secrets.env", "a/../../etc/passwd", "/etc/passwd"])
    def test_keys_cannot_escape_the_directory(self, tmp_path, key):
        storage = LocalStorage(tmp_path / "public", "https://x.example.com")
        with pytest.raises(StorageError):
            storage.path_for(key)

    def test_delete_removes_the_file(self, tmp_path, video):
        storage = LocalStorage(tmp_path / "public", "https://x.example.com")
        asset = storage.upload(video)
        storage.delete(asset.key)
        assert not (tmp_path / "public" / asset.key).exists()


class FakeS3:
    def __init__(self):
        self.uploaded = []
        self.deleted = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.uploaded.append((filename, bucket, key, ExtraArgs))

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://s3.example.com/{Params['Bucket']}/{Params['Key']}?X-Expires={ExpiresIn}"

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))


class TestS3:
    def test_private_bucket_gets_an_expiring_url(self, video):
        client = FakeS3()
        asset = S3Storage("bucket", client=client).upload(video)
        assert "X-Expires" in asset.url
        assert asset.expires_in is not None

    def test_public_base_url_gives_a_permanent_link(self, video):
        client = FakeS3()
        storage = S3Storage("bucket", public_base_url="https://cdn.example.com/", client=client)
        asset = storage.upload(video)
        assert asset.url == f"https://cdn.example.com/{asset.key}"
        assert asset.expires_in is None

    def test_content_type_is_sent(self, video):
        client = FakeS3()
        S3Storage("bucket", client=client).upload(video)
        assert client.uploaded[0][3] == {"ContentType": "video/mp4"}

    def test_bucket_is_required(self):
        with pytest.raises(StorageError):
            S3Storage("", client=FakeS3())

    def test_upload_errors_are_wrapped(self, video):
        class Broken(FakeS3):
            def upload_file(self, *a, **k):
                raise RuntimeError("network down")

        with pytest.raises(StorageError):
            S3Storage("bucket", client=Broken()).upload(video)

    def test_delete_passes_through(self, video):
        client = FakeS3()
        storage = S3Storage("bucket", client=client)
        storage.delete("renders/abc.mp4")
        assert client.deleted == [("bucket", "renders/abc.mp4")]


class TestFactory:
    def test_unset_returns_none(self):
        assert build_storage(Settings(_env_file=None)) is None

    def test_unknown_backend_is_refused(self):
        with pytest.raises(StorageError):
            build_storage(Settings(_env_file=None, STORAGE_BACKEND="ftp"))

    def test_status_explains_why_instagram_is_blocked(self):
        status = storage_status(Settings(_env_file=None))
        assert status["configured"] is False
        assert "Instagram" in status["error"]

    def test_status_reports_a_working_backend(self, tmp_path):
        settings = Settings(
            _env_file=None, STORAGE_BACKEND="local",
            SNSAUTO_PUBLIC_BASE_URL="https://x.example.com",
            SNSAUTO_WORKSPACE=str(tmp_path),
        )
        status = storage_status(settings)
        assert status["configured"] and status["backend"] == "local"

    def test_status_surfaces_misconfiguration_instead_of_raising(self):
        settings = Settings(_env_file=None, STORAGE_BACKEND="local")
        assert storage_status(settings)["configured"] is False
