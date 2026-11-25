"""Application settings, loaded from environment variables / .env."""

from functools import lru_cache

import os

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime configuration. Nothing secret lives in code."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "Saathi"
    debug: bool = False

    # Database
    database_url: str = "postgresql+psycopg://saathi:saathi@localhost:5432/saathi"

    # LLM
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.5-flash-lite"
    google_api_key: str = ""

    # Mobile app
    app_login_days: int = Field(default=30, ge=1, le=365)

    # Seed users (phone numbers in international format, digits only, e.g. 919876543210)
    seed_shopkeeper_phone: str = ""
    seed_shopkeeper_name: str = "Shopkeeper"
    seed_professor_phone: str = ""
    seed_professor_name: str = "Professor"
    seed_pin: str = ""  # 6 digits: lets the seeded demo users log in to the app

    # Agent behaviour
    history_limit: int = Field(default=10, ge=1)
    approval_threshold: int = Field(default=5000, ge=0)
    approval_ttl_minutes: int = Field(default=10, ge=1)

    # Files
    bills_dir: str = "bills"
    chroma_dir: str = "chroma"  # vector store for lecture notes
    embedding_model: str = "models/gemini-embedding-001"

    # Dashboard (off unless a secret is set)
    dashboard_secret: str = ""
    public_base_url: str = "http://localhost:8000"  # the ngrok/https URL in a demo
    dashboard_link_hours: int = Field(default=24, ge=1, le=168)

    # Scheduler
    scheduler_enabled: bool = True
    summary_hour: int = Field(default=21, ge=0, le=23)  # IST
    assignment_reminder_hour: int = Field(default=18, ge=0, le=23)  # IST, for assignments due the next day

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, v: str) -> str:
        # hosts such as Render hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the psycopg 3 driver
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @field_validator("public_base_url")
    @classmethod
    def _render_url(cls, v: str) -> str:
        # on Render the service's own https address is known only at runtime
        return os.environ.get("RENDER_EXTERNAL_URL", v) if v == "http://localhost:8000" else v


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings object."""
    return Settings()
