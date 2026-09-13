"""Running several accounts per platform."""

import subprocess

import pytest

from snsauto.config import Settings
from snsauto.creative.script import ScriptService
from snsauto.models import (
    Platform, PublicationStatus, Render, SocialAccount,
)
from snsauto.pipeline import Pipeline
from snsauto.platforms.accounts import AccountService


def _account(session, platform, name, project_id=None, token=None, active=True):
    account = SocialAccount(
        platform=platform, external_id=f"{name}-id", display_name=name,
        username=name, access_token=token or f"{name}-token",
        project_id=project_id, is_active=active,
    )
    session.add(account)
    session.flush()
    return account


@pytest.fixture
def render(session, project, tmp_path):
    # A real file: publishing runs the quality gate, and a path that does not
    # exist is a blocking defect - correctly, since the upload would fail too.
    video = tmp_path / "v.mp4"
    from snsauto.media.ffmpeg import ffmpeg_path

    subprocess.run(
        [ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
         "-t", "1", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-shortest",
         str(video)],
        check=True,
    )
    row = Render(storyboard_id=1, path=str(video), duration_sec=10)
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def script(session, project):
    return ScriptService(session).generate(project, "副業", Platform.TIKTOK, 12.0)


@pytest.fixture
def pipeline(session):
    return Pipeline(session, Settings(_env_file=None), llm=None)


class TestTargetExpansion:
    def test_a_platform_expands_to_every_connected_account(self, session, project, pipeline):
        _account(session, Platform.TIKTOK, "main")
        _account(session, Platform.TIKTOK, "second")
        targets = pipeline.publish_targets(project, [Platform.TIKTOK])
        assert len(targets) == 2
        assert {t[0] for t in targets} == {Platform.TIKTOK}
        assert None not in {t[1] for t in targets}

    def test_named_accounts_are_used_verbatim(self, session, project, pipeline):
        first = _account(session, Platform.TIKTOK, "main")
        _account(session, Platform.TIKTOK, "second")
        targets = pipeline.publish_targets(project, None, [first.id])
        assert targets == [(Platform.TIKTOK, first.id)]

    def test_accounts_across_platforms_can_be_mixed(self, session, project, pipeline):
        tiktok = _account(session, Platform.TIKTOK, "tt")
        youtube = _account(session, Platform.YOUTUBE, "yt")
        targets = pipeline.publish_targets(project, None, [tiktok.id, youtube.id])
        assert {t[0] for t in targets} == {Platform.TIKTOK, Platform.YOUTUBE}

    def test_no_connected_accounts_still_yields_one_target(self, session, project, pipeline):
        """Single-account installs on environment variables keep working."""
        assert pipeline.publish_targets(project, [Platform.X]) == [(Platform.X, None)]

    def test_disconnected_accounts_are_skipped(self, session, project, pipeline):
        _account(session, Platform.TIKTOK, "live")
        dead = _account(session, Platform.TIKTOK, "dead", active=False)
        targets = pipeline.publish_targets(project, [Platform.TIKTOK])
        assert dead.id not in {t[1] for t in targets}

    def test_naming_a_disconnected_account_is_ignored(self, session, project, pipeline):
        dead = _account(session, Platform.TIKTOK, "dead", active=False)
        assert pipeline.publish_targets(project, None, [dead.id]) == []

    def test_a_named_account_is_not_duplicated_by_its_platform(self, session, project, pipeline):
        first = _account(session, Platform.TIKTOK, "main")
        _account(session, Platform.TIKTOK, "second")
        targets = pipeline.publish_targets(project, [Platform.TIKTOK], [first.id])
        assert len(targets) == 2 and len(set(targets)) == 2

    def test_shared_and_project_accounts_both_receive(self, session, project, pipeline):
        shared = _account(session, Platform.TIKTOK, "shared")
        mine = _account(session, Platform.TIKTOK, "mine", project_id=project.id)
        ids = {t[1] for t in pipeline.publish_targets(project, [Platform.TIKTOK])}
        assert ids == {shared.id, mine.id}


