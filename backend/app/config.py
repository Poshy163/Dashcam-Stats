"""Deployment configuration.

Deliberately tiny. Only things that must be known before the database exists live
here; everything a user would reasonably want to change is a UI setting in
``app.core.settings_schema`` and is stored in the database.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
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

    # Home Assistant is the one deployment concern that deliberately does not live in
    # the settings table.  The bearer token must never be returned by GET /api/settings,
    # copied into the database backup, or cached into worker snapshots.  Only a path to a
    # Docker secret is accepted.  The unprefixed aliases match Home Assistant's usual
    # compose spelling; DASHCAM_* aliases keep this coherent with the existing config.
    ha_url: str = Field(
        default="",
        validation_alias=AliasChoices("HA_URL", "DASHCAM_HA_URL"),
    )
    ha_token_file: Path = Field(
        default=Path("/run/secrets/home_assistant_token"),
        validation_alias=AliasChoices("HA_TOKEN_FILE", "DASHCAM_HA_TOKEN_FILE"),
    )
    ha_obd_import_path: str = Field(
        default="/api/obd2_ble/import",
        validation_alias=AliasChoices("HA_OBD_IMPORT_PATH", "DASHCAM_HA_OBD_IMPORT_PATH"),
    )
    ha_request_timeout_s: float = Field(default=30.0, ge=2.0, le=300.0)

    #: Atomic exports published by the companion logger.  Kept off the footage path so a
    #: missing/unmounted media share can never make OBD validation delete or overwrite
    #: footage (and vice versa).
    obd_remote_ready_dir: str = Field(
        default="/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/ready",
        validation_alias=AliasChoices("DASHCAM_OBD_REMOTE_DIR", "DASHCAM_OBD_REMOTE_READY_DIR"),
    )
    obd_remote_status_file: str = Field(
        default="/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/status.json",
        validation_alias=AliasChoices("DASHCAM_OBD_STATUS_PATH", "DASHCAM_OBD_REMOTE_STATUS_FILE"),
    )
    obd_remote_receipts_dir: str = Field(
        default=("/storage/Tfcard/Android/data/com.dashcamstats.obdlogger/files/obd/receipts"),
        validation_alias=AliasChoices(
            "DASHCAM_OBD_RECEIPTS_DIR", "DASHCAM_OBD_REMOTE_RECEIPTS_DIR"
        ),
    )
    obd_max_bundle_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    obd_max_expanded_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024)
    obd_max_compression_ratio: int = Field(default=200, ge=2, le=1000)
    obd_max_samples: int = Field(default=10000, ge=1, le=100000)
    obd_import_poll_s: float = Field(default=5.0, ge=0.25, le=300.0)
    obd_retry_base_s: float = Field(default=30.0, ge=1.0, le=86400.0)
    obd_retry_max_s: float = Field(default=6 * 3600.0, ge=30.0, le=7 * 86400.0)

    # Authentication is deliberately absent from this file. It used to live here as
    # ``DASHCAM_AUTH_USERNAME``/``DASHCAM_AUTH_PASSWORD``, which meant the only way to put
    # a password on the app was to edit the compose file, put the password in it in clear
    # text, and restart the container -- and what you got for that was the browser's native
    # Basic prompt, with no way to sign out and no way to stay signed in. It is a UI
    # setting now, in ``app.core.settings_schema``, with the account in its own table. See
    # ``app.auth``.

    @field_validator("data_dir", "footage_dir", "ha_token_file", mode="before")
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

    @property
    def obd_dir(self) -> Path:
        return self.data_dir / "obd"

    @property
    def obd_staging_dir(self) -> Path:
        return self.obd_dir / "staging"

    @property
    def obd_verified_dir(self) -> Path:
        return self.obd_dir / "verified"

    @property
    def obd_quarantine_dir(self) -> Path:
        return self.obd_dir / "quarantine"

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.media_dir,
            self.models_dir,
            self.logs_dir,
            self.cache_dir,
            self.obd_staging_dir,
            self.obd_verified_dir,
            self.obd_quarantine_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig()
