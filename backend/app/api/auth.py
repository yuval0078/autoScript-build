import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..dependencies import get_current_user, require_admin
from ..models import AccessToken, User
from ..schemas.auth import LoginRequest, TokenResponse, UserCreate, UserResponse, UserUpdate
from ..services.auth import hash_password, hash_token, issue_token, verify_password


router = APIRouter(prefix="/api/v1", tags=["authentication"])


def _user_response(user):
    return UserResponse(
        id=user.id, username=user.username, role=user.role,
        is_active=user.is_active, created_at=user.created_at,
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, database: Session = Depends(get_db)):
    user = database.scalar(select(User).where(User.username == payload.username))
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    settings = get_settings()
    raw_token = issue_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.access_token_ttl_hours)
    database.add(AccessToken(
        id=uuid.uuid4(), user_id=user.id, token_hash=hash_token(raw_token), expires_at=expires_at
    ))
    database.commit()
    return TokenResponse(access_token=raw_token, expires_at=expires_at, user=_user_response(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    authorization: str | None = Header(default=None),
    database: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    del actor
    if authorization and authorization.lower().startswith("bearer "):
        token = database.scalar(select(AccessToken).where(
            AccessToken.token_hash == hash_token(authorization.split(None, 1)[1])
        ))
        if token is not None:
            token.revoked_at = datetime.now(timezone.utc)
            database.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/auth/me", response_model=UserResponse)
def me(actor: User = Depends(get_current_user)):
    return _user_response(actor)


@router.get("/users", response_model=list[UserResponse])
def list_users(database: Session = Depends(get_db), actor: User = Depends(require_admin)):
    del actor
    return [_user_response(user) for user in database.scalars(select(User).order_by(User.username)).all()]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, database: Session = Depends(get_db), actor: User = Depends(require_admin)):
    del actor
    user = User(username=payload.username, password_hash=hash_password(payload.password), role=payload.role, is_active=True)
    database.add(user)
    try:
        database.commit()
    except IntegrityError as exc:
        database.rollback()
        raise HTTPException(status_code=409, detail="Username already exists.") from exc
    database.refresh(user)
    return _user_response(user)


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: uuid.UUID, payload: UserUpdate, database: Session = Depends(get_db),
    actor: User = Depends(require_admin),
):
    user = database.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User was not found.")
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
    database.commit()
    return _user_response(user)
