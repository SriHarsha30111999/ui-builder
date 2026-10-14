import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Integer,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Naming convention => constraint names are stable, so migrations and error handling can rely on them.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator):
    """MySQL DATETIME has no time zone. Connections run in UTC (see db.py); values are returned tz-aware (UTC)
    so the API serializes them with an explicit offset and browsers do not read them as local time."""

    impl = DateTime(timezone=False).with_variant(mysql.DATETIME(fsp=6), "mysql")
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


# DATETIME(6): microsecond precision; maintained by the server so every writer gets the same behavior.
NOW6 = text("CURRENT_TIMESTAMP(6)")
NOW6_ON_UPDATE = text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)")
# Binary collation: exact, case-sensitive comparison (MySQL's default collation is case/accent-insensitive).
BIN = "utf8mb4_bin"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("type IN ('master', 'feature')", name="type_valid"),
        CheckConstraint(
            "status IN ('draft', 'generating', 'build_failed', 'ready', 'published')", name="status_valid"
        ),
        # Master: no parent, no base path. Feature: parent Master and base path both required.
        CheckConstraint(
            "(type = 'master' AND master_id IS NULL AND master_type IS NULL AND base_path IS NULL) OR "
            "(type = 'feature' AND master_id IS NOT NULL AND master_type = 'master' AND base_path IS NOT NULL)",
            name="master_feature_shape",
        ),
        # Target of the composite FK below: lets the DB verify the referenced row is a Master.
        UniqueConstraint("id", "type", name="uq_projects_id_type"),
        ForeignKeyConstraint(
            ["master_id", "master_type"],
            ["projects.id", "projects.type"],
            name="fk_projects_master_is_master",
            ondelete="RESTRICT",
        ),
        # Canonical-path uniqueness per Master; concurrent duplicates fail here.
        UniqueConstraint("master_id", "base_path", name="uq_projects_master_base_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    master_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    master_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    base_path: Mapped[str | None] = mapped_column(String(100, collation=BIN), nullable=True)
    initial_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")  # TEXT cannot have a literal DEFAULT in MySQL
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="draft")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=NOW6)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=NOW6_ON_UPDATE)

    screenshots: Mapped[list["Screenshot"]] = relationship(
        back_populates="project", order_by="Screenshot.created_at, Screenshot.id"
    )


class Screenshot(Base):
    __tablename__ = "screenshots"
    __table_args__ = (
        CheckConstraint("size_bytes > 0", name="size_positive"),
        UniqueConstraint("storage_key", name="uq_screenshots_storage_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Generated file name inside the storage directory; never derived from client input.
    storage_key: Mapped[str] = mapped_column(String(64, collation=BIN), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=NOW6)

    project: Mapped[Project] = relationship(back_populates="screenshots")


class GeneratedVersion(Base):
    """A successfully built UI. Rows exist only for builds that succeeded, so a failed attempt can never replace
    or masquerade as a previous good version."""

    __tablename__ = "generated_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_positive"),
        UniqueConstraint("project_id", "version_number", name="uq_generated_versions_project_version"),
        UniqueConstraint("session_id", name="uq_generated_versions_session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("generation_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("generated_versions.id", ondelete="RESTRICT"), nullable=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    uncertainties: Mapped[list] = mapped_column(JSON, nullable=False)
    files: Mapped[list] = mapped_column(JSON, nullable=False)  # [{"path", "size"}] of the source files (scaffold included)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    repaired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=NOW6)


class GenerationSession(Base):
    __tablename__ = "generation_sessions"
    __table_args__ = (
        CheckConstraint("input_mode IN ('prompt_only', 'screenshot_only', 'prompt_and_screenshot')", name="input_mode_valid"),
        CheckConstraint(
            "status IN ('created', 'queued', 'analyzing', 'generating', 'building', 'repairing', 'completed', 'failed')",
            name="status_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("generated_versions.id", ondelete="RESTRICT"), nullable=True
    )
    input_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="created")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    build_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=NOW6)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, server_default=NOW6_ON_UPDATE)
