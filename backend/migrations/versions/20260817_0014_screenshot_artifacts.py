"""Allow immutable screenshot archives on participant Runs.

Revision ID: 20260817_0014
Revises: 20260808_0013
"""

from alembic import op


revision = "20260817_0014"
down_revision = "20260808_0013"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("run_artifacts") as batch_op:
        batch_op.drop_constraint("ck_run_artifact_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_run_artifact_kind",
            "kind IN ('analysis_csv', 'trainable_json', 'analysis_state', 'screenshots_zip')",
        )


def downgrade():
    with op.batch_alter_table("run_artifacts") as batch_op:
        batch_op.drop_constraint("ck_run_artifact_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_run_artifact_kind",
            "kind IN ('analysis_csv', 'trainable_json', 'analysis_state')",
        )
