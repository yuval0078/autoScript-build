from sqlalchemy import select

from .config import get_settings
from .database import get_session_factory
from .models import User
from .services.storage import get_object_storage


def main():
    settings = get_settings()
    storage = get_object_storage()
    storage.ensure_bucket()
    print(f"Object-storage bucket is ready: {storage.bucket_name}")

    with get_session_factory()() as database:
        actor = database.scalar(
            select(User).where(User.username == settings.local_actor_username)
        )
        if actor is None:
            database.add(
                User(
                    username=settings.local_actor_username,
                    password_hash="!local-auth-disabled!",
                    role="admin",
                    is_active=True,
                )
            )
            database.commit()
            print(f"Local API actor is ready: {settings.local_actor_username}")


if __name__ == "__main__":
    main()
