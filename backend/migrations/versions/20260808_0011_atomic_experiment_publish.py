"""Add atomic Experiment publishing, staging, and current revision pointers."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0011"
down_revision = "20260808_0010"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("experiments") as batch_op:
        batch_op.add_column(sa.Column("current_revision_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_experiments_current_revision_id",
            "experiment_revisions",
            ["current_revision_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_experiments_current_revision_id", ["current_revision_id"]
        )

    op.execute(sa.text("""
        UPDATE experiments
        SET current_revision_id = (
            SELECT experiment_revisions.id
            FROM experiment_revisions
            WHERE experiment_revisions.experiment_id = experiments.id
            ORDER BY experiment_revisions.revision_number DESC
            LIMIT 1
        )
    """))

    op.create_table(
        "staged_block_assets",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=True),
        sa.Column("app_version", sa.String(length=32), nullable=True),
        sa.Column("expected_word_count", sa.Integer(), nullable=False),
        sa.Column("grid_rows", sa.Integer(), nullable=False),
        sa.Column("grid_cols", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expected_word_count >= 0", name="ck_staged_block_expected_words_nonnegative"
        ),
        sa.CheckConstraint("grid_cols > 0", name="ck_staged_block_grid_cols_positive"),
        sa.CheckConstraint("grid_rows > 0", name="ck_staged_block_grid_rows_positive"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_staged_block_size_nonnegative"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "request_id", name="uq_staged_block_owner_request"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_staged_block_assets_owner_id", "staged_block_assets", ["owner_id"])
    op.create_index("ix_staged_block_assets_sha256", "staged_block_assets", ["sha256"])
    op.create_index("ix_staged_block_assets_expires_at", "staged_block_assets", ["expires_at"])

    op.create_table(
        "experiment_publish_operations",
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("experiment_id", sa.Uuid(), nullable=False),
        sa.Column("revision_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["experiment_id"], ["experiments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["revision_id"], ["experiment_revisions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "request_id", name="uq_publish_operation_owner_request"),
    )
    op.create_index(
        "ix_experiment_publish_operations_owner_id",
        "experiment_publish_operations",
        ["owner_id"],
    )
    op.create_index(
        "ix_experiment_publish_operations_experiment_id",
        "experiment_publish_operations",
        ["experiment_id"],
    )


def downgrade():
    op.drop_index(
        "ix_experiment_publish_operations_experiment_id",
        table_name="experiment_publish_operations",
    )
    op.drop_index(
        "ix_experiment_publish_operations_owner_id",
        table_name="experiment_publish_operations",
    )
    op.drop_table("experiment_publish_operations")
    op.drop_index("ix_staged_block_assets_expires_at", table_name="staged_block_assets")
    op.drop_index("ix_staged_block_assets_sha256", table_name="staged_block_assets")
    op.drop_index("ix_staged_block_assets_owner_id", table_name="staged_block_assets")
    op.drop_table("staged_block_assets")
    with op.batch_alter_table("experiments") as batch_op:
        batch_op.drop_index("ix_experiments_current_revision_id")
        batch_op.drop_constraint("fk_experiments_current_revision_id", type_="foreignkey")
        batch_op.drop_column("current_revision_id")
