"""Security-specific settings kept separate from the application data config."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecuritySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="AUTOSCRIPT_",
        extra="ignore",
    )

    login_rate_limit_attempts: int = Field(default=5, ge=2, le=100)
    login_rate_limit_address_multiplier: int = Field(default=4, ge=1, le=100)
    login_rate_limit_window_seconds: int = Field(default=300, ge=10, le=86_400)
    login_rate_limit_lockout_seconds: int = Field(default=900, ge=10, le=86_400)
    security_state_retention_days: int = Field(default=30, ge=1, le=3650)


@lru_cache
def get_security_settings():
    return SecuritySettings()
