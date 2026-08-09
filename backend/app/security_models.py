"""Durable security state, isolated from experiment/result domain models."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class SecurityEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Append-only authentication and administrator audit event."""

    __tablename__ = "security_events"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'denied', 'error')",
            name="ck_security_event_outcome",
        ),
        Index("ix_security_events_event_created", "event_type", "created_at"),
    )

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    client_address_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[str | None] = mapped_column(Text)


class LoginRateLimit(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Durable failure window shared by every API process."""

    __tablename__ = "login_rate_limits"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", name="uq_login_rate_limit_scope_key"),
        CheckConstraint(
            "scope IN ('principal', 'address')",
            name="ck_login_rate_limit_scope",
        ),
        CheckConstraint(
            "failure_count >= 0", name="ck_login_rate_limit_failure_nonnegative"
        ),
    )

    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    blocked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
