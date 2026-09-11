import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=1024)
    role: str = "researcher"

    @field_validator("role")
    @classmethod
    def valid_role(cls, value):
        if value not in {"admin", "researcher", "operator"}:
            raise ValueError("Role must be admin, researcher, or operator.")
        return value


class UserUpdate(BaseModel):
    username: str | None = Field(
        default=None,
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    password: str | None = Field(default=None, min_length=10, max_length=1024)
    role: str | None = None
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, value):
        if value is not None and value not in {"admin", "researcher", "operator"}:
            raise ValueError("Role must be admin, researcher, or operator.")
        return value


class UserResponse(BaseModel):
    id: uuid.UUID
    username: str
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class DeviceTokenCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Device-token label cannot be blank.")
        return value


class DeviceTokenResponse(BaseModel):
    id: uuid.UUID
    label: str
    created_at: datetime
    revoked_at: datetime | None


class DeviceTokenIssuedResponse(DeviceTokenResponse):
    device_token: str
