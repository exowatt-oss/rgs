"""Standalone application settings; external identity configuration belongs to forwarded-auth."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parents[3] / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    app_env: str = "prod"
    environment: str = "production"
    app_base_url: str = ""
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://dev:dev@localhost:5432/app"
    admin_emails: str = ""  # Comma-separated verified human addresses; no auto-admin.
    cors_origins: list[str] = Field(default_factory=list)  # Empty in production by default.

    @model_validator(mode="after")
    def normalize_database_driver(self) -> Settings:
        if self.database_url.startswith("postgresql://"):
            self.database_url = self.database_url.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        elif self.database_url.startswith("postgres://"):
            self.database_url = self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
        return self

    @property
    def is_prod(self) -> bool:
        # Only two explicit local/test signals permit insecure demo identity.
        # Staging, unknown and misspelled values fail closed as production.
        return not (
            self.app_env in {"local", "dev", "test"}
            and self.environment in {"local", "development", "test"}
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
