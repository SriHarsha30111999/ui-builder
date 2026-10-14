"""initial schema: projects and screenshots (MySQL 8.0.16+)

Revision ID: 0001
Revises:
Create Date: 2026-10-09

MySQL notes: DDL is not transactional (a failed migration is not rolled back); CHECK constraints are only
enforced from 8.0.16, so this migration refuses to run on older servers instead of silently ignoring them.
"""
import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import mysql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

BIN = "utf8mb4_bin"
TABLE_OPTS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}


def _require_enforced_checks() -> None:
    if context.is_offline_mode():
        return
    row = op.get_bind().execute(sa.text("SELECT VERSION()")).scalar_one()
    if "mariadb" in row.lower():
        raise RuntimeError("MariaDB is not supported; use MySQL 8.0.16 or newer.")
    parts = row.split("-")[0].split(".")
    if tuple(int(p) for p in parts[:3]) < (8, 0, 16):
        raise RuntimeError(f"MySQL {row} does not enforce CHECK constraints; 8.0.16 or newer is required.")


def upgrade() -> None:
    _require_enforced_checks()
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("master_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("master_type", sa.String(16), nullable=True),
        sa.Column("base_path", sa.String(100, collation=BIN), nullable=True),
        sa.Column("initial_prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), server_default="draft", nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_projects")),
        sa.CheckConstraint("type IN ('master', 'feature')", name=op.f("ck_projects_type_valid")),
        sa.CheckConstraint(
            "status IN ('draft', 'generating', 'build_failed', 'ready', 'published')",
            name=op.f("ck_projects_status_valid"),
        ),
        sa.CheckConstraint(
            "(type = 'master' AND master_id IS NULL AND master_type IS NULL AND base_path IS NULL) OR "
            "(type = 'feature' AND master_id IS NOT NULL AND master_type = 'master' AND base_path IS NOT NULL)",
            name=op.f("ck_projects_master_feature_shape"),
        ),
        sa.UniqueConstraint("id", "type", name=op.f("uq_projects_id_type")),
        sa.ForeignKeyConstraint(
            ["master_id", "master_type"],
            ["projects.id", "projects.type"],
            name=op.f("fk_projects_master_is_master"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("master_id", "base_path", name=op.f("uq_projects_master_base_path")),
        **TABLE_OPTS,
    )
    op.create_table(
        "screenshots",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("project_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(64, collation=BIN), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screenshots")),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_screenshots_size_positive")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_screenshots_storage_key")),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name=op.f("fk_screenshots_project_id_projects"), ondelete="RESTRICT"
        ),
        **TABLE_OPTS,
    )
    op.create_index(op.f("ix_screenshots_project_id"), "screenshots", ["project_id"])


def downgrade() -> None:
    op.drop_table("screenshots")
    op.drop_table("projects")
