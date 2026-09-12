"""Posting-rate accounting, which keeps the scheduler inside platform caps."""

from datetime import timedelta

import pytest

from snsauto.config import Settings
from snsauto.models import Platform, PublishAttempt, SocialAccount, utcnow
from snsauto.platforms.accounts import DAILY_POST_CAP, AccountService


@pytest.fixture
def service(session):
    return AccountService(session, Settings(_env_file=None))


def _attempts(session, platform, count, hours_ago=1.0, succeeded=True, account_id=None):
    now = utcnow()
    for i in range(count):
        session.add(PublishAttempt(
            platform=platform, account_id=account_id, succeeded=succeeded,
            attempted_at=now - timedelta(hours=hours_ago) + timedelta(seconds=i),
        ))
    session.flush()


class TestWindow:
    def test_counts_only_successes(self, session, service):
        _attempts(session, Platform.TIKTOK, 3, succeeded=True)
        _attempts(session, Platform.TIKTOK, 5, succeeded=False)
        assert service.posts_in_window(Platform.TIKTOK) == 3

    def test_window_is_rolling_not_calendar(self, session, service):
        """These caps release 24h after each post, not at midnight."""
        _attempts(session, Platform.TIKTOK, 2, hours_ago=23.0)
        _attempts(session, Platform.TIKTOK, 2, hours_ago=25.0)
        assert service.posts_in_window(Platform.TIKTOK, hours=24.0) == 2

    def test_platforms_are_counted_separately(self, session, service):
        _attempts(session, Platform.TIKTOK, 3)
        _attempts(session, Platform.INSTAGRAM, 1)
        assert service.posts_in_window(Platform.TIKTOK) == 3
        assert service.posts_in_window(Platform.INSTAGRAM) == 1

    def test_accounts_are_counted_separately(self, session, service):
        session.add(SocialAccount(id=1, platform=Platform.TIKTOK,
                                  external_id="a", access_token="t"))
        session.add(SocialAccount(id=2, platform=Platform.TIKTOK,
                                  external_id="b", access_token="t"))
        session.flush()
        _attempts(session, Platform.TIKTOK, 3, account_id=1)
        _attempts(session, Platform.TIKTOK, 1, account_id=2)
        assert service.posts_in_window(Platform.TIKTOK, account_id=1) == 3
        assert service.posts_in_window(Platform.TIKTOK, account_id=2) == 1


class TestCaps:
    def test_under_the_cap_is_allowed(self, session, service):
        _attempts(session, Platform.TIKTOK, 10)
        assert service.check_rate(Platform.TIKTOK)["allowed"] is True

    def test_at_the_cap_is_refused_with_a_reopen_time(self, session, service):
        _attempts(session, Platform.TIKTOK, DAILY_POST_CAP[Platform.TIKTOK], hours_ago=5.0)
        rate = service.check_rate(Platform.TIKTOK)
        assert rate["allowed"] is False
        assert "上限" in rate["reason"]
        assert rate["frees_at"] is not None

    def test_platforms_without_a_post_cap_are_unrestricted(self, session, service):
        _attempts(session, Platform.YOUTUBE, 200)
        assert service.check_rate(Platform.YOUTUBE)["allowed"] is True

    def test_burst_limit_is_enforced(self, session, service):
        """TikTok caps upload initiation at 6 per minute."""
        _attempts(session, Platform.TIKTOK, 6, hours_ago=0.005)
        rate = service.check_rate(Platform.TIKTOK)
        assert rate["allowed"] is False
        assert "秒あたり" in rate["reason"]

    def test_older_bursts_do_not_block(self, session, service):
        _attempts(session, Platform.TIKTOK, 6, hours_ago=2.0)
        assert service.check_rate(Platform.TIKTOK)["allowed"] is True

    def test_failed_attempts_do_not_consume_the_cap(self, session, service):
        _attempts(session, Platform.TIKTOK, 30, succeeded=False)
        assert service.check_rate(Platform.TIKTOK)["allowed"] is True


class TestRecording:
    def test_success_and_failure_are_both_recorded(self, session, service):
        service.record_attempt(Platform.TIKTOK, None, 1, True)
        service.record_attempt(Platform.TIKTOK, None, 2, False, "token expired")
        rows = session.query(PublishAttempt).all()
        assert len(rows) == 2
        assert {r.succeeded for r in rows} == {True, False}
        assert any(r.error == "token expired" for r in rows)


class TestWorkerBehaviour:
    def test_a_capped_post_is_deferred_not_failed(self, tmp_path):
        """A cap is temporary, so the publication must stay scheduled and be
        retried once the window reopens."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session, sessionmaker

        from snsauto.models import (
            Base, Project, Publication, PublicationStatus, Render,
        )
        from snsauto.scheduling.worker import Worker

        engine = create_engine(f"sqlite:///{tmp_path}/w.db")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        settings = Settings(_env_file=None, SNSAUTO_WORKSPACE=str(tmp_path),
                            TIKTOK_ACCESS_TOKEN="env-token")
        video = tmp_path / "v.mp4"
        video.write_bytes(b"\x00" * 16)

        with factory() as s:
            project = Project(name="p", brand_profile={})
            s.add(project)
            s.flush()
            render = Render(storyboard_id=1, path=str(video), duration_sec=5)
            s.add(render)
            s.flush()
            s.add(Publication(
                project_id=project.id, render_id=render.id, platform=Platform.TIKTOK,
                status=PublicationStatus.SCHEDULED,
                scheduled_for=utcnow() - timedelta(minutes=1),
            ))
            now = utcnow()
            for i in range(DAILY_POST_CAP[Platform.TIKTOK]):
                s.add(PublishAttempt(platform=Platform.TIKTOK, succeeded=True,
                                     attempted_at=now - timedelta(hours=3) + timedelta(seconds=i)))
            s.commit()

        outcome = Worker(factory, settings, "w1").tick()["published"]
        assert outcome[0]["status"] == "deferred"

        with factory() as s:
            publication = s.query(Publication).one()
            assert publication.status == PublicationStatus.SCHEDULED
            assert "上限" in publication.error
