"""Add revocable long-lived device tokens.

Revision ID: 20260911_0016
Revises: 20260910_0015
"""

import sqlalchemy as sa
from alembic import op


revision = "20260911_0016"
down_revision = "20260910_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_device_tokens_user_id", "device_tokens", ["user_id"], unique=False
    )
    op.create_index(
        "ix_device_tokens_token_hash", "device_tokens", ["token_hash"], unique=True
    )


def downgrade():
    op.drop_index("ix_device_tokens_token_hash", table_name="device_tokens")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_table("device_tokens")
