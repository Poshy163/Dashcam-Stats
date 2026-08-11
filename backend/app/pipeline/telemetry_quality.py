"""Telemetry quality rollups and conservative paired-camera recovery."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Recording, StageState, TelemetryPoint


def quality_rollup(rows: Iterable[object]) -> tuple[int, float, int, int, int, int]:
    """Return gaps, longest, problems, explicit no-fix, OCR gaps and rejected fixes."""
    gaps = 0
    longest = 0.0
    start: float | None = None
    last = 0.0
    problems = 0
    no_fix = 0
    ocr_gap = 0
    rejected = 0
    for row in rows:
        if isinstance(row, dict):
            offset = float(row.get("t_offset_s", 0.0))
            quality = row.get("quality_json") or {}
            has_fix = bool(row.get("has_fix", False))
        else:
            offset = float(getattr(row, "t_offset_s", 0.0))
            quality = getattr(row, "quality_json", None) or {}
            has_fix = bool(getattr(row, "has_fix", False))
        if quality.get("problems") or quality.get("ocr_status") in {"failed", "rejected"}:
            problems += 1
        if not has_fix:
            gps_status = quality.get("gps_status")
            if gps_status == "no_fix":
                no_fix += 1
            elif gps_status == "rejected":
                rejected += 1
            else:
                ocr_gap += 1
            if start is None:
                start = offset
                gaps += 1
            last = offset
        elif start is not None:
            longest = max(longest, last - start + 1.0)
            start = None
    if start is not None:
        longest = max(longest, last - start + 1.0)
    return gaps, round(longest, 2), problems, no_fix, ocr_gap, rejected


async def recover_from_paired_camera(session: AsyncSession, recording: Recording) -> int:
    """Fill OCR-only holes from an overlapping camera, never an explicit GPS no-fix.

    A second camera is independent evidence only when its overlay decoded successfully.
    The camera's explicit zero-coordinate/no-fix marker is left untouched because copying
    over it would turn genuine satellite loss into invented continuity.
    """
    if recording.started_at is None or recording.ended_at is None:
        return 0
    partners = list(
        (
            await session.execute(
                select(Recording).where(
                    Recording.id != recording.id,
                    Recording.camera_id != recording.camera_id,
                    Recording.telemetry_state == StageState.DONE,
                    Recording.started_at < recording.ended_at,
                    Recording.ended_at > recording.started_at,
                )
            )
        )
        .scalars()
        .all()
    )
    if not partners:
        return 0

    recovered = 0
    pairs = [(recording, partner) for partner in partners] + [
        (partner, recording) for partner in partners
    ]
    for target_recording, source_recording in pairs:
        target = list(
            (
                await session.execute(
                    select(TelemetryPoint)
                    .where(TelemetryPoint.recording_id == target_recording.id)
                    .order_by(TelemetryPoint.t_offset_s)
                )
            )
            .scalars()
            .all()
        )
        source = list(
            (
                await session.execute(
                    select(TelemetryPoint).where(
                        TelemetryPoint.recording_id == source_recording.id,
                        TelemetryPoint.has_fix.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_second = {
            round(point.captured_at.timestamp()): point
            for point in source
            if point.captured_at is not None and point.lat is not None and point.lon is not None
        }
        changed = 0
        for point in target:
            quality = dict(point.quality_json or {})
            if point.has_fix or quality.get("gps_status") == "no_fix" or point.captured_at is None:
                continue
            donor = by_second.get(round(point.captured_at.timestamp()))
            if donor is None:
                continue
            point.lat, point.lon, point.has_fix = donor.lat, donor.lon, True
            point.speed_kmh = point.speed_kmh if point.speed_kmh is not None else donor.speed_kmh
            point.heading_deg = donor.heading_deg
            quality.update(
                gps_status="recovered",
                gps_source="paired_camera",
                paired_recording_id=source_recording.id,
                interpolated=False,
            )
            quality["problems"] = [
                p for p in quality.get("problems", []) if "position" not in str(p).lower()
            ]
            point.quality_json = quality
            changed += 1
        if changed:
            gaps, longest, problems, no_fix, ocr_gap, rejected = quality_rollup(target)
            target_recording.gps_recovered_count += changed
            target_recording.gps_point_count = sum(point.has_fix for point in target)
            target_recording.has_gps = target_recording.gps_point_count > 0
            target_recording.gps_gap_count = gaps
            target_recording.gps_longest_gap_s = longest
            target_recording.telemetry_problem_count = problems
            target_recording.gps_no_fix_count = no_fix
            target_recording.gps_ocr_gap_count = ocr_gap
            target_recording.gps_rejected_count = rejected
            recovered += changed
    return recovered
