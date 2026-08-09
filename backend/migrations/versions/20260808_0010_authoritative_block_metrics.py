"""Store authoritative prompt and grid metrics on Blocks and revisions."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0010"
down_revision = "20260808_0009"
branch_labels = None
depends_on = None


def _add_metrics(table_name, constraint_prefix):
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.add_column(sa.Column("expected_word_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("grid_rows", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("grid_cols", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            f"ck_{constraint_prefix}_expected_words_nonnegative",
            "expected_word_count IS NULL OR expected_word_count >= 0",
        )
        batch_op.create_check_constraint(
            f"ck_{constraint_prefix}_grid_rows_positive",
            "grid_rows IS NULL OR grid_rows > 0",
        )
        batch_op.create_check_constraint(
            f"ck_{constraint_prefix}_grid_cols_positive",
            "grid_cols IS NULL OR grid_cols > 0",
        )


def _drop_metrics(table_name, constraint_prefix):
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_constraint(
            f"ck_{constraint_prefix}_grid_cols_positive", type_="check"
        )
        batch_op.drop_constraint(
            f"ck_{constraint_prefix}_grid_rows_positive", type_="check"
        )
        batch_op.drop_constraint(
            f"ck_{constraint_prefix}_expected_words_nonnegative", type_="check"
        )
        batch_op.drop_column("grid_cols")
        batch_op.drop_column("grid_rows")
        batch_op.drop_column("expected_word_count")


def upgrade():
    # Existing rows remain NULL because their immutable ZIP contents live in
    # object storage and cannot be safely inspected inside a database migration.
    # Every newly uploaded Block and every new revision snapshot is populated.
    _add_metrics("experiment_blocks", "block")
    _add_metrics("experiment_revision_blocks", "revision_block")


def downgrade():
    _drop_metrics("experiment_revision_blocks", "revision_block")
    _drop_metrics("experiment_blocks", "block")
