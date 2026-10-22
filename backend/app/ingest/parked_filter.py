"""Opt-in camera decisions: preserve uncertain footage and audit every exclusion."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from app.core.logging import get_logger
from app.ingest import adb, movement
from app.ingest.models import RemoteFile

log = get_logger(__name__)
CAPABILITY = "clip_transfer_policy_v1"
POLICY_VERSION = "parked_visual_quiet_v1"
MAX_BYTES = 512 * 1024
MAX_DECISIONS = 512
MAX_FINGERPRINT_CHECKS = 64
MAX_AGE_SECONDS = 180
POLICY_READ_TIMEOUT_S = 6.0
FINGERPRINT_TIMEOUT_S = 6.0
AUDIT_TIMEOUT_S = 3.0
DIAGNOSTICS_TIMEOUT_S = 1.5
MAX_DEFER_SECONDS = 360
DEFER_SLEEP_RESERVE_SECONDS = 120
TOP_KEYS = {
    "schema_version",
    "source_id",
    "card_id",
    "session_id",
    "boot_id",
    "generated_at_utc",
    "producer_version",
    "policy_version",
    "enabled",
    "mode",
    "status",
    "truncated",
    "decisions",
}
DECISION_KEYS = {"clip", "decision", "reason", "guard", "visual", "defer_until_utc"}
GUARD_KEYS = {
    "start_utc",
    "end_utc",
    "window_ms",
    "acc_off_ms",
    "unknown_ms",
    "max_gap_ms",
    "observation_count",
    "on_samples",
    "unknown_samples",
    "segment_count",
    "positive_speed_samples",
}
VISUAL_KEYS = {
    "algorithm_version",
    "status",
    "input_frames",
    "decoded_frames",
    "sampled_frames",
    "duration_ms",
    "covered_ms",
    "max_gap_ms",
    "motion_frames",
    "analysis_completed_at_utc",
    "first_pts_us",
    "last_pts_us",
    "input_eos",
    "decoder_eos",
    "grid_width",
    "grid_height",
    "max_changed_pixels",
    "motion_metric",
    "rolling_frames",
    "max_coherent_pixels",
    "max_rolling_coherent_pixels",
    "max_consecutive_coherent_frames",
    "roi_profile",
    "roi_rows",
    "min_mean_luma",
    "max_mean_luma",
    "max_dark_fraction",
    "max_bright_fraction",
    "min_contrast_span",
}
REASONS = {
    "parked_visual_quiet",
    "policy_disabled",
    "locked_clip",
    "orphan_partial",
    "unstable_or_unreadable",
    "timing_unknown",
    "acc_not_fully_off",
    "observation_gap",
    "ignition_buffer",
    "visual_required",
    "visual_activity",
    "visual_incomplete",
    "analysis_error",
    "positive_movement",
    "card_or_identity_unknown",
    "analysis_pending",
    "awaiting_post_guard",
    "file_settling",
    "peer_coverage_required",
    "peer_analysis_pending",
}


@dataclass(slots=True)
class FilterResult:
    files: list[RemoteFile]
    deferred: list[tuple[RemoteFile, datetime]] = field(default_factory=list)
    skipped: list[RemoteFile] = field(default_factory=list)
    fingerprint_checks: int = 0


VISUAL_STATUSES = {"not_analyzed", "quiet", "activity", "unavailable", "error", "incomplete"}
VISUAL_REASONS = {
    "policy_not_permitted",
    "battery_temperature_unavailable",
    "battery_temperature_guard",
    "system_thermal_unavailable",
    "system_thermal_guard",
    "memory_probe_unavailable",
    "memory_guard",
    "cpu_temperature_unavailable",
    "cpu_temperature_guard",
    "cpu_temperature_hard_limit",
    "hardware_decoder_unavailable",
    "source_unavailable",
    "format_unsupported",
    "decoder_failure",
    "image_unavailable",
    "frame_quality",
    "visual_activity",
    "coverage_incomplete",
    "analysis_timeout",
    "frame_limit",
    "analysis_pending",
    "clip_changed",
    "background_priority_unavailable",
    "analysis_storage_unavailable",
    "unrecognized_reason",
    "reason_not_reported",
}
UNSAFE_CLIP_REASONS = {
    "locked_clip",
    "orphan_partial",
    "incomplete_time_bounds",
    "no_observation_coverage",
    "observation_gap",
    "clock_ambiguous",
    "file_unstable",
    "file_unreadable",
    "duration_unknown",
    "validated_speed_positive",
    "gps_confident_movement",
}


def analysis_summary(doc: dict[str, Any] | None, state: str) -> dict[str, Any]:
    """Bounded advisory categories, never filenames, raw exceptions or skip permission."""
    statuses: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for entry in doc["decisions"] if doc is not None else []:
        visual = entry["visual"]
        statuses[visual["status"]] += 1
        reason = visual.get("reason")
        if reason in VISUAL_REASONS:
            reasons[reason] += 1
        elif visual["status"] in {"unavailable", "error", "incomplete"}:
            reasons["reason_not_reported"] += 1
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "generated_at": doc["generated_at_utc"] if doc else None,
        "status": state,
        "clip_count": sum(statuses.values()),
        "status_counts": dict(statuses),
        "reason_counts": dict(reasons),
    }


def validate_analysis_summary(value: Any) -> dict[str, Any]:
    movement._keys(
        value,
        {"checked_at", "generated_at", "status", "clip_count", "status_counts", "reason_counts"},
    )
    movement._timestamp(value["checked_at"])
    movement._timestamp(value["generated_at"], nullable=True)
    if value["status"] not in {
        "disabled",
        "unsupported",
        "missing",
        "invalid",
        "stale",
        "card_mismatch",
        "ready",
    }:
        raise movement.ManifestError("invalid_analysis_summary")
    movement._integer(value["clip_count"], MAX_DECISIONS)
    for key, allowed in (("status_counts", VISUAL_STATUSES), ("reason_counts", VISUAL_REASONS)):
        counts = value[key]
        if not isinstance(counts, dict) or not set(counts) <= allowed:
            raise movement.ManifestError("invalid_analysis_summary")
        for count in counts.values():
            movement._integer(count, MAX_DECISIONS)
        if sum(counts.values()) > value["clip_count"]:
            raise movement.ManifestError("invalid_analysis_summary")
    if sum(value["status_counts"].values()) != value["clip_count"]:
        raise movement.ManifestError("invalid_analysis_summary")
    return value


def validate_policy_manifest(raw: str) -> dict[str, Any]:
    """Strict, bounded schema; retained peer evidence never confers skip authority."""
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise movement.ManifestError("too_large")
    try:
        doc = json.loads(
            raw, object_pairs_hook=movement._unique_pairs, parse_constant=movement._no_constant
        )
    except (ValueError, RecursionError) as exc:
        raise movement.ManifestError("invalid_json") from exc
    movement._keys(doc, TOP_KEYS)
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise movement.ManifestError("unsupported_schema")
    if doc["policy_version"] != POLICY_VERSION or doc["mode"] != "acc_off_visual_quiet":
        raise movement.ManifestError("unsupported_policy")
    if type(doc["enabled"]) is not bool or type(doc["truncated"]) is not bool:
        raise movement.ManifestError("invalid_fields")
    for key in ("source_id", "session_id", "boot_id"):
        movement._identifier(doc[key])
    if doc["card_id"] is not None and (
        not isinstance(doc["card_id"], str) or not movement._HEX.fullmatch(doc["card_id"])
    ):
        raise movement.ManifestError("invalid_card_identity")
    if not isinstance(doc["producer_version"], str) or not re.fullmatch(
        r"\d{1,3}\.\d{1,3}\.\d{1,3}", doc["producer_version"]
    ):
        raise movement.ManifestError("invalid_producer")
    generated = movement._timestamp(doc["generated_at_utc"])
    if not isinstance(doc["status"], str) or doc["status"] not in movement.REPORT_STATUSES:
        raise movement.ManifestError("invalid_status")
    if not isinstance(doc["decisions"], list) or len(doc["decisions"]) > MAX_DECISIONS:
        raise movement.ManifestError("too_many_clips")
    identities, paths = set(), set()
    for decision in doc["decisions"]:
        _validate_decision(decision)
        clip = decision["clip"]
        if clip["clip_id"] in identities or clip["relative_path"] in paths:
            raise movement.ManifestError("duplicate_clip")
        identities.add(clip["clip_id"])
        paths.add(clip["relative_path"])
        retained_proof = decision["decision"] == "upload" and decision["reason"] in {
            "peer_coverage_required",
            "peer_analysis_pending",
        }
        if decision["decision"] in {"skip", "defer"} or retained_proof:
            guard_end = movement._timestamp(decision["guard"]["end_utc"], nullable=True)
            completed = movement._timestamp(
                decision["visual"]["analysis_completed_at_utc"], nullable=True
            )
            if (
                guard_end is None
                or (
                    (
                        decision["decision"] == "skip"
                        or decision["reason"] == "peer_coverage_required"
                    )
                    and completed is None
                )
                or guard_end > generated
                or (completed is not None and completed > generated)
            ):
                raise movement.ManifestError("future_or_missing_proof")
    return doc


def _validate_decision(decision: Any) -> None:
    movement._keys(decision, DECISION_KEYS)
    movement._validate_clip(decision["clip"])
    if (
        not isinstance(decision["decision"], str)
        or decision["decision"] not in {"upload", "skip", "defer"}
        or not isinstance(decision["reason"], str)
        or decision["reason"] not in REASONS
    ):
        raise movement.ManifestError("invalid_decision")
    until = movement._timestamp(decision["defer_until_utc"], nullable=True)
    if (decision["decision"] == "defer") != (until is not None):
        raise movement.ManifestError("invalid_deferral")
    guard, visual = decision["guard"], decision["visual"]
    movement._keys(guard, GUARD_KEYS)
    movement._keys(
        visual,
        VISUAL_KEYS | ({"reason"} if isinstance(visual, dict) and "reason" in visual else set()),
    )
    # Optional diagnostics preserve old producers. Unknown/new producer categories are
    # replaced before persistence; their presence can never confer quiet authority.
    if (
        "reason" in visual
        and visual["reason"] is not None
        and (not isinstance(visual["reason"], str) or visual["reason"] not in VISUAL_REASONS)
    ):
        visual["reason"] = "unrecognized_reason"
    for key in ("start_utc", "end_utc"):
        movement._timestamp(guard[key], nullable=True)
    for key in GUARD_KEYS - {"start_utc", "end_utc"}:
        cap = 86_400_000 if key.endswith("_ms") else 1_000_000
        movement._integer(guard[key], cap)
    if type(visual["algorithm_version"]) is not int or visual["algorithm_version"] != 2:
        raise movement.ManifestError("unsupported_visual_algorithm")
    if (
        visual["motion_metric"] != "adjacent_rolling6_coherent3x3_v1"
        or visual["roi_profile"] != "dashcam_osd_bottom4_v1"
    ):
        raise movement.ManifestError("unsupported_visual_metric")
    if not isinstance(visual["status"], str) or visual["status"] not in VISUAL_STATUSES:
        raise movement.ManifestError("invalid_visual_status")
    for key in {"input_eos", "decoder_eos"}:
        if type(visual[key]) is not bool:
            raise movement.ManifestError("invalid_visual_eos")
    for key in {"first_pts_us", "last_pts_us"}:
        if visual[key] is not None:
            movement._integer(visual[key], 2**63 - 1)
    movement._timestamp(visual["analysis_completed_at_utc"], nullable=True)
    quality_numbers = {"min_mean_luma", "max_mean_luma", "max_dark_fraction", "max_bright_fraction"}
    for key in quality_numbers:
        if visual[key] is None:
            raise movement.ManifestError("invalid_visual_quality")
        movement._number(visual[key], 1.0 if key.endswith("fraction") else 255.0)
    for key in VISUAL_KEYS - {
        "algorithm_version",
        "status",
        "input_eos",
        "decoder_eos",
        "first_pts_us",
        "last_pts_us",
        "analysis_completed_at_utc",
        "motion_metric",
        "roi_profile",
        *quality_numbers,
    }:
        movement._integer(visual[key], 86_400_000 if key.endswith("_ms") else 1_000_000)


def _quiet_visual(visual: dict[str, Any], duration: int) -> bool:
    return bool(
        visual["status"] == "quiet"
        and visual.get("reason") is None
        and visual["input_eos"]
        and visual["decoder_eos"]
        and visual["input_frames"] == visual["decoded_frames"] == visual["sampled_frames"]
        and visual["sampled_frames"] >= 20
        and visual["motion_frames"] == 0
        and visual["grid_width"] == 160
        and visual["grid_height"] == 90
        and visual["rolling_frames"] == 6
        and visual["roi_rows"] == 86
        and visual["max_coherent_pixels"] < 16
        and visual["max_rolling_coherent_pixels"] < 128
        and visual["max_consecutive_coherent_frames"] < 2
        and 40 <= visual["min_mean_luma"] <= visual["max_mean_luma"] <= 215
        and visual["max_dark_fraction"] <= 0.55
        and visual["max_bright_fraction"] <= 0.4
        and visual["min_contrast_span"] >= 24
        and 0 < visual["max_gap_ms"] <= 150
        and (visual["sampled_frames"] - 1) * visual["max_gap_ms"] >= visual["covered_ms"] - 150
        and abs(visual["duration_ms"] - duration) <= 150
        and duration - 150 <= visual["covered_ms"] <= duration + 150
        and visual["first_pts_us"] is not None
        and visual["last_pts_us"] is not None
        and abs(visual["last_pts_us"] - visual["first_pts_us"] - duration * 1000) <= 150_000
    )


def _guard_off(guard: dict[str, Any]) -> bool:
    first = movement._timestamp(guard["start_utc"])
    last = movement._timestamp(guard["end_utc"])
    return bool(
        guard["window_ms"] == round((last - first).total_seconds() * 1000) > 0
        and guard["acc_off_ms"] == guard["window_ms"]
        and guard["unknown_ms"] == 0
        and 0 < guard["max_gap_ms"] <= 7500
        and guard["observation_count"] >= 2
        and (guard["observation_count"] + 1) * guard["max_gap_ms"] >= guard["window_ms"]
        and guard["on_samples"] == guard["unknown_samples"] == guard["positive_speed_samples"] == 0
        and guard["segment_count"] == 1
    )


def _no_movement(clip: dict[str, Any]) -> bool:
    evidence = clip["evidence"]
    return bool(
        evidence["acc_on_ms"] == evidence["moving_samples"] == evidence["gps_moving_samples"] == 0
        and (evidence["max_speed_kmh"] is None or evidence["max_speed_kmh"] == 0)
        and (evidence["max_gps_speed_m_s"] is None or evidence["max_gps_speed_m_s"] <= 1.5)
    )


def _eligible_quiet(decision: dict[str, Any], *, witness: bool = False) -> bool:
    """Check a clip's own evidence, independently of its opposite-lens coverage."""
    try:
        _validate_decision(decision)
        clip, guard, visual = decision["clip"], decision["guard"], decision["visual"]
        start, end = movement._timestamp(clip["start_utc"]), movement._timestamp(clip["end_utc"])
        first, last = movement._timestamp(guard["start_utc"]), movement._timestamp(guard["end_utc"])
        completed = movement._timestamp(visual["analysis_completed_at_utc"])
        duration = clip["duration_ms"]
        allowed = {("skip", "parked_visual_quiet")}
        if witness:
            allowed.update({("upload", "peer_coverage_required"), ("defer", "analysis_pending")})
        if (
            (decision["decision"], decision["reason"]) not in allowed
            or not clip["relative_path"].startswith("DCIM/Video/")
            or clip["locked"]
            or clip["fingerprint"] is None
            or clip["size_bytes"] <= 0
            or clip["time_quality"] != "bounded"
            or clip["analysis_status"] != "complete"
            or movement._effective_class(clip) != "acc_off_candidate"
            or set(clip["reasons"]) & UNSAFE_CLIP_REASONS
            or duration is None
            or not 30_000 <= duration <= 120_000
            or abs((end - start).total_seconds() * 1000 - duration) > 250
            or first > start - timedelta(seconds=120)
            or last < end + timedelta(seconds=120)
            or not _guard_off(guard)
            or not _quiet_visual(visual, duration)
            or completed < end
        ):
            return False
        return _no_movement(clip)
    except (movement.ManifestError, KeyError, TypeError, ValueError):
        return False


