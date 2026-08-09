"""Add transactional, revisioned Analyzer state and finalization metadata."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0012"
down_revision = "20260808_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "run_analysis_revisions",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "finalized",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("completed", sa.Boolean(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision_number > 0", name="ck_run_analysis_revision_positive"
        ),
        sa.CheckConstraint(
            "(finalized = false AND completed IS NULL AND finalized_at IS NULL) OR "
            "(finalized = true AND completed IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_run_analysis_finalization_fields",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["experiment_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "revision_number",
            name="uq_run_analysis_revision_number",
        ),
    )
    op.create_index(
        "ix_run_analysis_revisions_run_id", "run_analysis_revisions", ["run_id"]
    )
    op.create_index(
        "ix_run_analysis_revisions_source_fingerprint",
        "run_analysis_revisions",
        ["source_fingerprint"],
    )

    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.add_column(
            sa.Column("current_analysis_revision_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_experiment_runs_current_analysis_revision_id",
            "run_analysis_revisions",
            ["current_analysis_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_experiment_runs_current_analysis_revision_id",
            ["current_analysis_revision_id"],
        )

    with op.batch_alter_table("run_artifacts") as batch_op:
        batch_op.drop_constraint("uq_run_artifacts_exact_content", type_="unique")
        batch_op.add_column(
            sa.Column("analysis_revision_id", sa.Uuid(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_run_artifacts_analysis_revision_id",
            "run_analysis_revisions",
            ["analysis_revision_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_run_artifacts_analysis_revision_id", ["analysis_revision_id"]
        )
        batch_op.create_unique_constraint(
            "uq_run_artifacts_analysis_revision_kind",
            ["analysis_revision_id", "kind"],
        )

    op.create_table(
        "run_analysis_operations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("operation_kind", sa.String(length=16), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=True),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation_kind IN ('state', 'finalize')",
            name="ck_run_analysis_operation_kind",
        ),
        sa.CheckConstraint(
            "revision_number > 0", name="ck_run_analysis_operation_revision_positive"
        ),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["run_analysis_revisions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["experiment_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "request_id", name="uq_run_analysis_operation_request"
        ),
    )
    op.create_index(
        "ix_run_analysis_operations_run_id", "run_analysis_operations", ["run_id"]
    )

    op.create_table(
        "object_deletion_tasks",
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column(
            "attempt_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_object_deletion_attempt_nonnegative"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )


def downgrade():
    op.drop_table("object_deletion_tasks")
    op.drop_index(
        "ix_run_analysis_operations_run_id", table_name="run_analysis_operations"
    )
    op.drop_table("run_analysis_operations")
    with op.batch_alter_table("run_artifacts") as batch_op:
        batch_op.drop_constraint(
            "uq_run_artifacts_analysis_revision_kind", type_="unique"
        )
        batch_op.drop_index("ix_run_artifacts_analysis_revision_id")
        batch_op.drop_constraint(
            "fk_run_artifacts_analysis_revision_id", type_="foreignkey"
        )
        batch_op.drop_column("analysis_revision_id")
        batch_op.create_unique_constraint(
            "uq_run_artifacts_exact_content", ["run_id", "kind", "sha256"]
        )
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.drop_index("ix_experiment_runs_current_analysis_revision_id")
        batch_op.drop_constraint(
            "fk_experiment_runs_current_analysis_revision_id", type_="foreignkey"
        )
        batch_op.drop_column("current_analysis_revision_id")
    op.drop_index(
        "ix_run_analysis_revisions_source_fingerprint",
        table_name="run_analysis_revisions",
    )
    op.drop_index(
        "ix_run_analysis_revisions_run_id", table_name="run_analysis_revisions"
    )
    op.drop_table("run_analysis_revisions")
