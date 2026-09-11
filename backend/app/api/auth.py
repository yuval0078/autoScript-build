import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_user, require_admin
from ..models import AccessToken, DeviceToken, User
from ..schemas.auth import (
    DeviceTokenCreate,
    DeviceTokenIssuedResponse,
    DeviceTokenResponse,
    LoginRequest,
    TokenResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)
from ..schemas.security import SecurityEventResponse
from ..security_models import SecurityEvent
from ..security_settings import get_security_settings
from ..services.auth import (
    hash_password,
    hash_token,
    issue_device_token,
    issue_token,
    verify_password,
)
from ..services.security import (
    cleanup_security_state,
    client_address_hash,
    login_retry_after,
    login_scope_keys,
    record_security_event,
    register_login_failure,
    reset_principal_login_failures,
    security_event_metadata,
    utc_now,
)


router = APIRouter(prefix="/api/v1", tags=["authentication"])
MAX_ACTIVE_DEVICE_TOKENS = 10

_DUMMY_PASSWORD_HASH = (
    "pbkdf2_sha256$600000$ujEHW9ceS_1zl9ScxwywqA==$"
    "u5guApZogxmGhUXm2REuOt0bPnw3q5kjEZ8hXs_xnbs="
)


def _user_response(user):
    return UserResponse(
        id=user.id, username=user.username, role=user.role,
        is_active=user.is_active, created_at=user.created_at,
    )


def _device_token_response(token):
    return DeviceTokenResponse(
        id=token.id,
        label=token.label,
        created_at=token.created_at,
        revoked_at=token.revoked_at,
    )


def _request_security_context(request):
    address = request.client.host if request.client is not None else "unknown"
    return (
        getattr(request.state, "request_id", None),
        client_address_hash(address),
    )


def _record_cleanup_event(database, request_id, address_hash, counts):
    token_count, bucket_count = counts
    if token_count or bucket_count:
        record_security_event(
            database,
            "security_state_cleanup",
            "success",
            request_id=request_id,
            address_hash=address_hash,
            metadata={
                "access_tokens_removed": token_count,
                "rate_limit_buckets_removed": bucket_count,
            },
        )