def eligible_skip(decision: dict[str, Any]) -> bool:
    """Only an explicit final skip with complete own evidence may be omitted."""
    return _eligible_quiet(decision)


def _quiet_witness(decision: dict[str, Any]) -> bool:
    return _eligible_quiet(decision, witness=True)


def eligible_defer(
    decision: dict[str, Any], *, now: datetime, native_remaining_s: float | None
) -> bool:
    """A temporary analysis opportunity, never authority to claim footage backed up."""
    try:
        _validate_decision(decision)
        clip, guard, visual = decision["clip"], decision["guard"], decision["visual"]
        start, end = movement._timestamp(clip["start_utc"]), movement._timestamp(clip["end_utc"])
        until = movement._timestamp(decision["defer_until_utc"])
        first, last = movement._timestamp(guard["start_utc"]), movement._timestamp(guard["end_utc"])
        duration = round((end - start).total_seconds() * 1000)
        unsafe = UNSAFE_CLIP_REASONS - {"file_unstable", "duration_unknown"}
        return bool(
            decision["decision"] == "defer"
            and decision["reason"] in {"awaiting_post_guard", "file_settling", "analysis_pending"}
            and clip["relative_path"].startswith("DCIM/Video/")
            and not clip["locked"]
            and clip["size_bytes"] > 0
            and movement._effective_class(clip) == "acc_off_candidate"
            and not set(clip["reasons"]) & unsafe
            and _no_movement(clip)
            and 30_000 <= duration <= 120_000
            and end <= now < until
            and until == end + timedelta(seconds=MAX_DEFER_SECONDS)
            and type(native_remaining_s) in (int, float)
            and math.isfinite(native_remaining_s)
            and native_remaining_s > (until - now).total_seconds() + DEFER_SLEEP_RESERVE_SECONDS
            and first <= start - timedelta(seconds=120)
            and end <= last <= now
            and _guard_off(guard)
            and visual["motion_frames"] == 0
            and visual["max_coherent_pixels"] < 16
            and visual["max_rolling_coherent_pixels"] < 128
            and visual["max_consecutive_coherent_frames"] < 2
            and (
                (
                    visual["status"] == "not_analyzed"
                    and visual.get("reason") in {None, "analysis_pending"}
                    and visual["input_frames"]
                    == visual["decoded_frames"]
                    == visual["sampled_frames"]
                    == 0
                    and not visual["input_eos"]
                    and not visual["decoder_eos"]
                    and visual["first_pts_us"] is None
                    and visual["last_pts_us"] is None
                    and visual["analysis_completed_at_utc"] is None
                )
                or (
                    visual["status"] == "quiet"
                    and clip["duration_ms"] is not None
                    and _quiet_visual(visual, clip["duration_ms"])
                    and abs(duration - clip["duration_ms"]) <= 250
                    and movement._timestamp(visual["analysis_completed_at_utc"]) >= end
                )
            )
        )
    except (movement.ManifestError, KeyError, TypeError, ValueError):
        return False


