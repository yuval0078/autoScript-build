"""Add run completeness metadata, analysis status, and Analyzer artifacts."""

from alembic import op
import sqlalchemy as sa


revision = "20260807_0005"
down_revision = "20260807_0004"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.add_column(
            sa.Column("analysis_completed", sa.Boolean(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "analysis_updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
    with op.batch_alter_table("run_results") as batch_op:
        batch_op.add_column(
            sa.Column(
                "block_completed",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "experiment_completed",
                sa.Boolean(),
                server_default=sa.text("true"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "completed_word_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "expected_word_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_result_completed_words_nonnegative",
            "completed_word_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_result_expected_words_nonnegative",
            "expected_word_count >= 0",
        )

    op.create_table(
        "run_artifacts",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint(
            "kind IN ('analysis_csv', 'trainable_json')",
            name="ck_run_artifact_kind",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_run_artifact_size_nonnegative",
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
            "kind",
            "sha256",
            name="uq_run_artifacts_exact_content",
        ),
    )
    op.create_index("ix_run_artifacts_run_id", "run_artifacts", ["run_id"])
    op.create_index("ix_run_artifacts_kind", "run_artifacts", ["kind"])
    op.create_index("ix_run_artifacts_sha256", "run_artifacts", ["sha256"])


def downgrade():
    op.drop_index("ix_run_artifacts_sha256", table_name="run_artifacts")
    op.drop_index("ix_run_artifacts_kind", table_name="run_artifacts")
    op.drop_index("ix_run_artifacts_run_id", table_name="run_artifacts")
    op.drop_table("run_artifacts")
    with op.batch_alter_table("run_results") as batch_op:
        batch_op.drop_constraint(
            "ck_result_expected_words_nonnegative", type_="check"
        )
        batch_op.drop_constraint(
            "ck_result_completed_words_nonnegative", type_="check"
        )
        batch_op.drop_column("expected_word_count")
        batch_op.drop_column("completed_word_count")
        batch_op.drop_column("experiment_completed")
        batch_op.drop_column("block_completed")
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.drop_column("analysis_updated_at")
        batch_op.drop_column("analysis_completed")
