"""Allow versioned Analyzer edit-state artifacts."""

from alembic import op


revision = "20260808_0006"
down_revision = "20260807_0005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("run_artifacts") as batch_op:
        batch_op.drop_constraint("ck_run_artifact_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_run_artifact_kind",
            "kind IN ('analysis_csv', 'trainable_json', 'analysis_state')",
        )


def downgrade():
    with op.batch_alter_table("run_artifacts") as batch_op:
        batch_op.drop_constraint("ck_run_artifact_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_run_artifact_kind",
            "kind IN ('analysis_csv', 'trainable_json')",
        )