def _pending_witness(
    decision: dict[str, Any], *, now: datetime, native_remaining_s: float | None
) -> bool:
    if _quiet_witness(decision):
        return True
    if decision["decision"] == "upload" and decision["reason"] == "peer_analysis_pending":
        # A retained boundary clip may support its neighbour's bounded opportunity,
        # but never gets a new deadline or becomes a deferred target itself.
        try:
            until = movement._timestamp(decision["clip"]["end_utc"]) + timedelta(
                seconds=MAX_DEFER_SECONDS
            )
        except (movement.ManifestError, KeyError, TypeError, ValueError):
            return False
        decision = {
            **decision,
            "decision": "defer",
            "reason": "analysis_pending",
            "defer_until_utc": until.isoformat(),
        }
    return eligible_defer(decision, now=now, native_remaining_s=native_remaining_s)


def _relative_path(item: RemoteFile, source: str) -> str | None:
    directory = item.directory or source
    if directory != source or PurePosixPath(directory).name != "Video":
        return None
    path = f"DCIM/Video/{item.name}"
    return path if movement._PATH.fullmatch(path) else None


def _matched_proofs(
    doc: dict[str, Any],
    remote: list[RemoteFile],
    source: str,
    accept: Callable[[dict], bool],
) -> list[dict]:
    live = {_relative_path(item, source): item for item in remote}
    return [
        decision
        for decision in doc["decisions"]
        if (item := live.get(decision["clip"]["relative_path"]))
        and accept(decision)
        and item.size == decision["clip"]["size_bytes"]
        and item.mtime == decision["clip"]["mtime_ms"] // 1000
    ]


