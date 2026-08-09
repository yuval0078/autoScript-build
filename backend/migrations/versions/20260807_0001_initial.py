"""Create users, experiments, and immutable experiment versions."""

from alembic import op
import sqlalchemy as sa


revision = "20260807_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.String(length=32),
            server_default=sa.text("'researcher'"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "experiments",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_experiments_owner_name"),
    )
    op.create_index("ix_experiments_owner_id", "experiments", ["owner_id"])
    op.create_table(
        "experiment_versions",
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=True),
        sa.Column("app_version", sa.String(length=32), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_version_size_nonnegative"),
        sa.CheckConstraint("version_number > 0", name="ck_version_number_positive"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
        sa.UniqueConstraint(
            "experiment_id",
            "version_number",
            name="uq_experiment_versions_number",
        ),
    )
    op.create_index(
        "ix_experiment_versions_experiment_id",
        "experiment_versions",
        ["experiment_id"],
    )
    op.create_index(
        "ix_experiment_versions_sha256",
        "experiment_versions",
        ["sha256"],
    )


def downgrade():
    op.drop_index("ix_experiment_versions_sha256", table_name="experiment_versions")
    op.drop_index(
        "ix_experiment_versions_experiment_id",
        table_name="experiment_versions",
    )
    op.drop_table("experiment_versions")
    op.drop_index("ix_experiments_owner_id", table_name="experiments")
    op.drop_table("experiments")
    op.drop_table("users")
