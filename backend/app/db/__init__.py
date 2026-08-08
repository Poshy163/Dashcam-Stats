"""Data layer: the schema, the engine/session plumbing and first-boot seeding."""

from __future__ import annotations

from app.db import models
from app.db.models import Base
from app.db.seed import (
    DEFAULT_CAMERAS,
    DEFAULT_OSD_PATTERN,
    DEFAULT_OSD_PROFILE_NAME,
    DEFAULT_OSD_REGION,
    seed_cameras,
    seed_defaults,
    seed_osd_profile,
)
from app.db.session import (
    dispose_engine,
    get_engine,
    get_session,
    get_session_factory,
    init_db,
    session_scope,
    upgrade_to_head,
)

__all__ = [
    "DEFAULT_CAMERAS",
    "DEFAULT_OSD_PATTERN",
    "DEFAULT_OSD_PROFILE_NAME",
    "DEFAULT_OSD_REGION",
    "Base",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_db",
    "models",
    "seed_cameras",
    "seed_defaults",
    "seed_osd_profile",
    "session_scope",
    "upgrade_to_head",
]
