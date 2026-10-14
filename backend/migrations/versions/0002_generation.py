"""generation sessions and generated versions (MySQL 8.0.16+)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09

generated_versions and generation_sessions reference each other (session -> parent version, version -> session), so the
session -> parent_version foreign key is added after both tables exist. MySQL DDL is not transactional.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
NOW6 = sa.text("CURRENT_TIMESTAMP(6)")
STATUSES = "'created', 'queued', 'analyzing', 'generating', 'building', 'repairing', 'completed', 'failed'"


def upgrade() -> None:
    op.create_table(
        "generation_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("input_mode", sa.String(32), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("screenshot_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("build_log", sa.Text(), nullable=True),
        sa.Column("model_id", sa.String(200), nullable=True),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=NOW6, nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_sessions")),
        sa.CheckConstraint(
            "input_mode IN ('prompt_only', 'screenshot_only', 'prompt_and_screenshot')",
            name=op.f("ck_generation_sessions_input_mode_valid"),
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_generation_sessions_status_valid")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_generation_sessions_project_id_projects"), ondelete="RESTRICT"
        ),
        **TABLE_OPTS,
    )
    op.create_index(op.f("ix_generation_sessions_project_id"), "generation_sessions", ["project_id"])

    op.create_table(
        "generated_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("parent_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("uncertainties", sa.JSON(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("repaired", sa.Boolean(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=NOW6, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generated_versions")),
        sa.CheckConstraint("version_number > 0", name=op.f("ck_generated_versions_version_positive")),
        sa.UniqueConstraint("project_id", "version_number", name=op.f("uq_generated_versions_project_version")),
        sa.UniqueConstraint("session_id", name=op.f("uq_generated_versions_session_id")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_generated_versions_project_id_projects"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["generation_sessions.id"],
            name=op.f("fk_generated_versions_session_id_generation_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["generated_versions.id"],
            name=op.f("fk_generated_versions_parent_version_id_generated_versions"),
            ondelete="RESTRICT",
        ),
        **TABLE_OPTS,
    )
    op.create_index(op.f("ix_generated_versions_project_id"), "generated_versions", ["project_id"])
    op.create_foreign_key(
        op.f("fk_generation_sessions_parent_version_id_generated_versions"),
        "generation_sessions",
        "generated_versions",
        ["parent_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_generation_sessions_parent_version_id_generated_versions"), "generation_sessions", type_="foreignkey"
    )
    op.drop_table("generated_versions")
    op.drop_table("generation_sessions")
