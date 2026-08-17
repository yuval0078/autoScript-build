from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import AccessToken, User
from .services.auth import hash_token


bearer_auth = HTTPBearer(
    auto_error=False,
    bearerFormat="opaque access token",
    description=(
        "AutoScript access token returned by POST /api/v1/auth/login. "
        "Local development mode may use its configured local actor without a token."
    ),
)

ADMIN_ROLE = "admin"
RESEARCHER_ROLE = "researcher"
OPERATOR_ROLE = "operator"
VALID_ROLES = frozenset({ADMIN_ROLE, RESEARCHER_ROLE, OPERATOR_ROLE})
EDITOR_ROLES = frozenset({ADMIN_ROLE, RESEARCHER_ROLE})


def get_current_user(
    authorization: str | None = Header(default=None, include_in_schema=False),
    _documented_bearer: HTTPAuthorizationCredentials | None = Security(bearer_auth),
    database: Session = Depends(get_db),
):
    # Keep explicit parsing so malformed Authorization headers behave identically
    # in local and token modes. The Security dependency documents Bearer auth.
    del _documented_bearer
    settings = get_settings()
    if authorization:
        scheme, _, raw_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not raw_token:
            raise HTTPException(status_code=401, detail="Invalid Authorization header.")
        token = database.scalar(
            select(AccessToken).where(
                AccessToken.token_hash == hash_token(raw_token),
                AccessToken.revoked_at.is_(None),
            )
        )
        expires_at = token.expires_at if token is not None else None
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if token is None or expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Access token is invalid or expired.")
        user = database.get(User, token.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="User account is inactive.")
        if user.role not in VALID_ROLES:
            raise HTTPException(status_code=403, detail="User role is not supported.")
        return user
    if settings.auth_mode == "token":
        raise HTTPException(
            status_code=401,
            detail="Authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = database.scalar(
        select(User).where(
            User.username == settings.local_actor_username,
            User.is_active.is_(True),
        )
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The local API actor has not been bootstrapped.",
        )
    if user.role not in VALID_ROLES:
        raise HTTPException(status_code=403, detail="User role is not supported.")
    return user


def require_admin(actor: User = Depends(get_current_user)):
    if actor.role != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return actor


def require_researcher(actor: User = Depends(get_current_user)):
    """Allow users who can author Experiments and work with analysis data."""
    if actor.role not in EDITOR_ROLES:
        raise HTTPException(status_code=403, detail="Researcher access is required.")
    return actor
