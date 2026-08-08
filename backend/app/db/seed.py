"""Rows the application assumes exist on first boot.

Seeding creates, it never overwrites: ``init_db`` runs this every start, and a user who
renamed a camera or retuned an OSD region must not have that undone on restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Camera, CameraRole, OsdProfile

DEFAULT_OSD_PROFILE_NAME = "default"

#: Fractions of frame width/height, so the profile survives a non-1080p source. The OSD
#: is a single text band hugging the bottom edge of both cameras.
DEFAULT_OSD_REGION: tuple[float, float, float, float] = (0.0, 0.94, 1.0, 0.06)

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


async def seed_defaults(session: AsyncSession) -> None:
    """Every seed step, in dependency order. Safe to run on every boot."""
    await seed_cameras(session)
    await seed_osd_profile(session)


__all__ = [
    "DEFAULT_CAMERAS",
    "DEFAULT_OSD_PATTERN",
    "DEFAULT_OSD_PROFILE_NAME",
    "DEFAULT_OSD_REGION",
    "CameraSeed",
    "seed_cameras",
    "seed_defaults",
    "seed_osd_profile",
]
