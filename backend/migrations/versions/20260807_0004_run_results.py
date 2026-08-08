"""Add immutable Experiment runs and per-Block raw result artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "20260807_0004"
down_revision = "20260807_0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiment_runs",
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("participant_number", sa.Integer(), nullable=False),
        sa.Column("participant_age", sa.Integer(), nullable=False),
        sa.Column("participant_gender", sa.String(length=32), nullable=False),
        sa.Column("block_count", sa.Integer(), nullable=False),
        sa.Column("source_experiment_name", sa.String(length=200), nullable=False),
        sa.Column("source_experiment_id", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "participant_number > 0",
            name="ck_run_participant_number_positive",
        ),
        sa.CheckConstraint(
            "participant_age > 0",
            name="ck_run_participant_age_positive",
        ),
        sa.CheckConstraint("block_count > 0", name="ck_run_block_count_positive"),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["experiments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "experiment_id",
            "session_id",
            name="uq_experiment_runs_session",
        ),
    )
    op.create_index(
        "ix_experiment_runs_experiment_id",
        "experiment_runs",
        ["experiment_id"],
    )
    op.create_index(
        "ix_experiment_runs_session_id",
        "experiment_runs",
        ["session_id"],
    )

    op.create_table(
        "run_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("block_id", sa.Uuid(), nullable=True),
        sa.Column("block_index", sa.Integer(), nullable=False),
        sa.Column("block_count", sa.Integer(), nullable=False),
        sa.Column("block_name", sa.String(length=200), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("app_version", sa.String(length=32), nullable=False),
        sa.Column("result_timestamp", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint("block_index > 0", name="ck_result_block_index_positive"),
        sa.CheckConstraint("block_count > 0", name="ck_result_block_count_positive"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_result_size_nonnegative"),
        sa.ForeignKeyConstraint(
            ["block_id"], ["experiment_blocks.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["experiment_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
        sa.UniqueConstraint(
            "run_id",
            "block_index",
            name="uq_run_results_block_index",
        ),
    )
    op.create_index("ix_run_results_block_id", "run_results", ["block_id"])
    op.create_index("ix_run_results_run_id", "run_results", ["run_id"])
    op.create_index("ix_run_results_sha256", "run_results", ["sha256"])


def downgrade():
    op.drop_index("ix_run_results_sha256", table_name="run_results")
    op.drop_index("ix_run_results_run_id", table_name="run_results")
    op.drop_index("ix_run_results_block_id", table_name="run_results")
    op.drop_table("run_results")
    op.drop_index("ix_experiment_runs_session_id", table_name="experiment_runs")
    op.drop_index("ix_experiment_runs_experiment_id", table_name="experiment_runs")
    op.drop_table("experiment_runs")
