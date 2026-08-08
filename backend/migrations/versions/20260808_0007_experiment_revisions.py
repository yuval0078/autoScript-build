"""Add immutable Experiment revisions and pin Runs to a revision."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0007"
down_revision = "20260808_0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiment_revisions",
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revision_number > 0", name="ck_experiment_revision_positive"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("experiment_id", "revision_number", name="uq_experiment_revision_number"),
    )
    op.create_index("ix_experiment_revisions_experiment_id", "experiment_revisions", ["experiment_id"])
    op.create_table(
        "experiment_revision_blocks",
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("source_block_id", sa.Uuid(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("same_page_as_previous", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=True),
        sa.Column("app_version", sa.String(length=32), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_revision_block_position_nonnegative"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_revision_block_size_nonnegative"),
        sa.ForeignKeyConstraint(["revision_id"], ["experiment_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "position", name="uq_revision_blocks_position"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_experiment_revision_blocks_revision_id", "experiment_revision_blocks", ["revision_id"])
    op.create_index("ix_experiment_revision_blocks_sha256", "experiment_revision_blocks", ["sha256"])
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.add_column(sa.Column("revision_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key("fk_experiment_runs_revision_id", "experiment_revisions", ["revision_id"], ["id"], ondelete="RESTRICT")
        batch_op.create_index("ix_experiment_runs_revision_id", ["revision_id"])


def downgrade():
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.drop_index("ix_experiment_runs_revision_id")
        batch_op.drop_constraint("fk_experiment_runs_revision_id", type_="foreignkey")
        batch_op.drop_column("revision_id")
    op.drop_index("ix_experiment_revision_blocks_sha256", table_name="experiment_revision_blocks")
    op.drop_index("ix_experiment_revision_blocks_revision_id", table_name="experiment_revision_blocks")
    op.drop_table("experiment_revision_blocks")
    op.drop_index("ix_experiment_revisions_experiment_id", table_name="experiment_revisions")
    op.drop_table("experiment_revisions")
