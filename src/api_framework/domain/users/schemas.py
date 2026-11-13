"""User and token API schemas.

Request and response models are separate types on purpose. A single shared
model is how password hashes and internal flags end up in responses: the field
exists for one direction and leaks in the other.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from api_framework.auth.principal import Role

MIN_PASSWORD_LENGTH = 12


class UserCreate(BaseModel):
    """Registration / admin-create payload."""

    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("password")
    @classmethod
    def _enforce_password_complexity(cls, value: str) -> str:
        """Require a mix of character classes.

        Length is the dominant factor in password strength, which is why the
        minimum is 12 rather than the traditional 8. Composition rules add
        little, but they are cheap and commonly mandated; the important part is
        that this runs before hashing and is expressed once, here.
        """
        if not any(ch.isupper() for ch in value):
            raise ValueError("Password must contain an uppercase letter")
        if not any(ch.islower() for ch in value):
            raise ValueError("Password must contain a lowercase letter")
        if not any(ch.isdigit() for ch in value):
            raise ValueError("Password must contain a digit")
        return value


class UserUpdate(BaseModel):
    """PATCH payload. Every field optional; unset fields are left untouched."""

    full_name: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None


class UserRead(BaseModel):
    """Outbound user representation. Note the absence of `hashed_password`.

    Because responses are built from an explicit allow-list of fields, adding a
    sensitive column to the model later cannot accidentally expose it.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: str
    email: EmailStr
    full_name: str | None
    # Read from `User.role_set` (parsed enum values), not the raw
    # comma-separated `User.roles` column, so the wire format is a JSON array.
    roles: list[Role] = Field(validation_alias="role_set")
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None


class PrincipalRead(BaseModel):
    """What the token verifier extracted. Response model for `/auth/me/principal`."""

    model_config = ConfigDict(from_attributes=True)

    subject: str
    roles: list[Role]
    email: str | None = None
    token_id: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    """OAuth2-shaped token response.

    `token_type` and `expires_in` are part of RFC 6749 and are what generic
    OAuth2 clients (including Swagger UI) expect.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)
