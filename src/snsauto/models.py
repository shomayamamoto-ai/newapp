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
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# SQLite cannot ALTER a constraint, so Alembic rebuilds the table in "batch"
# mode - which requires every constraint to have a name it can reproduce.
# Without a convention, autogenerate fails on any foreign key it adds later.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


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

    competitor_accounts: Mapped[list["CompetitorAccount"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
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

    # What shaped this population: which search options the platform actually
    # honoured, which it dropped, and how many posts the exclusion rules
    # removed. Without it a run's numbers cannot be compared to another run's.
    filters: Mapped[dict] = mapped_column(JSON, default=dict)

    # Set when this run is a scheduled sweep of one watched competitor rather
    # than a keyword search.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("competitor_accounts.id")
    )

    project: Mapped[Project] = relationship(back_populates="research_runs")
    account: Mapped["CompetitorAccount | None"] = relationship(
        back_populates="runs"
    )
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
    comments_mined: Mapped[list["PostComment"]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )
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


class CompetitorAccount(Base, TimestampMixin):
    """A competitor worth watching over time rather than searching for once.

    Keyword runs answer "what is working for this topic". Watching an account
    answers "what is this specific rival doing now", which is the question that
    actually recurs week to week.
    """

    __tablename__ = "competitor_accounts"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "platform", "handle", name="uq_account_handle"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    handle: Mapped[str] = mapped_column(String(200), index=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    label: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)

    project: Mapped[Project] = relationship(back_populates="competitor_accounts")
    runs: Mapped[list["ResearchRun"]] = relationship(back_populates="account")

    @property
    def display(self) -> str:
        return self.label or f"@{self.handle}"


class PostComment(Base, TimestampMixin):
    """A viewer comment on a competing post.

    The comment *count* was already in CompetitorPost. This is the text, which
    is where the questions people actually have get written down.
    """

    __tablename__ = "post_comments"
    __table_args__ = (
        UniqueConstraint("post_id", "external_id", name="uq_comment_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("competitor_posts.id"))
    external_id: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(200))
    likes: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Set by the mining pass: question | complaint | request | praise | other
    intent: Mapped[str | None] = mapped_column(String(40), index=True)

    # back_populates targets `comments_mined`, not `comments`: the latter is
    # the integer comment *count* that came back with the post.
    post: Mapped["CompetitorPost"] = relationship(back_populates="comments_mined")


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

    # Whether any line came back matching a competitor's copy, and what
    # matched. Recorded even when clean, so "not checked" and "checked and
    # clean" stay distinguishable before a post goes out.
    originality: Mapped[dict] = mapped_column(JSON, default=dict)

    # Which of this account's own measured findings shaped this script, and
    # how many posts they came from. Recorded so a script can be read back
    # against the evidence that produced it.
    playbook: Mapped[dict] = mapped_column(JSON, default=dict)

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
    renders: Mapped[list["Render"]] = relationship(back_populates="storyboard")
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

    # Needed to read a retention curve against the timeline that produced it:
    # the shots carry the second every telop appeared, because we placed them.
    storyboard: Mapped["Storyboard"] = relationship(back_populates="renders")


class Publication(Base, TimestampMixin):
    """A post (or scheduled post) on a platform."""

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    render_id: Mapped[int | None] = mapped_column(ForeignKey("renders.id"))
    script_id: Mapped[int | None] = mapped_column(ForeignKey("scripts.id"))

    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    # Which connected account this was posted as. Null means "whichever account
    # resolves at publish time", which is how single-account installs behave.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("social_accounts.id"))
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

    # What the platform actually set, which is not always what was asked for.
    # YouTube locks API uploads to private until the project passes its
    # compliance audit and TikTok forces SELF_ONLY until the app passes its
    # own; both return success. Kept apart from `error` because the post did
    # succeed - it is simply somewhere nobody can see it, and that has to
    # survive as a property of the post rather than a line in a log.
    visibility: Mapped[str | None] = mapped_column(String(40))
    warning: Mapped[str | None] = mapped_column(Text)

    # Held by the worker that is publishing this row, so two workers on one
    # database cannot post the same video twice.
    claimed_by: Mapped[str | None] = mapped_column(String(120))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Needed to read a post back against the choices that produced it: the
    # script carries the hook, the render carries the shots and the duration.
    script: Mapped["Script | None"] = relationship()
    render: Mapped["Render | None"] = relationship()
    snapshots: Mapped[list["MetricSnapshot"]] = relationship(
        back_populates="publication", cascade="all, delete-orphan"
    )
    account: Mapped["SocialAccount | None"] = relationship(
        foreign_keys=[account_id]
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

    # Retention, where the platform reports it. Nullable on purpose: a missing
    # retention figure is not a retention of zero, and storing 0.0 for it would
    # drag every average that touches it.
    avg_watch_sec: Mapped[float | None] = mapped_column(Float)
    retention_rate: Mapped[float | None] = mapped_column(Float)
    skip_rate: Mapped[float | None] = mapped_column(Float)
    reach: Mapped[int | None] = mapped_column(Integer)
    impressions: Mapped[int | None] = mapped_column(Integer)
    click_through_rate: Mapped[float | None] = mapped_column(Float)

    # Where viewers left, not just how many. Only YouTube reports this, and
    # only for the channel's own videos.
    retention_curve: Mapped[dict | None] = mapped_column(JSON)

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


class AlertLevel(str, enum.Enum):
    WARNING = "warning"
    ERROR = "error"


class Alert(Base, TimestampMixin):
    """Something that needs a human, raised by unattended work.

    Deduplicated on ``fingerprint``: a worker retrying a broken token every
    minute must not send sixty emails or bury the dashboard.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    level: Mapped[AlertLevel] = mapped_column(Enum(AlertLevel), default=AlertLevel.ERROR)
    source: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(String(200))
    context: Mapped[dict] = mapped_column(JSON, default=dict)

    @property
    def is_open(self) -> bool:
        return self.acknowledged_at is None


class SocialAccount(Base, TimestampMixin):
    """A connected platform account and its OAuth credentials.

    Credentials live here rather than in environment variables for three
    reasons: tokens have to be rewritten when they refresh, one install may
    run several accounts, and the UI needs to show when a connection is about
    to expire. Environment variables remain a fallback for single-account
    setups.
    """

    __tablename__ = "social_accounts"
    __table_args__ = (
        UniqueConstraint("project_id", "platform", "external_id", name="uq_account"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))

    # The platform's own identifier, and something a human recognises.
    external_id: Mapped[str] = mapped_column(String(200))
    display_name: Mapped[str | None] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(600))

    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    # X uses OAuth 1.0a, whose access token is a pair.
    token_secret: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list] = mapped_column(JSON, default=list)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_error: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    project: Mapped["Project | None"] = relationship()

    def seconds_until_expiry(self, now: datetime | None = None) -> float | None:
        if self.expires_at is None:
            return None
        now = now or utcnow()
        expires = self.expires_at
        if expires.tzinfo is None and now.tzinfo is not None:
            expires = expires.replace(tzinfo=now.tzinfo)
        return (expires - now).total_seconds()

    @property
    def is_expired(self) -> bool:
        remaining = self.seconds_until_expiry()
        return remaining is not None and remaining <= 0

    @property
    def can_refresh(self) -> bool:
        return bool(self.refresh_token)


class PublishAttempt(Base):
    """One publish call, kept so rate limits can be honoured locally.

    TikTok allows 25 posts per account per day and Instagram enforces a rolling
    window of its own. Counting our own calls lets the scheduler stop before
    the platform rejects a post, rather than burning a slot on an error.
    """

    __tablename__ = "publish_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("social_accounts.id"))
    publication_id: Mapped[int | None] = mapped_column(ForeignKey("publications.id"))
    platform: Mapped[Platform] = mapped_column(Enum(Platform))
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
