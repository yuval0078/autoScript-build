"""Durable rate limiting, session cleanup, and security audit helpers."""

import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..models import AccessToken
from ..security_models import LoginRateLimit, SecurityEvent
from ..security_settings import SecuritySettings


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value):
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def normalize_username(value):
    return str(value or "").strip().casefold()[:64]


def client_address_hash(address):
    normalized = str(address or "unknown").strip().casefold()
    return hashlib.sha256(f"address\0{normalized}".encode("utf-8")).hexdigest()


def _scope_key_hash(scope, value):
    return hashlib.sha256(f"{scope}\0{value}".encode("utf-8")).hexdigest()


def login_scope_keys(username, address_hash):
    return {
        "principal": _scope_key_hash("principal", normalize_username(username)),
        "address": _scope_key_hash("address", address_hash),
    }


def record_security_event(
    database: Session,
    event_type,
    outcome,
    *,
    actor_user_id=None,
    subject_user_id=None,
    username=None,
    request_id=None,
    address_hash=None,
    metadata=None,
):
    safe_metadata = json.dumps(
        metadata or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    event = SecurityEvent(
        id=uuid.uuid4(),
        event_type=event_type,
        outcome=outcome,
        actor_user_id=actor_user_id,
        subject_user_id=subject_user_id,
        username=normalize_username(username) or None,
        request_id=request_id,
        client_address_hash=address_hash,
        metadata_json=safe_metadata,
    )
    database.add(event)
    return event


def security_event_metadata(event):
    try:
        value = json.loads(event.metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def cleanup_security_state(database: Session, settings: SecuritySettings, *, now=None):
    """Remove unusable sessions and old, inactive rate-limit buckets."""
    now = now or utc_now()
    token_result = database.execute(
        delete(AccessToken).where(
            or_(
                AccessToken.expires_at <= now,
                AccessToken.revoked_at.is_not(None),
            )
        ).execution_options(synchronize_session=False)
    )
    cutoff = now - timedelta(days=settings.security_state_retention_days)
    bucket_result = database.execute(
        delete(LoginRateLimit).where(
            LoginRateLimit.updated_at < cutoff,
            or_(
                LoginRateLimit.blocked_until.is_(None),
                LoginRateLimit.blocked_until <= now,
            ),
        ).execution_options(synchronize_session=False)
    )
    return max(0, token_result.rowcount or 0), max(0, bucket_result.rowcount or 0)


def login_retry_after(database, scope_keys, *, now=None):
    now = now or utc_now()
    buckets = database.scalars(
        select(LoginRateLimit)
        .where(
            LoginRateLimit.scope.in_(scope_keys),
            LoginRateLimit.key_hash.in_(scope_keys.values()),
        )
        .with_for_update()
    ).all()
    seconds = 0
    for bucket in buckets:
        if bucket.key_hash != scope_keys.get(bucket.scope):
            continue
        blocked_until = as_utc(bucket.blocked_until)
        if blocked_until is not None and blocked_until > now:
            seconds = max(seconds, math.ceil((blocked_until - now).total_seconds()))
    return seconds


def register_login_failure(database, scope_keys, settings, *, now=None):
    now = now or utc_now()
    thresholds = {
        "principal": settings.login_rate_limit_attempts,
        "address": (
            settings.login_rate_limit_attempts
            * settings.login_rate_limit_address_multiplier
        ),
    }
    window = timedelta(seconds=settings.login_rate_limit_window_seconds)
    lockout = timedelta(seconds=settings.login_rate_limit_lockout_seconds)
    retry_after = 0
    for scope, key_hash in scope_keys.items():
        bucket = database.scalar(
            select(LoginRateLimit)
            .where(
                LoginRateLimit.scope == scope,
                LoginRateLimit.key_hash == key_hash,
            )
            .with_for_update()
        )
        if bucket is None:
            bucket = LoginRateLimit(
                id=uuid.uuid4(),
                scope=scope,
                key_hash=key_hash,
                failure_count=0,
                window_started_at=now,
                updated_at=now,
            )
            database.add(bucket)
        window_started_at = as_utc(bucket.window_started_at)
        if window_started_at is None or now - window_started_at >= window:
            bucket.failure_count = 0
            bucket.window_started_at = now
            bucket.blocked_until = None
        bucket.failure_count += 1
        bucket.updated_at = now
        if bucket.failure_count >= thresholds[scope]:
            bucket.blocked_until = now + lockout
            retry_after = max(retry_after, settings.login_rate_limit_lockout_seconds)
    return retry_after


def reset_principal_login_failures(database, scope_keys):
    database.execute(
        delete(LoginRateLimit).where(
            LoginRateLimit.scope == "principal",
            LoginRateLimit.key_hash == scope_keys["principal"],
        ).execution_options(synchronize_session=False)
    )
