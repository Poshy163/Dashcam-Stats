"""Separate discarded candidate failures from problems in the selected telemetry.

Only known parser failures are reconciled. Repair provenance and downstream validation
failures are deliberately outside this allowlist, even when a sample still has a fix.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

PUNCTUATION_RECOVERY = "coordinate punctuation recovered from labelled overlay fields"
_LEGACY_CANDIDATE_NOISE = re.compile(r"^selected best fields from \d+ candidate frames$")
_TIME_FAILURE = re.compile(r"^(?:implausible year \d+|invalid date/time: .+)$")
_GPS_FAILURE = re.compile(
    r"^(?:coordinate precision inconsistent .+; digits were lost or gained in the read"
    r"|coordinate (?:out of range|is not finite|is not a number) \(lat=.+, lon=.+\))$"
)
_SPEED_FAILURE = re.compile(r"^implausible speed -?\d+(?:\.\d+)? km/h$")


def effective_problems(
    problems: object,
    *,
    time_valid: bool = False,
    gps_valid: bool = False,
    speed_valid: bool = False,
) -> list[str]:
    """Keep unresolved/unknown warnings; successful fields supersede parser failures.

    Evidence must describe the selected field, not merely a populated timestamp or
    position after timeline/interpolation/paired-camera recovery. Defaults intentionally
    preserve the old conservative behavior for callers without this evidence.
    """
    if not isinstance(problems, (list, tuple)):
        return []
    kept = []
    for value in problems:
        problem = str(value)
        if _LEGACY_CANDIDATE_NOISE.fullmatch(problem):
            continue
        if time_valid and (problem == "timestamp unreadable" or _TIME_FAILURE.fullmatch(problem)):
            continue
        if gps_valid and (
            problem
            in {
                "GPS fields unreadable",
                "coordinates could not be parsed",
                "coordinate sign ambiguous; refused rather than risk a wrong hemisphere",
            }
            or _GPS_FAILURE.fullmatch(problem)
        ):
            continue
        if speed_valid and (
            problem == "speed could not be parsed" or _SPEED_FAILURE.fullmatch(problem)
        ):
            continue
        if (
            time_valid
            and gps_valid
            and speed_valid
            and problem == "no recognisable overlay content"
        ):
            continue
        kept.append(problem)
    return list(dict.fromkeys(kept))


def stored_problems(
    quality: Mapping[str, object], *, has_fix: bool, speed_kmh: object = None
) -> list[str]:
    """Reconcile historical warnings without changing their recorded provenance.

    A paired-camera donor can supply speed without marking its individual source, even
    when its position is subsequently rejected. Only the original all-fields-valid OCR
    status proves this speed was parsed before recovery. Partial/unknown historical rows
    retain speed warnings. Failed/rejected OCR remains a problem in quality_rollup.
    """
    speed_valid = (
        isinstance(speed_kmh, (int, float))
        and not isinstance(speed_kmh, bool)
        and math.isfinite(speed_kmh)
        and 0 <= speed_kmh <= 400
        and quality.get("ocr_status") == "valid"
    )
    return effective_problems(
        quality.get("problems"),
        time_valid=quality.get("time_status") == "valid"
        and quality.get("time_source") == "overlay",
        gps_valid=has_fix
        and quality.get("gps_status") == "valid"
        and quality.get("gps_source") == "direct",
        speed_valid=speed_valid,
    )
