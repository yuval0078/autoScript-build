"""Add durable API security audit events and login rate-limit state."""

from alembic import op
import sqlalchemy as sa


revision = "20260808_0013"
down_revision = "20260808_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "security_events",
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("subject_user_id", sa.Uuid(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("client_address_hash", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'error')",
            name="ck_security_event_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_security_events_actor_user_id", "security_events", ["actor_user_id"]
    )
    op.create_index(
        "ix_security_events_subject_user_id",
        "security_events",
        ["subject_user_id"],
    )
    op.create_index("ix_security_events_username", "security_events", ["username"])
    op.create_index(
        "ix_security_events_request_id", "security_events", ["request_id"]
    )
    op.create_index(
        "ix_security_events_event_created",
        "security_events",
        ["event_type", "created_at"],
    )

    op.create_table(
        "login_rate_limits",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('principal', 'address')",
            name="ck_login_rate_limit_scope",
        ),
        sa.CheckConstraint(
            "failure_count >= 0",
            name="ck_login_rate_limit_failure_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope", "key_hash", name="uq_login_rate_limit_scope_key"
        ),
    )
    op.create_index(
        "ix_login_rate_limits_key_hash", "login_rate_limits", ["key_hash"]
    )
    op.create_index(
        "ix_login_rate_limits_blocked_until",
        "login_rate_limits",
        ["blocked_until"],
    )


def downgrade():
    op.drop_index(
        "ix_login_rate_limits_blocked_until", table_name="login_rate_limits"
    )
    op.drop_index("ix_login_rate_limits_key_hash", table_name="login_rate_limits")
    op.drop_table("login_rate_limits")
    op.drop_index("ix_security_events_event_created", table_name="security_events")
    op.drop_index("ix_security_events_request_id", table_name="security_events")
    op.drop_index("ix_security_events_username", table_name="security_events")
    op.drop_index(
        "ix_security_events_subject_user_id", table_name="security_events"
    )
    op.drop_index("ix_security_events_actor_user_id", table_name="security_events")
    op.drop_table("security_events")
