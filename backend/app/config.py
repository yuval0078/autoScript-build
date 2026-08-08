from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AUTOSCRIPT_",
        extra="ignore",
    )

    app_name: str = "AutoScript API"
    environment: str = "development"
    database_url: str = (
        "postgresql+psycopg://autoscript:autoscript@localhost:5432/autoscript"
    )
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "autoscript"
    minio_secret_key: str = "change-me"
    minio_bucket: str = "autoscript-artifacts"
    minio_secure: bool = False
    local_actor_username: str = "local-admin"
    max_upload_bytes: int = 512 * 1024 * 1024
    max_uncompressed_package_bytes: int = 2 * 1024 * 1024 * 1024


@lru_cache
def get_settings():
    return Settings()