def _security_event_response(event):
    return SecurityEventResponse(
        id=event.id,
        event_type=event.event_type,
        outcome=event.outcome,
        actor_user_id=event.actor_user_id,
        subject_user_id=event.subject_user_id,
        username=event.username,
        request_id=event.request_id,
        metadata=security_event_metadata(event),
        created_at=event.created_at,
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    database: Session = Depends(get_db),
):
    security_settings = get_security_settings()
    request_id, address_hash = _request_security_context(request)
    scope_keys = login_scope_keys(payload.username, address_hash)
    cleanup_counts = cleanup_security_state(database, security_settings)
    retry_after = login_retry_after(database, scope_keys)
    if retry_after:
        _record_cleanup_event(database, request_id, address_hash, cleanup_counts)
        record_security_event(
            database,
            "login_rate_limited",
            "denied",
            username=payload.username,
            request_id=request_id,
            address_hash=address_hash,
            metadata={"retry_after_seconds": retry_after},
        )
        database.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    user = database.scalar(select(User).where(User.username == payload.username))
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, password_hash)
    if user is None or not user.is_active or not password_valid:
        try:
            retry_after = register_login_failure(
                database, scope_keys, security_settings
            )
            _record_cleanup_event(database, request_id, address_hash, cleanup_counts)
            record_security_event(
                database,
                "login_rate_limited" if retry_after else "login_failure",
                "denied",
                subject_user_id=user.id if user is not None else None,
                username=payload.username,
                request_id=request_id,
                address_hash=address_hash,
                metadata={
                    "reason": "invalid_credentials_or_inactive",
                    **(
                        {"retry_after_seconds": retry_after}
                        if retry_after
                        else {}
                    ),
                },
            )
            database.commit()
        except IntegrityError:
            # Two workers may create the same durable bucket together. The
            # loser retries against the winner's committed row.
            database.rollback()
            cleanup_counts = cleanup_security_state(database, security_settings)
            retry_after = register_login_failure(
                database, scope_keys, security_settings
            )
            _record_cleanup_event(database, request_id, address_hash, cleanup_counts)
            record_security_event(
                database,
                "login_rate_limited" if retry_after else "login_failure",
                "denied",
                subject_user_id=user.id if user is not None else None,
                username=payload.username,
                request_id=request_id,
                address_hash=address_hash,
                metadata={"reason": "invalid_credentials_or_inactive"},
            )
            database.commit()
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    settings = get_settings()
    raw_token = issue_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.access_token_ttl_hours)
    reset_principal_login_failures(database, scope_keys)
    database.add(AccessToken(
        id=uuid.uuid4(), user_id=user.id, token_hash=hash_token(raw_token), expires_at=expires_at
    ))
    _record_cleanup_event(database, request_id, address_hash, cleanup_counts)
    record_security_event(
        database,
        "login_success",
        "success",
        actor_user_id=user.id,
        subject_user_id=user.id,
        username=user.username,
        request_id=request_id,
        address_hash=address_hash,
    )
    database.commit()
    return TokenResponse(access_token=raw_token, expires_at=expires_at, user=_user_response(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    authorization: str | None = Header(default=None),
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    request_id, address_hash = _request_security_context(request)
    if authorization and authorization.lower().startswith("bearer "):
        raw_token = authorization.split(None, 1)[1]
        token_model = DeviceToken if raw_token.startswith("asd_") else AccessToken
        token = database.scalar(
            select(token_model).where(token_model.token_hash == hash_token(raw_token))
        )
        if token is not None:
            token.revoked_at = utc_now()
    record_security_event(
        database,
        "logout",
        "success",
        actor_user_id=actor.id,
        subject_user_id=actor.id,
        username=actor.username,
        request_id=request_id,
        address_hash=address_hash,
    )
    cleanup_counts = cleanup_security_state(database, get_security_settings())
    _record_cleanup_event(database, request_id, address_hash, cleanup_counts)
    database.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=UserResponse)
def me(actor: User = Depends(get_current_user)):
    return _user_response(actor)


@router.post(
    "/auth/device-tokens",
    response_model=DeviceTokenIssuedResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_device_token(
    payload: DeviceTokenCreate,
    request: Request,
    response: Response,
    database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    request_id, address_hash = _request_security_context(request)
    if not verify_password(payload.password, actor.password_hash):
        record_security_event(
            database,
            "device_token_creation",
            "denied",
            actor_user_id=actor.id,
            subject_user_id=actor.id,
            username=actor.username,
            request_id=request_id,
            address_hash=address_hash,
            metadata={"reason": "step_up_authentication_failed"},
        )
        database.commit()
        raise HTTPException(status_code=401, detail="Current password is incorrect.")

    active_tokens = database.scalars(
        select(DeviceToken).where(
            DeviceToken.user_id == actor.id,
            DeviceToken.revoked_at.is_(None),
        )
    ).all()
    if len(active_tokens) >= MAX_ACTIVE_DEVICE_TOKENS:
        raise HTTPException(
            status_code=409,
            detail="Revoke an existing device token before creating another.",
        )

    raw_token = issue_device_token()
    token = DeviceToken(
        id=uuid.uuid4(),
        user_id=actor.id,
        label=payload.label,
        token_hash=hash_token(raw_token),
    )
    database.add(token)
    database.flush()
    record_security_event(
        database,
        "device_token_created",
        "success",
        actor_user_id=actor.id,
        subject_user_id=actor.id,
        username=actor.username,
        request_id=request_id,
        address_hash=address_hash,
        metadata={"device_token_id": str(token.id), "label": token.label},
    )
    database.commit()
    database.refresh(token)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return DeviceTokenIssuedResponse(
        **_device_token_response(token).model_dump(),
        device_token=raw_token,
    )


@router.get("/auth/device-tokens", response_model=list[DeviceTokenResponse])
def list_device_tokens(
    database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    tokens = database.scalars(
        select(DeviceToken)
        .where(DeviceToken.user_id == actor.id)
        .order_by(DeviceToken.created_at.desc(), DeviceToken.id.desc())
    ).all()
    return [_device_token_response(token) for token in tokens]


@router.delete("/auth/device-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_device_token(
    token_id: uuid.UUID,
    request: Request,
    database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    token = database.scalar(
        select(DeviceToken).where(
            DeviceToken.id == token_id,
            DeviceToken.user_id == actor.id,
        )
    )
    if token is None:
        raise HTTPException(status_code=404, detail="Device token was not found.")
    if token.revoked_at is None:
        token.revoked_at = utc_now()
        request_id, address_hash = _request_security_context(request)
        record_security_event(
            database,
            "device_token_revoked",
            "success",
            actor_user_id=actor.id,
            subject_user_id=actor.id,
            username=actor.username,
            request_id=request_id,
            address_hash=address_hash,
            metadata={"device_token_id": str(token.id), "label": token.label},
        )
        database.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", response_model=list[UserResponse])
def list_users(database: Session = Depends(get_db), actor: User = Depends(require_admin)):
    del actor
    return [_user_response(user) for user in database.scalars(select(User).order_by(User.username)).all()]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    request: Request,
    database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    user = User(username=payload.username, password_hash=hash_password(payload.password), role=payload.role, is_active=True)
    database.add(user)
    try:
        database.flush()
        request_id, address_hash = _request_security_context(request)
        record_security_event(
            database,
            "user_created",
            "success",
            actor_user_id=actor.id,
            subject_user_id=user.id,
            username=user.username,
            request_id=request_id,
            address_hash=address_hash,
            metadata={"role": user.role},
        )
        database.commit()
    except IntegrityError as exc:
        database.rollback()
        raise HTTPException(status_code=409, detail="Username already exists.") from exc
    database.refresh(user)
    return _user_response(user)


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: uuid.UUID, payload: UserUpdate, request: Request,
    database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    user = database.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User was not found.")
    if payload.username is not None:
        user.username = payload.username
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
    if payload.role is not None:
        if user.id == actor.id and payload.role != "admin":
            raise HTTPException(status_code=409, detail="An administrator cannot remove their own admin role.")
        user.role = payload.role
    if payload.is_active is not None:
        if user.id == actor.id and not payload.is_active:
            raise HTTPException(status_code=409, detail="An administrator cannot deactivate their own account.")
        user.is_active = payload.is_active
    if payload.password is not None or payload.is_active is False:
        database.execute(
            update(AccessToken)
            .where(AccessToken.user_id == user.id, AccessToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        database.execute(
            update(DeviceToken)
            .where(DeviceToken.user_id == user.id, DeviceToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
    changed_fields = sorted(payload.model_fields_set)
    request_id, address_hash = _request_security_context(request)
    record_security_event(
        database,
        "user_updated",
        "success",
        actor_user_id=actor.id,
        subject_user_id=user.id,
        username=user.username,
        request_id=request_id,
        address_hash=address_hash,
        metadata={"changed_fields": changed_fields},
    )
    cleanup_counts = cleanup_security_state(database, get_security_settings())
    _record_cleanup_event(database, request_id, address_hash, cleanup_counts)
    try:
        database.commit()
    except IntegrityError as exc:
        database.rollback()
        raise HTTPException(status_code=409, detail="Username already exists.") from exc
    return _user_response(user)


@router.get("/security/events", response_model=list[SecurityEventResponse])
def list_security_events(
    limit: int = Query(default=100, ge=1, le=500),
    event_type: str | None = Query(default=None, min_length=1, max_length=64),
    database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    del actor
    statement = select(SecurityEvent)
    if event_type is not None:
        statement = statement.where(SecurityEvent.event_type == event_type)
    events = database.scalars(
        statement.order_by(SecurityEvent.created_at.desc(), SecurityEvent.id.desc())
        .limit(limit)
    ).all()
    return [_security_event_response(event) for event in events]