def _overlapping_entries(doc: dict[str, Any], decision: dict) -> list[dict]:
    clip = decision["clip"]
    start, end = movement._timestamp(clip["start_utc"]), movement._timestamp(clip["end_utc"])
    return [
        entry
        for entry in doc["decisions"]
        if entry["clip"]["relative_path"] != clip["relative_path"]
        and entry["clip"]["start_utc"] is not None
        and entry["clip"]["end_utc"] is not None
        and movement._timestamp(entry["clip"]["start_utc"]) < end
        and movement._timestamp(entry["clip"]["end_utc"]) > start
    ]


def _overlapping_peers(doc: dict[str, Any], decision: dict) -> list[dict]:
    return [
        entry
        for entry in _overlapping_entries(doc, decision)
        if entry["clip"]["camera_key"] != decision["clip"]["camera_key"]
    ]


def _covers(peers: list[dict], start: datetime, end: datetime) -> bool:
    through = start
    for lower, upper in sorted(
        (
            movement._timestamp(peer["clip"]["start_utc"]),
            movement._timestamp(peer["clip"]["end_utc"]),
        )
        for peer in peers
    ):
        if lower > through:
            return False
        through = max(through, upper)
    return through >= end


def _paired_candidates(
    doc: dict[str, Any],
    remote: list[RemoteFile],
    source: str,
    accept: Callable[[dict], bool] | None = None,
    *,
    witness_accept: Callable[[dict], bool] | None = None,
) -> list[dict]:
    matched = _matched_proofs(doc, remote, source, accept or eligible_skip)
    witnesses = _matched_proofs(doc, remote, source, witness_accept or accept or _quiet_witness)
    witness_paths = {entry["clip"]["relative_path"] for entry in witnesses}
    # Every overlapping opposite-lens perspective must be present, independently quiet
    # and identity matched. Missing/locked/unknown peers protect the whole interval.
    paired = []
    by_path = {entry["clip"]["relative_path"]: entry for entry in doc["decisions"]}
    for decision in matched:
        clip = decision["clip"]
        start, end = movement._timestamp(clip["start_utc"]), movement._timestamp(clip["end_utc"])
        local_start = datetime.fromisoformat(clip["local_start"])
        local_end = local_start + (end - start)
        unknown_peer = False
        for item in remote:
            parent = PurePosixPath(item.directory or source).name
            path = f"DCIM/{parent}/{item.name}"
            match = movement._PATH.fullmatch(path)
            if match is None or path == clip["relative_path"]:
                continue
            peer_start = datetime.strptime(match[2], "%Y%m%d%H%M%S")
            entry = by_path.get(path)
            if local_start - timedelta(seconds=120) <= peer_start < local_end and (
                entry is None
                or entry["clip"]["start_utc"] is None
                or entry["clip"]["end_utc"] is None
            ):
                unknown_peer = True
                break
        if unknown_peer:
            continue
        # An uncertain/active duplicate interval on this lens also remains protected.
        # Adjacent fully proved segments may overlap by a few decoded frames.
        if any(
            entry["clip"]["relative_path"] != clip["relative_path"]
            and entry["clip"]["camera_key"] == clip["camera_key"]
            and entry["clip"]["relative_path"] not in witness_paths
            and entry["clip"]["start_utc"] is not None
            and entry["clip"]["end_utc"] is not None
            and movement._timestamp(entry["clip"]["start_utc"]) < end
            and movement._timestamp(entry["clip"]["end_utc"]) > start
            for entry in doc["decisions"]
        ):
            continue
        peers = _overlapping_peers(doc, decision)
        if any(peer["clip"]["relative_path"] not in witness_paths for peer in peers) or not _covers(
            peers, start, end
        ):
            continue
        paired.append(decision)
    return paired


