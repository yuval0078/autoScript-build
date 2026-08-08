"""Backfill lifecycle status for Runs created before explicit lifecycle support."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0009"
down_revision = "20260808_0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(sa.text("""
        UPDATE experiment_runs
        SET status = CASE
            WHEN (SELECT COUNT(*) FROM run_results WHERE run_results.run_id = experiment_runs.id) = block_count
             AND NOT EXISTS (
                 SELECT 1 FROM run_results
                 WHERE run_results.run_id = experiment_runs.id
                   AND (block_completed = false OR experiment_completed = false)
             )
             AND COALESCE((
                 SELECT SUM(completed_word_count) FROM run_results
                 WHERE run_results.run_id = experiment_runs.id
             ), 0) = COALESCE((
                 SELECT SUM(expected_word_count) FROM run_results
                 WHERE run_results.run_id = experiment_runs.id
             ), 0)
            THEN 'completed' ELSE 'incomplete' END,
            started_at = created_at,
            finalized_at = created_at
    """))
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.alter_column(
            "status", existing_type=sa.String(length=32),
            server_default="created", existing_nullable=False,
        )


def downgrade():
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.alter_column(
            "status", existing_type=sa.String(length=32),
            server_default="running", existing_nullable=False,
        )
