from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import User


def get_current_user(database: Session = Depends(get_db)):
    """Return the single local actor until authenticated users are introduced."""
    settings = get_settings()
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
    return user
