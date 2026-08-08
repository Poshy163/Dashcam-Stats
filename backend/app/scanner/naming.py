"""Filename parsing and front/rear pairing.

The corpus names files ``YYYYMMDDHHMMSS_camera_N.ts``. Other patterns are accepted as
fallbacks, but an unrecognised name returns ``None`` rather than a guess — a wrong start
time would put a recording in the wrong journey and misplace every detection in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Camera, CameraRole

#: Front and rear filenames for the same moment differ by up to a few seconds — the two
#: cameras open their files independently. In the real corpus the offsets run -3..+2 s.
DEFAULT_PAIR_WINDOW_S = 5.0

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # The corpus format: 20260804174353_camera_0.ts
    (re.compile(r"^(?P<ts>\d{14})_(?P<cam>camera_\d+)$", re.IGNORECASE), "%Y%m%d%H%M%S"),
    # Same stamp, no camera token.
    (re.compile(r"^(?P<ts>\d{14})$"), "%Y%m%d%H%M%S"),
    # Common alternatives from other dashcams.
    (re.compile(r"^(?P<ts>\d{8}_\d{6})_?(?P<cam>[A-Za-z0-9_]+)?$"), "%Y%m%d_%H%M%S"),
    (
        re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[_ ]\d{2}-\d{2}-\d{2})[-_]?(?P<cam>[A-Za-z]+)?$"),
        "%Y-%m-%d_%H-%M-%S",
    ),
)

#: Tokens that identify a camera position when the filename does not use camera_N.
_ROLE_HINTS: tuple[tuple[re.Pattern[str], CameraRole], ...] = (
    (re.compile(r"(front|_f$|^f$|_a$)", re.IGNORECASE), CameraRole.FRONT),
    (re.compile(r"(rear|back|_r$|^r$|_b$)", re.IGNORECASE), CameraRole.REAR),
    (re.compile(r"(cabin|interior|inside|_i$)", re.IGNORECASE), CameraRole.CABIN),
)

#: camera_0 is the front camera and camera_1 the rear. This is not discoverable from the
#: files — it was confirmed against the physical installation — and the mapping is seeded
#: as data so a different dashcam can be reconfigured in the UI.
_INDEX_ROLES: dict[str, CameraRole] = {
    "camera_0": CameraRole.FRONT,
    "camera_1": CameraRole.REAR,
}


@dataclass(frozen=True, slots=True)
class ParsedName:
    started_at: datetime
    camera_key: str
    role: CameraRole


def _role_for(camera_key: str) -> CameraRole:
    known = _INDEX_ROLES.get(camera_key.lower())
    if known is not None:
        return known
    for pattern, role in _ROLE_HINTS:
        if pattern.search(camera_key):
            return role
    return CameraRole.OTHER


def parse_recording_name(filename: str) -> ParsedName | None:
    """Extract the start time and camera from a filename, or None if unrecognised."""
    stem = filename.rsplit(".", 1)[0]
    for pattern, fmt in _PATTERNS:
        match = pattern.match(stem)
        if not match:
            continue
        try:
            started_at = datetime.strptime(match.group("ts"), fmt)
        except ValueError:
            # A well-shaped but impossible stamp (month 13, hour 25) is corruption, not a
            # different convention — try the remaining patterns rather than accepting it.
            continue
        camera_key = (match.groupdict().get("cam") or "camera_0").lower()
        return ParsedName(started_at=started_at, camera_key=camera_key, role=_role_for(camera_key))
    return None


async def resolve_camera(session: AsyncSession, camera_key: str) -> Camera:
    """Fetch or create the Camera row for a filename token."""
    existing = (
        await session.execute(select(Camera).where(Camera.key == camera_key))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    role = _role_for(camera_key)
    name = {
        CameraRole.FRONT: "Front",
        CameraRole.REAR: "Rear",
        CameraRole.CABIN: "Cabin",
    }.get(role, camera_key.replace("_", " ").title())

    camera = Camera(key=camera_key, name=name, role=role)
    session.add(camera)
    await session.flush()
    return camera


def find_time_pair(
    target: datetime,
    candidates: list[tuple[datetime, int]],
    window_s: float = DEFAULT_PAIR_WINDOW_S,
) -> int | None:
    """Nearest candidate id within ``window_s`` of *target*, or None.

    Pairing is by time proximity and never by filename equality: in the real corpus only
    106 of 354 front files share an exact timestamp with a rear file, and 44 have no rear
    counterpart at all. Matching on equality would drop most pairs and then invent a
    separate journey for every unmatched rear segment.
    """
    best_id: int | None = None
    best_delta = timedelta(seconds=window_s)
    for when, candidate_id in candidates:
        delta = abs(when - target)
        if delta <= best_delta:
            best_delta = delta
            best_id = candidate_id
    return best_id
