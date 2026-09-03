"""Persistent domain model.

The schema is the accumulation layer ("データ蓄積"): every research run,
generated artefact, publication and metric snapshot is retained so PDCA
cycles can compare hypotheses against measured outcomes over time.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Platform(str, enum.Enum):
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    X = "x"


class PublicationStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    FAILED = "failed"


class PdcaStage(str, enum.Enum):
    PLAN = "plan"
    DO = "do"
    CHECK = "check"
    ACT = "act"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Project(Base, TimestampMixin):
    """A brand / account / campaign that the tool operates for."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    # Persona, tone, banned words, brand colours - fed into every LLM prompt.
    brand_profile: Mapped[dict] = mapped_column(JSON, default=dict)

    research_runs: Mapped[list["ResearchRun"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    scripts: Mapped[list["Script"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    pdca_cycles: Mapped[list["PdcaCycle"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ResearchRun(Base, TimestampMixin):
    """One keyword sweep across one platform (the 'top 50' collection)."""

    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    keyword: Mapped[str] = mapped_column(String(300), index=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    limit: Mapped[int] = mapped_column(Integer, default=50)
    source: Mapped[str] = mapped_column(String(50), default="api")
    notes: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="research_runs")
    posts: Mapped[list["CompetitorPost"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class CompetitorPost(Base, TimestampMixin):
    """A single competing post captured during a research run."""

    __tablename__ = "competitor_posts"
    __table_args__ = (UniqueConstraint("run_id", "external_id", name="uq_run_external"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("research_runs.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    rank: Mapped[int] = mapped_column(Integer, default=0)

    url: Mapped[str | None] = mapped_column(String(600))
    title: Mapped[str | None] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_sec: Mapped[float | None] = mapped_column(Float)

    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)

    # Derived during analysis so ranking stays reproducible.
    engagement_rate: Mapped[float] = mapped_column(Float, default=0.0)
    velocity: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0)

    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped[ResearchRun] = relationship(back_populates="posts")
    structure: Mapped["StructureAnalysis | None"] = relationship(
        back_populates="post", cascade="all, delete-orphan", uselist=False
    )


class StructureAnalysis(Base, TimestampMixin):
    """Composition / telop breakdown of a competing post."""

    __tablename__ = "structure_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("competitor_posts.id"), unique=True)

    hook_text: Mapped[str | None] = mapped_column(Text)
    hook_type: Mapped[str | None] = mapped_column(String(80))
    # Ordered beats: [{"label": "hook", "start": 0.0, "end": 2.5, "purpose": "..."}]
    beats: Mapped[list] = mapped_column(JSON, default=list)
    # Telop stats: density, avg chars, placement, colour usage.
    telop: Mapped[dict] = mapped_column(JSON, default=dict)
    cta: Mapped[str | None] = mapped_column(Text)
    takeaways: Mapped[list] = mapped_column(JSON, default=list)

    post: Mapped[CompetitorPost] = relationship(back_populates="structure")


class Script(Base, TimestampMixin):
    """A generated 台本."""

    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("research_runs.id"))
    title: Mapped[str] = mapped_column(String(300))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    target_duration_sec: Mapped[float] = mapped_column(Float, default=30.0)

    hook: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(Text)
    # [{"index":0,"start":0.0,"end":3.0,"narration":"...","telop":"...","visual":"..."}]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)

    project: Mapped[Project] = relationship(back_populates="scripts")
    storyboards: Mapped[list["Storyboard"]] = relationship(
        back_populates="script", cascade="all, delete-orphan"
    )


class Storyboard(Base, TimestampMixin):
    """A generated 絵コンテ derived from a script."""

    __tablename__ = "storyboards"

    id: Mapped[int] = mapped_column(primary_key=True)
    script_id: Mapped[int] = mapped_column(ForeignKey("scripts.id"))
    aspect_ratio: Mapped[str] = mapped_column(String(20), default="9:16")
    style: Mapped[str | None] = mapped_column(String(200))

    script: Mapped[Script] = relationship(back_populates="storyboards")
    shots: Mapped[list["Shot"]] = relationship(
        back_populates="storyboard",
        cascade="all, delete-orphan",
        order_by="Shot.index",
    )


class Shot(Base, TimestampMixin):
    """One cut in the storyboard, and the asset generated for it."""

    __tablename__ = "shots"

    id: Mapped[int] = mapped_column(primary_key=True)
    storyboard_id: Mapped[int] = mapped_column(ForeignKey("storyboards.id"))
    index: Mapped[int] = mapped_column(Integer)

    start: Mapped[float] = mapped_column(Float, default=0.0)
    end: Mapped[float] = mapped_column(Float, default=0.0)
    narration: Mapped[str | None] = mapped_column(Text)
    telop: Mapped[str | None] = mapped_column(Text)
    visual_prompt: Mapped[str | None] = mapped_column(Text)
    camera: Mapped[str | None] = mapped_column(String(200))
    transition: Mapped[str | None] = mapped_column(String(80), default="cut")

    image_path: Mapped[str | None] = mapped_column(String(600))
    clip_path: Mapped[str | None] = mapped_column(String(600))

    storyboard: Mapped[Storyboard] = relationship(back_populates="shots")

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Render(Base, TimestampMixin):
    """A finished video file assembled from a storyboard."""

    __tablename__ = "renders"

    id: Mapped[int] = mapped_column(primary_key=True)
    storyboard_id: Mapped[int] = mapped_column(ForeignKey("storyboards.id"))
    path: Mapped[str] = mapped_column(String(600))
    width: Mapped[int] = mapped_column(Integer, default=1080)
    height: Mapped[int] = mapped_column(Integer, default=1920)
    fps: Mapped[int] = mapped_column(Integer, default=30)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    preset: Mapped[str | None] = mapped_column(String(80))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Publication(Base, TimestampMixin):
    """A post (or scheduled post) on a platform."""

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    render_id: Mapped[int | None] = mapped_column(ForeignKey("renders.id"))
    script_id: Mapped[int | None] = mapped_column(ForeignKey("scripts.id"))

    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    status: Mapped[PublicationStatus] = mapped_column(
        Enum(PublicationStatus), default=PublicationStatus.DRAFT
    )
    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)

    external_id: Mapped[str | None] = mapped_column(String(200))
    external_url: Mapped[str | None] = mapped_column(String(600))
    error: Mapped[str | None] = mapped_column(Text)

    # Held by the worker that is publishing this row, so two workers on one
    # database cannot post the same video twice.
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)

    snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="publication", cascade="all, delete-orphan"
    )


class MetricSnapshot(Base):
    """Time series of a publication's performance."""

    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publications.id"))
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    watch_time_sec: Mapped[float] = mapped_column(Float, default=0.0)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    publication: Mapped[Publication] = relationship(back_populates="snapshots")


class PdcaCycle(Base, TimestampMixin):
    """One improvement loop: hypothesis -> execution -> measurement -> decision."""

    __tablename__ = "pdca_cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(300))
    stage: Mapped[PdcaStage] = mapped_column(Enum(PdcaStage), default=PdcaStage.PLAN)

    hypothesis: Mapped[str | None] = mapped_column(Text)
    # {"metric": "engagement_rate", "target": 0.06, "baseline": 0.041}
    target: Mapped[dict] = mapped_column(JSON, default=dict)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    publication_ids: Mapped[list] = mapped_column(JSON, default=list)

    result: Mapped[dict] = mapped_column(JSON, default=dict)
    verdict: Mapped[str | None] = mapped_column(String(40))
    learnings: Mapped[str | None] = mapped_column(Text)
    next_actions: Mapped[list] = mapped_column(JSON, default=list)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    project: Mapped[Project] = relationship(back_populates="pdca_cycles")


class Report(Base, TimestampMixin):
    """A rendered HTML / PDF report."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(String(80))
    template: Mapped[str] = mapped_column(String(200))
    html_path: Mapped[str | None] = mapped_column(String(600))
    pdf_path: Mapped[str | None] = mapped_column(String(600))
    context: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------------
# Visual sourcing, voice, scheduling, experiments and access control
# ---------------------------------------------------------------------------


class VisualMode(str, enum.Enum):
    """How a shot's picture is produced."""

    STILL = "still"        # generated image + Ken Burns move
    ANIMATE = "animate"    # image -> video model, the shot actually moves
    FOOTAGE = "footage"    # real / stock footage matched to the shot


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ClipAsset(Base, TimestampMixin):
    """A piece of real or stock footage available to the matcher."""

    __tablename__ = "clip_assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(600), unique=True)
    label: Mapped[str | None] = mapped_column(String(300))
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    has_audio: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(80), default="local")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def is_vertical(self) -> bool:
        return self.height > self.width


class Experiment(Base, TimestampMixin):
    """An A/B test: several variants that differ in exactly one dimension."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    cycle_id: Mapped[int | None] = mapped_column(ForeignKey("pdca_cycles.id"))
    name: Mapped[str] = mapped_column(String(300))
    # The single thing that differs between variants - holding everything else
    # constant is what makes the comparison mean anything.
    dimension: Mapped[str] = mapped_column(String(80), default="hook")
    metric: Mapped[str] = mapped_column(String(80), default="engagement_rate")
    base_script_id: Mapped[int | None] = mapped_column(ForeignKey("scripts.id"))
    winner_variant_id: Mapped[int | None] = mapped_column(Integer)
    conclusion: Mapped[dict] = mapped_column(JSON, default=dict)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)

    project: Mapped[Project] = relationship()
    variants: Mapped[list["Variant"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", order_by="Variant.id"
    )


class Variant(Base, TimestampMixin):
    """One arm of an experiment."""

    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    script_id: Mapped[int | None] = mapped_column(ForeignKey("scripts.id"))
    label: Mapped[str] = mapped_column(String(80))
    # What this arm actually changed, e.g. {"hook": "...", "hook_type": "question"}
    treatment: Mapped[dict] = mapped_column(JSON, default=dict)
    publication_ids: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[dict] = mapped_column(JSON, default=dict)

    experiment: Mapped[Experiment] = relationship(back_populates="variants")
    script: Mapped["Script | None"] = relationship()


class Job(Base, TimestampMixin):
    """A unit of background work started from the web UI or the worker."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    kind: Mapped[str] = mapped_column(String(80))
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.QUEUED)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Set while a worker holds the job, so two workers cannot run it twice.
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)


class User(Base, TimestampMixin):
    """A login for the web UI. Only used when the UI is exposed beyond localhost."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(40), default="editor")  # admin | editor | viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "editor")

    @property
    def can_publish(self) -> bool:
        return self.role == "admin"
