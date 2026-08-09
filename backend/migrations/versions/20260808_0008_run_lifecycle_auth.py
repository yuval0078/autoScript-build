"""Add explicit Run lifecycle and revocable access tokens."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0008"
down_revision = "20260808_0007"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(length=32), server_default="running", nullable=False))
        batch_op.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_experiment_run_status",
            "status IN ('created', 'running', 'completed', 'incomplete', 'failed', 'cancelled')",
        )
    op.create_table(
        "access_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_access_tokens_user_id", "access_tokens", ["user_id"])
    op.create_index("ix_access_tokens_token_hash", "access_tokens", ["token_hash"], unique=True)
    op.create_index("ix_access_tokens_expires_at", "access_tokens", ["expires_at"])


def downgrade():
    op.drop_index("ix_access_tokens_expires_at", table_name="access_tokens")
    op.drop_index("ix_access_tokens_token_hash", table_name="access_tokens")
    op.drop_index("ix_access_tokens_user_id", table_name="access_tokens")
    op.drop_table("access_tokens")
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.drop_constraint("ck_experiment_run_status", type_="check")
        batch_op.drop_column("finalized_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("status")
