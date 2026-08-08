"""Deployment configuration.

Deliberately tiny. Only things that must be known before the database exists live
here; everything a user would reasonably want to change is a UI setting in
``app.core.settings_schema`` and is stored in the database.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DASHCAM_", env_file=None, extra="ignore")

    #: Persistent application state. Never a deletion target, under any circumstance.
    data_dir: Path = Field(default=Path("/data"))
    #: Raw footage mount. Mounted read-only in the recommended deployment.
    footage_dir: Path = Field(default=Path("/dashcam"))

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    #: Set by the container build so the UI can show what it is running.
    version: str = Field(default="dev")

    #: Escape hatch for tests and unusual deployments; normally left alone.
    database_url: str | None = None

    @field_validator("data_dir", "footage_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(os.path.expanduser(v))
        return v

    # -- derived paths -----------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "dashcam.db"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def sync_sqlalchemy_url(self) -> str:
        """Sync URL, used by Alembic migrations."""
        if self.database_url:
            return self.database_url.replace("+aiosqlite", "")
        return f"sqlite:///{self.db_path}"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.media_dir, self.models_dir, self.logs_dir, self.cache_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig()