def _required_quiet_proofs(doc: dict[str, Any], selected: list[dict]) -> list[dict]:
    """Hash every perspective needed by a skip, including retained boundary witnesses."""
    paths = {entry["clip"]["relative_path"] for entry in selected}
    for decision in selected:
        paths.update(peer["clip"]["relative_path"] for peer in _overlapping_entries(doc, decision))
    return [entry for entry in doc["decisions"] if entry["clip"]["relative_path"] in paths]


async def _read_policy(address: str, status_path: str) -> dict[str, Any] | None:
    from app.ingest.obd_transfer import _safe_remote_path

    path = _safe_remote_path(
        str(PurePosixPath(status_path).parent / "movement/transfer-policy.json")
    )
    prefix = "dashcam-transfer-policy:"
    command = (
        f"if [ -f '{path}' ] && [ ! -L '{path}' ]; then "
        f"printf '{prefix}'; head -c {MAX_BYTES + 1} '{path}'; fi; exit 0"
    )
    raw = await adb.shell(address, command, timeout=3.0)
    if not raw:
        return None
    if not raw.startswith(prefix):
        raise movement.ManifestError("invalid_response")
    return validate_policy_manifest(raw[len(prefix) :])


async def _verify_fingerprints(address: str, source: str, decisions: list[dict]) -> set[str]:
    """Read only three bounded samples on the camera; return hashes, never footage."""
    from app.ingest.obd_transfer import _safe_remote_path

    directory = _safe_remote_path(source)
    selected = decisions[:MAX_FINGERPRINT_CHECKS]
    commands = []
    for index, decision in enumerate(selected):
        clip = decision["clip"]
        path = _safe_remote_path(f"{directory}/{PurePosixPath(clip['relative_path']).name}")
        size, mtime = clip["size_bytes"], clip["mtime_ms"] // 1000
        reads = [f"printf 'sha256-sampled-v1\\000%d\\000' {size}"]
        for offset in dict.fromkeys((0, max(0, (size - 16384) // 2), max(0, size - 16384))):
            length = min(16384, size - offset)
            reads.append(
                f"head -c {length} '{path}'"
                if offset == 0
                else f"tail -c +{offset + 1} '{path}' 2>/dev/null | head -c {length}"
            )
        check = f"[ \"$(stat -c '%s %Y' '{path}' 2>/dev/null)\" = '{size} {mtime}' ]"
        commands.append(
            f"if [ -f '{path}' ] && [ ! -L '{path}' ] && {check}; then "
            f"v=$({{ {'; '.join(reads)}; }} | sha256sum); "
            f"if {check}; then printf '{index} %s\\n' \"${{v%% *}}\"; fi; fi"
        )
    if not commands:
        return set()
    reply = await adb.shell(address, "; ".join(commands), timeout=5.0)
    verified = set()
    for line in reply.splitlines():
        match = re.fullmatch(r"(\d{1,3}) ([0-9a-f]{64})", line)
        if match and int(match[1]) < len(selected):
            entry = selected[int(match[1])]
            if match[2] == entry["clip"]["fingerprint"]["digest"]:
                verified.add(entry["clip"]["clip_id"])
    return verified


async def filter_inventory(
    address: str,
    status_path: str,
    remote: list[RemoteFile],
    source: str,
    logger: dict[str, Any] | None,
    *,
    enabled: bool,
    native_remaining_s: float | None = None,
    fingerprint_budget: int = MAX_FINGERPRINT_CHECKS,
) -> FilterResult:
    """An optional proof failure copies normally; only committed audit records can exclude."""
    from app.ingest import clip_exclusions

    started = asyncio.get_running_loop().time()
    checks = 0
    doc, selected, deferred, state = None, [], [], "disabled"
    try:
        if enabled:
            state = "unsupported"
            if isinstance(logger, dict) and CAPABILITY in (logger.get("capabilities") or []):
                logger_age = (
                    datetime.now(UTC) - movement._timestamp(logger.get("updated_at_utc"))
                ).total_seconds()
                if not -30 <= logger_age <= 30 or logger.get("transfer_policy_state") not in {
                    "ready",
                    "scanning",
                    "waiting",
                }:
                    raise movement.ManifestError("logger_policy_not_ready")
                if logger.get("movement_state") != "ready":
                    raise movement.ManifestError("logger_storage_not_ready")
                if logger.get("transfer_policy_enabled") is not True:
                    raise movement.ManifestError("logger_policy_not_enabled")
                async with asyncio.timeout(POLICY_READ_TIMEOUT_S):
                    doc = await _read_policy(address, status_path)
                    state = "missing" if doc is None else "invalid"
                    if doc is not None:
                        age = (
                            datetime.now(UTC) - movement._timestamp(doc["generated_at_utc"])
                        ).total_seconds()
                        if not -30 <= age <= MAX_AGE_SECONDS:
                            state = "stale"
                        elif not doc["enabled"]:
                            state = "disabled"
                        elif doc["status"] == "ready" and not doc["truncated"] and doc["card_id"]:
                            card = await movement.read_remote_card_id(address, source)
                            state = "card_mismatch"
                            if card == doc["card_id"]:
                                if logger.get("transfer_policy_state") == "ready":
                                    selected = _paired_candidates(doc, remote, source)
                                now = datetime.now(UTC)
                                deferred = _paired_candidates(
                                    doc,
                                    remote,
                                    source,
                                    lambda entry: eligible_defer(
                                        entry, now=now, native_remaining_s=native_remaining_s
                                    ),
                                    witness_accept=lambda entry: _pending_witness(
                                        entry, now=now, native_remaining_s=native_remaining_s
                                    ),
                                )
                                state = "ready"
                if selected:
                    async with asyncio.timeout(FINGERPRINT_TIMEOUT_S):
                        checking = _required_quiet_proofs(doc, selected)[
                            : max(0, min(MAX_FINGERPRINT_CHECKS, fingerprint_budget))
                        ]
                        checks = len(checking)
                        verified = await _verify_fingerprints(address, source, checking)
                    # Retained boundary clips can supply a neighbour's quiet perspective,
                    # but every target and witness needs its own current byte identity.
                    # Keep the full report so an unverified/unsafe overlap cannot disappear.
                    selected = _paired_candidates(
                        doc,
                        remote,
                        source,
                        lambda entry: eligible_skip(entry) and entry["clip"]["clip_id"] in verified,
                        witness_accept=lambda entry: (
                            _quiet_witness(entry) and entry["clip"]["clip_id"] in verified
                        ),
                    )
    except asyncio.CancelledError:
        raise
    except Exception:
        selected, deferred, state = [], [], "invalid"
        log.warning("camera quiet-footage proof unavailable; copying normally")
    # Report refusal categories separately from exclusion authority. This also works
    # when zero clips qualify and avoids writing diagnostic rows into the media index.
    try:
        async with asyncio.timeout(DIAGNOSTICS_TIMEOUT_S):
            status = movement.get_movement_status()
            # Both helpers shield their retained file worker. A stalled local state
            # volume may finish later, while the independent exclusion audit proceeds.
            await movement.load_status(status)
            status.update_policy_analysis(analysis_summary(doc, state))
            await movement._persist_async(status)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass  # Optional diagnostics must not hold up or veto a backup.
    try:
        async with asyncio.timeout(AUDIT_TIMEOUT_S):
            await clip_exclusions.record_evaluation(
                manifest=doc,
                excluded=selected,
                status=state,
                considered_count=len(remote),
                considered_bytes=sum(item.size for item in remote),
                policy_enabled=enabled,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("camera quiet-footage policy could not be proved; copying normally")
        return FilterResult(remote, fingerprint_checks=checks)
    if deferred:
        now = datetime.now(UTC)
        age = (now - movement._timestamp(doc["generated_at_utc"])).total_seconds()
        logger_age = (now - movement._timestamp(logger["updated_at_utc"])).total_seconds()
        remaining = (
            native_remaining_s - (asyncio.get_running_loop().time() - started)
            if type(native_remaining_s) in (int, float)
            else None
        )
        deferred_ids = {entry["clip"]["clip_id"] for entry in deferred}
        deferred = (
            _paired_candidates(
                doc,
                remote,
                source,
                lambda entry: (
                    entry["clip"]["clip_id"] in deferred_ids
                    and eligible_defer(entry, now=now, native_remaining_s=remaining)
                ),
                witness_accept=lambda entry: _pending_witness(
                    entry, now=now, native_remaining_s=remaining
                ),
            )
            if -30 <= age <= MAX_AGE_SECONDS and -30 <= logger_age <= 30
            else []
        )
    skipped = {entry["clip"]["relative_path"] for entry in selected}
    held = {
        entry["clip"]["relative_path"]: movement._timestamp(entry["defer_until_utc"])
        for entry in deferred
    }
    return FilterResult(
        [item for item in remote if _relative_path(item, source) not in skipped | held.keys()],
        [
            (item, held[_relative_path(item, source)])
            for item in remote
            if _relative_path(item, source) in held
        ],
        [item for item in remote if _relative_path(item, source) in skipped],
        fingerprint_checks=checks,
    )
