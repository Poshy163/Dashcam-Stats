"""Rows the application assumes exist on first boot.

Seeding creates, it never overwrites: ``init_db`` runs this every start, and a user who
renamed a camera or retuned an OSD region must not have that undone on restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import AppSetting, Camera, CameraRole, OsdProfile, Recording

log = get_logger(__name__)

DEFAULT_OSD_PROFILE_NAME = "default"

#: Fractions of frame width/height, so the profile survives a non-1080p source. The OSD
#: is a single text band hugging the bottom edge of both cameras.
DEFAULT_OSD_REGION: tuple[float, float, float, float] = (0.0, 0.9537, 1.0, 0.0463)

#: Parses the burned-in overlay, e.g.
#:     2026-08-04 17:44:38   E:138.6769 N:-34.8088  68 km/h
#: `E:` is LONGITUDE and `N:` is LATITUDE -- signed decimal degrees, not hemisphere
#: letters, despite the labels (ARCHITECTURE.md section 1.3). Separator runs are matched
#: loosely because glyph segmentation collapses variable whitespace.
DEFAULT_OSD_PATTERN = (
    r"(?P<date>\d{4}-\d{2}-\d{2})\s*"
    r"(?P<time>\d{1,2}:\d{2}:\d{2})\s*"
    r"E\s*:\s*(?P<lon>[-+]?\d{1,3}\.\d{1,6})\s*"
    r"N\s*:\s*(?P<lat>[-+]?\d{1,3}\.\d{1,6})\s*"
    r"(?P<speed>\d{1,3}(?:\.\d{1,2})?)\s*km\s*/\s*h"
)


@dataclass(frozen=True)
class CameraSeed:
    key: str
    name: str
    role: CameraRole
    description: str


#: The keys are the token in ``YYYYMMDDHHMMSS_camera_N.ts``. This mapping is data rather
#: than code so a different dashcam can be configured through the UI.
DEFAULT_CAMERAS: tuple[CameraSeed, ...] = (
    CameraSeed(
        key="camera_0",
        name="Front",
        role=CameraRole.FRONT,
        description="Forward-facing camera. ~30 fps with an 8 kHz mono AAC track.",
    ),
    CameraSeed(
        key="camera_1",
        name="Rear",
        role=CameraRole.REAR,
        description="Rear-facing camera. ~25 fps, no audio. Carries the same OSD as the front.",
    ),
)


async def seed_cameras(session: AsyncSession) -> list[Camera]:
    """Ensure the front/rear camera rows exist. Returns them in DEFAULT_CAMERAS order."""
    keys = [c.key for c in DEFAULT_CAMERAS]
    existing = {
        camera.key: camera
        for camera in (await session.scalars(select(Camera).where(Camera.key.in_(keys)))).all()
    }

    result: list[Camera] = []
    created = False
    for spec in DEFAULT_CAMERAS:
        camera = existing.get(spec.key)
        if camera is None:
            camera = Camera(
                key=spec.key,
                name=spec.name,
                role=spec.role,
                description=spec.description,
            )
            session.add(camera)
            created = True
        result.append(camera)

    if created:
        # Flush so callers in the same transaction can use camera.id straight away.
        await session.flush()
    return result


async def seed_osd_profile(session: AsyncSession) -> OsdProfile:
    """Ensure the default OSD profile exists.

    Telemetry in this corpus is pixels, not metadata, so without a profile the telemetry
    stage has nothing to read.
    """
    profile = await session.scalar(
        select(OsdProfile).where(OsdProfile.name == DEFAULT_OSD_PROFILE_NAME)
    )
    if profile is not None:
        return profile

    x, y, w, h = DEFAULT_OSD_REGION
    profile = OsdProfile(
        name=DEFAULT_OSD_PROFILE_NAME,
        region_x=x,
        region_y=y,
        region_w=w,
        region_h=h,
        pattern=DEFAULT_OSD_PATTERN,
        active=True,
        auto_calibrated=False,
        applies_to_camera_id=None,
        notes=(
            "Built-in profile for the bottom-edge overlay: "
            "'YYYY-MM-DD HH:MM:SS  E:<lon> N:<lat>  <speed> km/h'. Updates at 1 Hz. "
            "'E:00.0000 N:00.0000' is a no-fix marker and must never be stored as (0, 0)."
        ),
    )
    session.add(profile)
    await session.flush()
    return profile


async def seed_timezone(session: AsyncSession) -> bool:
    """Write ``general.timezone`` from ``TZ`` the first time, and never again.

    ``TZ`` is one of the five documented deployment variables and is what a first-time
    installer sets; the zone that actually decides when every recording happened is the
    ``general.timezone`` setting, which takes its *default* from it. A default is recomputed
    on every process start, so without this row the meaning of an existing library would
    quietly change the day somebody edited the compose file -- reinterpreting every
    filename timestamp, and with it every journey boundary and date filter, with no
    migration and no warning.

    Writing it once turns that into what the README already promises: ``TZ`` seeds the
    setting on first boot, and after that the setting is where the zone lives.

    **A library that has already been indexed keeps the zone it was indexed with**, which
    is the whole reason this pins a value rather than leaving a default to be recomputed.
    ``TZ`` did nothing before this existed, so a deployment holding recordings read every
    one of their filenames through the old hardcoded default whatever its ``TZ`` said --
    and writing the environment's answer into such a database would shift every timestamp
    in it retroactively, splitting journeys at the wrong places and moving every date
    filter, on an upgrade nobody asked to change anything. The presence of recordings is
    what separates the two cases, and it is exact: no recordings means nothing has been
    interpreted yet.

    Returns True when a row was created.
    """
    from app.core.settings_schema import HISTORICAL_TIMEZONE, default_timezone

    key = "general.timezone"
    existing = (
        await session.execute(select(AppSetting).where(AppSetting.key == key))
    ).scalar_one_or_none()
    if existing is not None:
        return False

    indexed = int((await session.execute(select(func.count(Recording.id)))).scalar() or 0)
    value = default_timezone() if indexed == 0 else HISTORICAL_TIMEZONE
    if indexed:
        log.info(
            "pinning the timezone this library was already indexed with; change it in "
            "Settings if it is wrong",
            timezone=value,
            recordings=indexed,
        )
    session.add(AppSetting(key=key, value=value))
    await session.flush()
    return True


async def seed_defaults(session: AsyncSession) -> None:
    """Every seed step, in dependency order. Safe to run on every boot."""
    await seed_cameras(session)
    await seed_osd_profile(session)
    await seed_timezone(session)


__all__ = [
    "DEFAULT_CAMERAS",
    "DEFAULT_OSD_PATTERN",
    "DEFAULT_OSD_PROFILE_NAME",
    "DEFAULT_OSD_REGION",
    "CameraSeed",
    "seed_cameras",
    "seed_defaults",
    "seed_osd_profile",
    "seed_timezone",
]