class TestPublicationBinding:
    def test_each_account_gets_its_own_publication(
        self, session, project, render, script, pipeline
    ):
        first = _account(session, Platform.TIKTOK, "main")
        second = _account(session, Platform.TIKTOK, "second")
        publications = pipeline.publish(
            project, render, script, [Platform.TIKTOK], dry_run=True
        )
        assert len(publications) == 2
        assert {p.account_id for p in publications} == {first.id, second.id}

    def test_the_account_is_recorded_on_the_publication(
        self, session, project, render, script, pipeline
    ):
        account = _account(session, Platform.TIKTOK, "main")
        publication = pipeline.publish(
            project, render, script, None, dry_run=True, account_ids=[account.id]
        )[0]
        assert publication.account_id == account.id
        assert publication.account.display_name == "main"

    def test_metrics_follow_the_publishing_account(self, session, project, render, script):
        """A post's metrics are only readable with the token that made it."""
        from snsauto.platforms import adapter_for_account

        first = _account(session, Platform.INSTAGRAM, "one", token="token-one")
        second = _account(session, Platform.INSTAGRAM, "two", token="token-two")
        settings = Settings(_env_file=None)

        adapter_one, _ = adapter_for_account(
            Platform.INSTAGRAM, session, settings, project.id, first.id
        )
        adapter_two, _ = adapter_for_account(
            Platform.INSTAGRAM, session, settings, project.id, second.id
        )
        assert adapter_one._token() == "token-one"
        assert adapter_two._token() == "token-two"


class TestPerAccountLimits:
    def test_caps_are_counted_per_account(self, session, project):
        """Two connected accounts each get a full allowance."""
        from snsauto.models import PublishAttempt
        from snsauto.platforms.accounts import DAILY_POST_CAP

        busy = _account(session, Platform.TIKTOK, "busy")
        fresh = _account(session, Platform.TIKTOK, "fresh")
        for _ in range(DAILY_POST_CAP[Platform.TIKTOK]):
            session.add(PublishAttempt(platform=Platform.TIKTOK,
                                       account_id=busy.id, succeeded=True))
        session.flush()

        service = AccountService(session, Settings(_env_file=None))
        assert service.check_rate(Platform.TIKTOK, busy.id)["allowed"] is False
        assert service.check_rate(Platform.TIKTOK, fresh.id)["allowed"] is True

    def test_a_capped_account_does_not_block_the_others(
        self, session, project, render, script, pipeline, monkeypatch
    ):
        from snsauto.models import PublishAttempt
        from snsauto.platforms.accounts import DAILY_POST_CAP
        from snsauto.platforms.base import Capability, PublishResult

        busy = _account(session, Platform.TIKTOK, "busy")
        fresh = _account(session, Platform.TIKTOK, "fresh")
        for _ in range(DAILY_POST_CAP[Platform.TIKTOK]):
            session.add(PublishAttempt(platform=Platform.TIKTOK,
                                       account_id=busy.id, succeeded=True))
        session.flush()

        # Stub the adapter: the uncapped account now gets far enough to make a
        # real API call, and a test must never leave the machine.
        posted = []

        class StubAdapter:
            platform = Platform.TIKTOK

            def capabilities(self):
                return {Capability.PUBLISH}

            def publish(self, request):
                posted.append(request)
                return PublishResult(external_id="posted-1",
                                     url="https://tiktok.com/@x/video/1")

        from snsauto.platforms.accounts import Credentials

        def stub_adapter(platform, session_, settings_, project_id, account_id):
            # Credentials must carry the account id: without it the rate check
            # falls back to counting the whole platform, and one exhausted
            # account would appear to cap every other one.
            return StubAdapter(), Credentials(
                access_token="t", account_id=account_id, source="connected"
            )

        monkeypatch.setattr("snsauto.pipeline.adapter_for_account", stub_adapter)

        publications = pipeline.publish(
            project, render, script, [Platform.TIKTOK], dry_run=False
        )
        by_account = {p.account_id: p for p in publications}

        # Capped: deferred rather than failed, so the slot is retried later.
        assert by_account[busy.id].status == PublicationStatus.SCHEDULED
        assert "上限" in by_account[busy.id].error
        # Uncapped: unaffected by the other account's exhausted quota.
        assert by_account[fresh.id].status == PublicationStatus.PUBLISHED
        assert len(posted) == 1


class TestLabels:
    def test_label_includes_the_handle(self, session):
        account = _account(session, Platform.TIKTOK, "demo")
        service = AccountService(session, Settings(_env_file=None))
        assert service.label(account) == "demo @demo"

    def test_label_falls_back_to_the_id(self, session):
        account = SocialAccount(platform=Platform.X, external_id="123",
                                access_token="t")
        session.add(account)
        session.flush()
        assert AccountService(session, Settings(_env_file=None)).label(account) == "123"
