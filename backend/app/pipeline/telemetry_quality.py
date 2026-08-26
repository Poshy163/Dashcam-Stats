"""Telemetry quality rollups and conservative paired-camera recovery."""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import Recording, StageState, TelemetryPoint
from app.osd.reasons import GpsQuality
from app.osd.track_quality import Fix, classify

log = get_logger(__name__)

#: A "problem" the extractor used to record on every cleanly parsed sample.
#:
#: Two candidate frames per overlay second is the design, so this said nothing except that
#: the extractor did its job. It was counted all the same, which made
#: ``telemetry_problem_count`` equal the point count on healthy recordings and put every
#: library on the Telemetry Health page into "degraded". The extractor no longer writes it;
#: this is here because the rows it already wrote are still in the database, and a rollup
#: recomputed over them must reach the same verdict as one computed from fresh samples.
#: Matched rather than deleted from ``quality_json``: how many frames were considered is
#: real provenance, and it stays where it was recorded.
_LEGACY_CANDIDATE_NOISE = re.compile(r"^selected best fields from \d+ candidate frames$")


def real_problems(problems: object) -> list[str]:
    """The entries in a stored ``problems`` list that describe an actual fault."""
    if not isinstance(problems, (list, tuple)):
        return []
    return [str(p) for p in problems if not _LEGACY_CANDIDATE_NOISE.match(str(p))]


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
        if real_problems(quality.get("problems")) or quality.get("ocr_status") in {
            "failed",
            "rejected",
        }:
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


def _revert_implausible(target: list[TelemetryPoint], filled: list[TelemetryPoint]) -> int:
    """Undo copied positions that the target recording's own fixes contradict.

    Only the rows this pass filled are eligible to be undone. A position the target read
    for itself is not this function's business — it has already been judged where it was
    extracted, and second-guessing it here would apply the same rule twice with less
    context than the first time.
    """
    ordered = sorted(
        (p for p in target if p.has_fix and p.lat is not None and p.lon is not None),
        key=lambda p: float(p.t_offset_s or 0.0),
    )
    if len(ordered) < 3:
        return 0

    verdicts = classify(
        [
            Fix(
                t_s=float(point.t_offset_s or 0.0),
                lat=float(point.lat),
                lon=float(point.lon),
                # Copied positions are corroboration, not independent evidence, so they
                # are marked synthetic and never used to justify one another.
                synthetic=(point.quality_json or {}).get("gps_source") == "paired_camera",
            )
            for point in ordered
        ]
    )

    eligible = {id(point) for point in filled}
    reverted = 0
    for point, verdict in zip(ordered, verdicts, strict=True):
        if verdict.quality is not GpsQuality.REJECTED or id(point) not in eligible:
            continue
        quality = dict(point.quality_json or {})
        quality.update(
            gps_status="rejected",
            gps_source="none",
            gps_reason=str(verdict.reasons[0]),
            interpolated=False,
        )
        quality["problems"] = [*quality.get("problems", []), verdict.detail]
        point.quality_json = quality
        point.lat = point.lon = point.heading_deg = None
        point.has_fix = False
        point.gps_quality = str(GpsQuality.REJECTED)
        point.gps_reason = str(verdict.reasons[0])
        reverted += 1
    if reverted:
        log.info(
            "refused positions copied from the paired camera",
            reverted=reverted,
            of=len(filled),
        )
    return reverted


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
        filled: list[TelemetryPoint] = []
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
            # A copied position is corroboration from the other camera, not something this
            # recording observed, so it carries the same weight as an interpolated one:
            # drawable, never counted as evidence for distance or for placing a sighting.
            point.gps_quality = str(GpsQuality.INTERPOLATED)
            point.gps_reason = None
            filled.append(point)
            changed += 1

        # Every donor is matched on the second its overlay clock printed, and that clock is
        # read by the same OCR as everything else. A single misread digit moves it by an
        # hour or a day, so the second it lands on may belong to a completely different
        # part of the drive -- and nothing here had ever checked that the coordinate it
        # brought with it made sense where it was pasted. On the live library that put a
        # copied position 3.1 km off the route in the middle of an otherwise clean clip.
        #
        # Judging the filled rows against the whole target timeline is what catches it:
        # a donor from the wrong second disagrees with the target's own fixes either side.
        if filled:
            reverted = _revert_implausible(target, filled)
            changed -= reverted
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
