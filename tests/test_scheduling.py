"""Worker: scheduled publishing, snapshot cadence, and claim locking."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from snsauto.config import Settings
from snsauto.models import (
    Base, MetricSnapshot, Platform, Project, Publication, PublicationStatus, Render,
)
from snsauto.scheduling.jobs import HANDLERS, JobRunner
from snsauto.scheduling.worker import Worker, due_metric_targets, next_metric_due

SCHEDULE = (1, 3, 6, 12, 24)


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/w.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    settings = Settings(_env_file=None, SNSAUTO_WORKSPACE=str(tmp_path))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 32)
    with factory() as s:
        project = Project(name="w", brand_profile={})
        s.add(project)
        s.flush()
        render = Render(storyboard_id=1, path=str(video), duration_sec=10)
        s.add(render)
        s.flush()
        ids = {"project": project.id, "render": render.id}
        s.commit()
    return factory, settings, ids


def _schedule(factory, ids, platform, when):
    with factory() as s:
        pub = Publication(project_id=ids["project"], render_id=ids["render"],
                          platform=platform, status=PublicationStatus.SCHEDULED,
                          scheduled_for=when)
        s.add(pub)
        s.flush()
        s.commit()
        return pub.id


class TestCadence:
    def test_first_snapshot_is_an_hour_in(self):
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert next_metric_due(published, 0, SCHEDULE).hour == 1

    def test_cadence_decays(self):
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        gaps = [
            (next_metric_due(published, i + 1, SCHEDULE)
             - next_metric_due(published, i, SCHEDULE)).total_seconds()
            for i in range(len(SCHEDULE) - 1)
        ]
        assert gaps == sorted(gaps), "polling should get sparser, not denser"

    def test_schedule_ends(self):
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert next_metric_due(published, len(SCHEDULE), SCHEDULE) is None

    def test_only_due_publications_are_returned(self, env):
        factory, settings, ids = env
        now = datetime.now(timezone.utc)
        with factory() as s:
            fresh = Publication(project_id=ids["project"], platform=Platform.X,
                                status=PublicationStatus.PUBLISHED, external_id="a",
                                published_at=now - timedelta(minutes=10))
            old = Publication(project_id=ids["project"], platform=Platform.X,
                              status=PublicationStatus.PUBLISHED, external_id="b",
                              published_at=now - timedelta(hours=5))
            s.add_all([fresh, old])
            s.flush()
            due = due_metric_targets([fresh, old], SCHEDULE, now)
            assert [p.external_id for p in due] == ["b"]

    def test_a_taken_snapshot_pushes_the_next_one_out(self, env):
        factory, settings, ids = env
        now = datetime.now(timezone.utc)
        with factory() as s:
            pub = Publication(project_id=ids["project"], platform=Platform.X,
                              status=PublicationStatus.PUBLISHED, external_id="c",
                              published_at=now - timedelta(hours=2))
            s.add(pub)
            s.flush()
            assert due_metric_targets([pub], SCHEDULE, now)
            s.add(MetricSnapshot(publication_id=pub.id, captured_at=now))
            s.flush()
            s.refresh(pub)
            assert not due_metric_targets([pub], SCHEDULE, now)


class TestPublishing:
    def test_only_past_due_posts_are_picked_up(self, env):
        factory, settings, ids = env
        now = datetime.now(timezone.utc)
        _schedule(factory, ids, Platform.TIKTOK, now - timedelta(minutes=5))
        _schedule(factory, ids, Platform.X, now + timedelta(hours=3))
        worker = Worker(factory, settings, "w1")
        with factory() as s:
            assert [p.platform for p in worker.due_publications(s)] == [Platform.TIKTOK]

    def test_missing_credentials_fail_the_row_not_the_worker(self, env):
        factory, settings, ids = env
        _schedule(factory, ids, Platform.TIKTOK, datetime.now(timezone.utc) - timedelta(minutes=1))
        outcomes = Worker(factory, settings, "w1").tick()["published"]
        assert outcomes[0]["status"] == "failed"
        # The message points at the fix: connect the account in the UI.
        assert "未連携" in outcomes[0]["error"]

    def test_two_workers_never_publish_the_same_row(self, env):
        """A duplicate public post cannot be quietly undone."""
        factory, settings, ids = env
        _schedule(factory, ids, Platform.TIKTOK, datetime.now(timezone.utc) - timedelta(minutes=1))
        first = Worker(factory, settings, "w1").tick()["published"]
        second = Worker(factory, settings, "w2").tick()["published"]
        assert len(first) == 1 and second == []

    def test_a_publication_without_a_render_fails_cleanly(self, env):
        factory, settings, ids = env
        with factory() as s:
            s.add(Publication(project_id=ids["project"], platform=Platform.TIKTOK,
                              status=PublicationStatus.SCHEDULED,
                              scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1)))
            s.commit()
        outcomes = Worker(factory, settings, "w1").tick()["published"]
        assert outcomes[0]["status"] == "failed"
        assert "render" in outcomes[0]["error"]

    def test_run_stops_after_max_ticks(self, env):
        factory, settings, _ = env
        Worker(factory, settings, "w1").run(interval=0.01, max_ticks=2)


class TestJobs:
    def test_unknown_kind_is_rejected(self, env):
        factory, _, _ = env
        with factory() as s:
            with pytest.raises(ValueError):
                JobRunner.enqueue(s, "teleport", {})

    def test_every_kind_has_a_handler(self):
        assert set(HANDLERS) == {
            "research", "script", "video", "publish", "metrics", "experiment"
        }

    def test_a_job_runs_once(self, env):
        factory, settings, ids = env
        with factory() as s:
            job = JobRunner.enqueue(s, "metrics", {"project_id": ids["project"]})
            s.commit()
            job_id = job.id
        assert JobRunner(factory, settings).run_job(job_id)["status"] == "succeeded"
        again = JobRunner(factory, settings, "other").run_job(job_id)
        assert again["status"] == "skipped"

    def test_a_failing_job_records_the_error(self, env):
        factory, settings, ids = env
        with factory() as s:
            job = JobRunner.enqueue(s, "script", {"project_id": 999, "keyword": "x",
                                                  "platform": "tiktok"})
            s.commit()
            job_id = job.id
        outcome = JobRunner(factory, settings).run_job(job_id)
        assert outcome["status"] == "failed" and outcome["error"]

    def test_run_queued_drains_the_queue(self, env):
        factory, settings, ids = env
        with factory() as s:
            for _ in range(3):
                JobRunner.enqueue(s, "metrics", {"project_id": ids["project"]})
            s.commit()
        assert len(JobRunner(factory, settings).run_queued(limit=5)) == 3
