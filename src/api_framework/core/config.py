"""Application settings.

Single source of truth for configuration. Values come from the environment
(12-factor); `.env` is read only as a developer convenience.

Why one settings object rather than scattered `os.getenv` calls:

* every value is validated and typed once, at startup;
* a missing or malformed value fails fast at boot instead of at 3am on the
  first request that happens to need it;
* the settings object is trivially overridable in tests.
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

INSECURE_SECRET_KEY = "change-me-in-every-environment-except-local"


class Environment(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    """How bearer tokens are verified.

    LOCAL    - this service issues and verifies its own symmetric (HS256)
               tokens. Self-contained: clone and run.
    EXTERNAL - tokens are issued by an external identity provider and verified
               against its JWKS endpoint using asymmetric keys. This service
               never handles credentials.
    """

    LOCAL = "local"
    EXTERNAL = "external"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Application ------------------------------------------------------
    env: Environment = Environment.LOCAL
    debug: bool = False
    project_name: str = "API Framework"
    api_v1_prefix: str = "/api/v1"

    # --- Logging ----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False
    log_request_body: bool = False

    # --- Database ---------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./api_framework.db"
    db_echo: bool = False

    # --- Security ---------------------------------------------------------
    secret_key: str = INSECURE_SECRET_KEY
    access_token_ttl_minutes: int = Field(default=15, ge=1)
    refresh_token_ttl_days: int = Field(default=14, ge=1)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "api-framework"
    jwt_audience: str = "api-framework"

    # --- Auth mode --------------------------------------------------------
    auth_mode: AuthMode = AuthMode.LOCAL
    oidc_jwks_url: str | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_roles_claim: str = "roles"

    # --- CORS -------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Bootstrap admin --------------------------------------------------
    first_admin_email: str | None = "admin@example.com"
    first_admin_password: str | None = "ChangeMe123!"

    # NOTE: these comparisons use `==`, not `is`.
    # `Environment` is a StrEnum, so `Environment.PRODUCTION == "production"`
    # is True while `is` is False. `model_copy(update=...)` bypasses validation
    # and can therefore leave a plain string in this field - with `is` that
    # silently reports "not production" and re-enables /docs. `==` is correct
    # for both the enum and the string.
    @property
    def is_production(self) -> bool:
        return self.env == Environment.PRODUCTION

    @property
    def is_local(self) -> bool:
        return self.env == Environment.LOCAL

    @property
    def is_external_auth(self) -> bool:
        return self.auth_mode == AuthMode.EXTERNAL

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @model_validator(mode="after")
    def _enforce_deployment_safety(self) -> Self:
        """Refuse to start with a configuration that is unsafe when deployed.

        These checks are deliberately fatal. A service that boots with a
        default signing key is worse than one that does not boot: the failure
        is silent, and every token it issues is forgeable.
        """
        if self.is_local:
            return self

        problems: list[str] = []

        if self.secret_key == INSECURE_SECRET_KEY:
            problems.append(
                "APP_SECRET_KEY is still the shipped default. Generate one with "
                '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
            )
        if len(self.secret_key) < 32:
            problems.append("APP_SECRET_KEY must be at least 32 characters.")
        if self.debug:
            problems.append("APP_DEBUG must be false outside local.")
        if "*" in self.cors_origins:
            problems.append(
                'APP_CORS_ORIGINS must not be ["*"] outside local: wildcard origins '
                "cannot be combined with credentialed requests."
            )
        if self.is_external_auth and not self.oidc_jwks_url:
            problems.append("APP_OIDC_JWKS_URL is required when APP_AUTH_MODE=external.")
        if self.is_production and self.first_admin_password:
            problems.append(
                "APP_FIRST_ADMIN_PASSWORD must be unset in production; "
                "provision the first admin out of band."
            )
        if self.is_production and self.log_request_body:
            problems.append("APP_LOG_REQUEST_BODY must be false in production (PII risk).")

        if problems:
            raise ValueError(
                f"Invalid configuration for env={self.env}:\n  - " + "\n  - ".join(problems)
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the environment is parsed once. Tests call
    `get_settings.cache_clear()` after mutating the environment.
    """
    return Settings()


def generate_secret_key() -> str:
    """Convenience helper referenced by the README and Makefile."""
    return secrets.token_urlsafe(48)
