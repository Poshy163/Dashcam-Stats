"""Bounded, report-only camera-side movement manifests.

These are observations, never transfer or retention instructions. Invalid, stale,
unmatched and missing observations leave the ordinary footage plan unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import get_config
from app.ingest import adb
from app.ingest.models import RemoteFile

CAPABILITY = "clip_movement_manifest_v1"
MAX_BYTES = 512 * 1024
MAX_CLIPS = 512
MAX_STORED_BYTES = MAX_BYTES + 64 * 1024
READ_TIMEOUT_SECONDS = 4.0
INSPECTION_TIMEOUT_SECONDS = 6.0
MAX_AGE_SECONDS = 600
MAX_FUTURE_SECONDS = 120
CLASSIFICATIONS = ("moving_observed", "sampled_stationary", "acc_off_candidate", "unknown")
REPORT_STATUSES = {
    "ready",
    "directory_required",
    "directory_unavailable",
    "storage_unavailable",
    "scan_failed",
}
REASONS = {
    "validated_speed_positive",
    "gps_confident_movement",
    "zero_speed_samples",
    "acc_off_observed",
    "locked_clip",
    "orphan_partial",
    "incomplete_time_bounds",
    "no_observation_coverage",
    "observation_gap",
    "clock_ambiguous",
    "file_unstable",
    "file_unreadable",
    "duration_unknown",
    "report_only",
}
TOP_KEYS = {
    "schema_version",
    "source_id",
    "card_id",
    "session_id",
    "boot_id",
    "generated_at_utc",
    "producer_version",
    "classifier_version",
    "report_only",
    "truncated",
    "coverage_start_utc",
    "coverage_end_utc",
    "status",
    "clips",
}
CLIP_KEYS = {
    "clip_id",
    "relative_path",
    "camera_key",
    "locked",
    "size_bytes",
    "mtime_ms",
    "fingerprint",
    "local_start",
    "timezone",
    "start_utc",
    "end_utc",
    "duration_ms",
    "time_quality",
    "classification",
    "reasons",
    "analysis_status",
    "coverage",
    "evidence",
}
COVERAGE_KEYS = {"window_ms", "acc_ms", "speed_ms", "gps_ms", "unknown_ms", "max_gap_ms"}
EVIDENCE_KEYS = {
    "acc_on_ms",
    "acc_off_ms",
    "valid_speed_samples",
    "moving_samples",
    "max_speed_kmh",
    "gps_speed_samples",
    "gps_moving_samples",
    "max_gps_speed_m_s",
}
_PATH = re.compile(r"^DCIM/(Video/|LockVideo/|pre_)(\d{14})_camera_([01])\.ts$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_PREFIX = "dashcam-movement-file:"
_MISSING = "dashcam-movement-missing"


class ManifestError(ValueError):
    """A fixed-category contract failure; raw values never enter the public status."""


def _integer(value: Any, maximum: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ManifestError("invalid_number")
    return value


def _number(value: Any, maximum: float) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= maximum
    ):
        raise ManifestError("invalid_number")


def _timestamp(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ManifestError("invalid_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError("invalid_time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ManifestError("invalid_time")
    return parsed.astimezone(UTC)


def _identifier(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ManifestError("invalid_identifier") from exc


def _keys(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ManifestError("invalid_fields")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestError("duplicate_json_key")
        result[key] = value
    return result


def _no_constant(_: str) -> None:
    raise ManifestError("invalid_number")


def validate_manifest(raw: str) -> dict[str, Any]:
    """Strict wire validation, including privacy bounds; no arbitrary nested data survives."""
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ManifestError("too_large")
    try:
        doc = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise ManifestError("invalid_json") from exc
    _keys(doc, TOP_KEYS)
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        raise ManifestError("unsupported_schema")
    if type(doc["classifier_version"]) is not int or doc["classifier_version"] != 1:
        raise ManifestError("unsupported_classifier")
    if doc["report_only"] is not True or type(doc["truncated"]) is not bool:
        raise ManifestError("report_only_required")
    for key in ("source_id", "session_id", "boot_id"):
        _identifier(doc[key])
    if doc["card_id"] is not None and (
        not isinstance(doc["card_id"], str) or not _HEX.fullmatch(doc["card_id"])
    ):
        raise ManifestError("invalid_card_identity")
    if not isinstance(doc["producer_version"], str) or not re.fullmatch(
        r"\d{1,3}\.\d{1,3}\.\d{1,3}(?:[-+][A-Za-z0-9.]{1,20})?", doc["producer_version"]
    ):
        raise ManifestError("invalid_producer")
    _timestamp(doc["generated_at_utc"])
    start = _timestamp(doc["coverage_start_utc"], nullable=True)
    end = _timestamp(doc["coverage_end_utc"], nullable=True)
    if (start is None) != (end is None) or (start and end and end < start):
        raise ManifestError("invalid_coverage_time")
    if not isinstance(doc["status"], str) or doc["status"] not in REPORT_STATUSES:
        raise ManifestError("invalid_status")
    if not isinstance(doc["clips"], list) or len(doc["clips"]) > MAX_CLIPS:
        raise ManifestError("too_many_clips")
    if doc["clips"] and (doc["card_id"] is None or doc["status"] != "ready"):
        raise ManifestError("card_identity_required")
    paths, identifiers = set(), set()
    for clip in doc["clips"]:
        _validate_clip(clip)
        if clip["relative_path"] in paths or clip["clip_id"] in identifiers:
            raise ManifestError("duplicate_clip")
        paths.add(clip["relative_path"])
        identifiers.add(clip["clip_id"])
    return doc


def _validate_clip(clip: Any) -> None:
    _keys(clip, CLIP_KEYS)
    _identifier(clip["clip_id"])
    path = clip["relative_path"]
    match = _PATH.fullmatch(path) if isinstance(path, str) else None
    if match is None or clip["camera_key"] != f"camera_{match[3]}":
        raise ManifestError("invalid_clip_path")
    if type(clip["locked"]) is not bool or clip["locked"] != path.startswith("DCIM/LockVideo/"):
        raise ManifestError("invalid_locked_state")
    _integer(clip["size_bytes"], 1024**4)
    _integer(clip["mtime_ms"], 4_102_444_800_000)
    if clip["fingerprint"] is not None:
        fingerprint = clip["fingerprint"]
        _keys(fingerprint, {"algorithm", "sample_bytes", "digest"})
        if (
            fingerprint["algorithm"] != "sha256-sampled-v1"
            or type(fingerprint["sample_bytes"]) is not int
            or fingerprint["sample_bytes"] != 16384
            or not isinstance(fingerprint["digest"], str)
            or not _HEX.fullmatch(fingerprint["digest"])
        ):
            raise ManifestError("invalid_fingerprint")
    if not isinstance(clip["local_start"], str) or len(clip["local_start"]) > 32:
        raise ManifestError("invalid_local_time")
    try:
        local = datetime.fromisoformat(clip["local_start"])
        if local.tzinfo is not None or local.strftime("%Y%m%d%H%M%S") != match[2]:
            raise ValueError
    except ValueError as exc:
        raise ManifestError("invalid_local_time") from exc
    if not isinstance(clip["timezone"], str) or not re.fullmatch(
        r"[A-Za-z0-9_+:/.-]{1,80}", clip["timezone"]
    ):
        raise ManifestError("invalid_timezone")
    start = _timestamp(clip["start_utc"], nullable=True)
    end = _timestamp(clip["end_utc"], nullable=True)
    if start and end and not 0 < (end - start).total_seconds() <= 86400:
        raise ManifestError("invalid_clip_interval")
    if clip["duration_ms"] is not None:
        _integer(clip["duration_ms"], 86_400_000, minimum=1)
    if (
        not isinstance(clip["time_quality"], str)
        or clip["time_quality"] not in {"unknown", "bounded"}
        or clip["classification"] not in CLASSIFICATIONS
        or not isinstance(clip["analysis_status"], str)
        or clip["analysis_status"] not in {"complete", "unknown"}
    ):
        raise ManifestError("invalid_classification")
    if (
        not isinstance(clip["reasons"], list)
        or len(clip["reasons"]) > len(REASONS)
        or any(not isinstance(reason, str) or reason not in REASONS for reason in clip["reasons"])
        or len(set(clip["reasons"])) != len(clip["reasons"])
    ):
        raise ManifestError("invalid_reason")
    _keys(clip["coverage"], COVERAGE_KEYS)
    for number in clip["coverage"].values():
        _integer(number, 86_400_000)
    _keys(clip["evidence"], EVIDENCE_KEYS)
    for key in ("acc_on_ms", "acc_off_ms"):
        _integer(clip["evidence"][key], 86_400_000)
    for key in ("valid_speed_samples", "moving_samples", "gps_speed_samples", "gps_moving_samples"):
        _integer(clip["evidence"][key], 1_000_000)
    _number(clip["evidence"]["max_speed_kmh"], 255)
    _number(clip["evidence"]["max_gps_speed_m_s"], 150)
    if path.startswith("DCIM/pre_") and (
        clip["classification"] != "unknown" or clip["time_quality"] != "unknown"
    ):
        raise ManifestError("orphan_must_be_unknown")


def _time_agrees(clip: dict[str, Any]) -> bool:
    if clip["time_quality"] != "bounded" or clip["analysis_status"] != "complete":
        return False
    start, end = (
        _timestamp(clip["start_utc"], nullable=True),
        _timestamp(clip["end_utc"], nullable=True),
    )
    if not start or not end:
        return False
    local = datetime.fromisoformat(clip["local_start"])
    name = clip["timezone"]
    try:
        offset = re.fullmatch(r"(?:GMT|UTC)([+-])(\d{2}):(\d{2})", name)
        if offset:
            seconds = (int(offset[2]) * 60 + int(offset[3])) * 60
            if int(offset[2]) > 18 or int(offset[3]) > 59:
                return False
            zone = timezone(timedelta(seconds=seconds * (1 if offset[1] == "+" else -1)))
        else:
            zone = ZoneInfo(name)
        expected = local.replace(tzinfo=zone)
        if expected.utcoffset() != local.replace(tzinfo=zone, fold=1).utcoffset():
            return False
        if expected.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local:
            return False
        if abs((expected.astimezone(UTC) - start).total_seconds()) > 2:
            return False
    except (ValueError, ZoneInfoNotFoundError):
        return False
    window = clip["coverage"]["window_ms"]
    return window > 0 and abs((end - start).total_seconds() * 1000 - window) <= 2000


def _effective_class(clip: dict[str, Any]) -> str:
    if not _time_agrees(clip):
        return "unknown"
    coverage, evidence = clip["coverage"], clip["evidence"]
    window = coverage["window_ms"]
    if any(coverage[key] > window for key in ("acc_ms", "speed_ms", "gps_ms", "unknown_ms")):
        return "unknown"
    if evidence["acc_on_ms"] + evidence["acc_off_ms"] > coverage["acc_ms"]:
        return "unknown"
    if (
        evidence["moving_samples"] > evidence["valid_speed_samples"]
        or evidence["gps_moving_samples"] > evidence["gps_speed_samples"]
    ):
        return "unknown"
    classification = clip["classification"]
    # Android counts qualifying GPS *pairs*, so one paired interval already requires
    # two fresh fixes. Raw GPS jitter does not establish either movement or stillness.
    obd_positive = evidence["moving_samples"] > 0 and (
        evidence["max_speed_kmh"] is not None and evidence["max_speed_kmh"] > 0
    )
    gps_positive = (
        evidence["gps_moving_samples"] > 0
        and evidence["gps_speed_samples"] >= 2
        and evidence["max_gps_speed_m_s"] is not None
        and evidence["max_gps_speed_m_s"] > 1.5
    )
    positive = obd_positive or gps_positive
    if classification == "moving_observed":
        return classification if positive else "unknown"
    if (
        positive
        or evidence["moving_samples"] > 0
        or evidence["gps_moving_samples"] > 0
        or (evidence["max_speed_kmh"] is not None and evidence["max_speed_kmh"] > 0)
    ):
        return "unknown"
    if classification == "sampled_stationary":
        return (
            classification
            if evidence["valid_speed_samples"] > 0
            and evidence["max_speed_kmh"] == 0
            and coverage["speed_ms"] >= window * 0.9
            and coverage["max_gap_ms"] <= 10000
            else "unknown"
        )
    if classification == "acc_off_candidate":
        return (
            classification
            if coverage["acc_ms"] >= window * 0.9 and evidence["acc_off_ms"] >= window * 0.9
            else "unknown"
        )
    return "unknown"


def _empty_classes() -> dict[str, int]:
    return dict.fromkeys(CLASSIFICATIONS, 0)


def _manifest_summary(doc: dict[str, Any]) -> dict[str, Any]:
    counts, sizes = _empty_classes(), _empty_classes()
    reasons: Counter[str] = Counter()
    for clip in doc["clips"]:
        counts[clip["classification"]] += 1
        sizes[clip["classification"]] += clip["size_bytes"]
        reasons.update(clip["reasons"])
    return {
        "clip_count": len(doc["clips"]),
        "total_bytes": sum(sizes.values()),
        "class_counts": counts,
        "class_bytes": sizes,
        "reason_counts": dict(reasons),
    }


def inventory_summary(
    doc: dict[str, Any] | None,
    remote: list[RemoteFile],
    source: str,
    *,
    card_identity: str = "unknown",
) -> dict[str, Any]:
    """Match finalized directory, name, camera, size and second-resolution remote mtime."""
    clips = {clip["relative_path"]: clip for clip in doc["clips"]} if doc else {}
    counts, sizes = _empty_classes(), _empty_classes()
    matched = matched_bytes = mismatched = unmatched = 0
    for item in remote:
        parent = PurePosixPath(item.directory or source).name
        path = f"DCIM/{parent}/{item.name}" if parent in {"Video", "LockVideo"} else ""
        clip = clips.get(path)
        classification = "unknown"
        if clip is None:
            unmatched += 1
        elif (
            card_identity == "mismatch"
            or clip["size_bytes"] != item.size
            or clip["mtime_ms"] // 1000 != item.mtime
        ):
            mismatched += 1
        elif card_identity != "verified":
            unmatched += 1
        else:
            matched += 1
            matched_bytes += item.size
            classification = _effective_class(clip)
        counts[classification] += 1
        sizes[classification] += item.size
    return {
        "clip_count": len(remote),
        "total_bytes": sum(item.size for item in remote),
        "matched_clips": matched,
        "matched_bytes": matched_bytes,
        "unmatched_clips": unmatched,
        "identity_mismatches": mismatched,
        "class_counts": counts,
        "class_bytes": sizes,
        "card_identity": card_identity,
    }


def remote_manifest_path(status_path: str) -> str:
    from app.ingest.obd_transfer import _safe_remote_path

    return _safe_remote_path(str(PurePosixPath(status_path).parent / "movement" / "report.json"))


async def read_remote_manifest(address: str, status_path: str) -> str | None:
    path = remote_manifest_path(status_path)
    command = (
        f"if [ -f '{path}' ] && [ ! -L '{path}' ]; then "
        f"printf '{_PREFIX}'; head -c {MAX_BYTES + 1} '{path}'; "
        f"else printf '{_MISSING}'; fi; exit 0"
    )
    async with asyncio.timeout(READ_TIMEOUT_SECONDS):
        raw = await adb.shell(address, command, timeout=3.0)
    if raw == _MISSING:
        return None
    if not raw.startswith(_PREFIX):
        raise ManifestError("invalid_response")
    raw = raw[len(_PREFIX) :]
    if len(raw.encode("utf-8")) > MAX_BYTES:
        raise ManifestError("too_large")
    return raw


async def read_remote_card_id(address: str, source: str) -> str | None:
    """Resolve only the footage alias, never trust a guessed or persisted card token."""
    from app.ingest.obd_transfer import _safe_remote_path

    try:
        path = _safe_remote_path(source)
        async with asyncio.timeout(1.5):
            resolved = (await adb.shell(address, f"readlink -f '{path}'", timeout=1.0)).strip()
        matched = re.fullmatch(r"/storage/([A-Fa-f0-9-]{4,64})/DCIM/(?:Video|LockVideo)", resolved)
        return hashlib.sha256(matched[1].encode("utf-8")).hexdigest() if matched else None
    except Exception:
        return None


class MovementStatus:
    """One private bounded last observation, plus a cheap aggregate public projection."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.loaded = False
        self.doc: dict[str, Any] | None = None
        self.inventory: dict[str, Any] | None = None
        self.received_at: str | None = None
        self.checked_at: str | None = None
        self.error: str | None = None
        self.policy_analysis: dict[str, Any] | None = None
        self.persistence_error = False
        self.persisting = False
        self.restored = False
        self.load_task: asyncio.Task[None] | None = None
        self.persist_task: asyncio.Task[None] | None = None

    def load_once(self) -> None:
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
        # Local I/O must not hold the public projection's lock. A fresh observation
        # arriving while this old file is read always wins over the restored cache.
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_STORED_BYTES:
                return
            stored = json.loads(
                self.path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_pairs,
                parse_constant=_no_constant,
            )
            _keys(
                stored,
                {"schema_version", "received_at", "checked_at", "error", "manifest", "inventory"}
                | ({"policy_analysis"} if "policy_analysis" in stored else set()),
            )
            if type(stored["schema_version"]) is not int or stored["schema_version"] != 1:
                return
            doc = (
                validate_manifest(json.dumps(stored["manifest"]))
                if stored["manifest"] is not None
                else None
            )
            for key in ("received_at", "checked_at"):
                _timestamp(stored[key], nullable=True)
            if stored["error"] not in {
                None,
                "manifest_missing",
                "invalid_manifest",
                "read_timeout",
                "read_failed",
            }:
                return
            inventory = stored["inventory"]
            policy_analysis = stored.get("policy_analysis")
            if policy_analysis is not None:
                from app.ingest.parked_filter import validate_analysis_summary

                validate_analysis_summary(policy_analysis)
            if inventory is not None:
                _keys(inventory, set(inventory_summary(None, [], "")))
                for key, value in inventory.items():
                    if key == "card_identity":
                        if value not in {"unknown", "mismatch", "verified"}:
                            return
                    elif key in {"class_counts", "class_bytes"}:
                        _keys(value, set(CLASSIFICATIONS))
                        for count in value.values():
                            _integer(count, 2**63 - 1)
                    else:
                        _integer(value, 2**63 - 1)
            with self.lock:
                if self.policy_analysis is None:
                    self.policy_analysis = policy_analysis
                if self.checked_at is not None:
                    return
                self.doc, self.inventory = doc, inventory
                self.received_at, self.checked_at, self.error = (
                    stored["received_at"],
                    stored["checked_at"],
                    stored["error"],
                )
                self.restored = True
        except (OSError, ValueError, RecursionError, TypeError):
            return

    def observe(
        self, doc: dict[str, Any] | None, inventory: dict[str, Any], error: str | None
    ) -> None:
        self.load_once()
        self.update_memory(doc, inventory, error)
        self._persist()

    def update_memory(
        self, doc: dict[str, Any] | None, inventory: dict[str, Any], error: str | None
    ) -> None:
        """No I/O: a late persistence worker cannot overwrite a newer observation."""
        with self.lock:
            self.loaded = True
            self.restored = False
            now = datetime.now(UTC).isoformat()
            self.checked_at, self.error, self.inventory = now, error, copy.deepcopy(inventory)
            if doc is not None:
                self.doc, self.received_at = copy.deepcopy(doc), now

    def update_policy_analysis(self, summary: dict[str, Any]) -> None:
        from app.ingest.parked_filter import validate_analysis_summary

        validate_analysis_summary(summary)
        with self.lock:
            self.policy_analysis = copy.deepcopy(summary)

    def _persist(self) -> None:
        with self.lock:
            if self.persisting:
                return
            self.persisting = True
            envelope = copy.deepcopy(
                {
                    "schema_version": 1,
                    "received_at": self.received_at,
                    "checked_at": self.checked_at,
                    "error": self.error,
                    "manifest": self.doc,
                    "inventory": self.inventory,
                    "policy_analysis": self.policy_analysis,
                }
            )
        raw = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_STORED_BYTES:
            self.persistence_error = True
            self.persisting = False
            return
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.partial")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self.persistence_error = False
        except OSError:
            self.persistence_error = True
        finally:
            self.persisting = False
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        with self.lock:
            now = now or datetime.now(UTC)
            doc = self.doc
            age = (now - _timestamp(doc["generated_at_utc"])).total_seconds() if doc else None
            stale = self.restored or (
                age is not None and (age > MAX_AGE_SECONDS or age < -MAX_FUTURE_SECONDS)
            )
            state = "unavailable" if doc is None else "stale" if stale else "ready"
            if self.error:
                state = (
                    "invalid"
                    if self.error == "invalid_manifest"
                    else "unavailable"
                    if self.error == "manifest_missing"
                    else "error"
                )
            inventory = copy.deepcopy(self.inventory)
            analysis = copy.deepcopy(self.policy_analysis)
            if analysis is not None:
                from app.ingest.parked_filter import MAX_AGE_SECONDS as POLICY_MAX_AGE_SECONDS

                checked_age = (now - _timestamp(analysis["checked_at"])).total_seconds()
                generated = _timestamp(analysis["generated_at"], nullable=True)
                generated_age = (now - generated).total_seconds() if generated else None
                analysis["stale"] = bool(
                    checked_age < -30
                    or checked_age > POLICY_MAX_AGE_SECONDS
                    or generated_age is None
                    or generated_age < -MAX_FUTURE_SECONDS
                    or generated_age > POLICY_MAX_AGE_SECONDS
                )
            if inventory and (stale or self.error):
                inventory["class_counts"] = _empty_classes()
                inventory["class_bytes"] = _empty_classes()
                inventory["class_counts"]["unknown"] = inventory["clip_count"]
                inventory["class_bytes"]["unknown"] = inventory["total_bytes"]
            return {
                "report_only": True,
                "transfer_filtering": False,
                "state": state,
                "checked_at": self.checked_at,
                "received_at": self.received_at,
                "generated_at": doc["generated_at_utc"] if doc else None,
                "producer_version": doc["producer_version"] if doc else None,
                "report_status": doc["status"] if doc else None,
                "truncated": doc["truncated"] if doc else False,
                "manifest": _manifest_summary(doc) if doc else None,
                "inventory": inventory,
                "card_identity_verified": bool(
                    inventory
                    and inventory["card_identity"] == "verified"
                    and not stale
                    and not self.error
                ),
                "fingerprints_verified": False,
                "report_age_seconds": max(0, age) if age is not None else None,
                "freshness_limit_seconds": MAX_AGE_SECONDS,
                "retained_snapshot": doc is not None,
                "requires_live_verification": self.restored,
                "persistence_error": self.persistence_error,
                "error": self.error,
                "policy_analysis": analysis,
            }


_status: MovementStatus | None = None
_validation_task: asyncio.Task[dict[str, Any]] | None = None


def _consume_task(task: asyncio.Task) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


async def load_status(status: MovementStatus) -> None:
    if status.loaded:
        return
    if status.load_task is None or status.load_task.done():
        status.load_task = asyncio.create_task(asyncio.to_thread(status.load_once))
        status.load_task.add_done_callback(_consume_task)
    await asyncio.shield(status.load_task)


async def _validate_async(raw: str) -> dict[str, Any]:
    global _validation_task
    if _validation_task is not None and not _validation_task.done():
        raise RuntimeError("manifest validation is busy")
    _validation_task = asyncio.create_task(asyncio.to_thread(validate_manifest, raw))
    _validation_task.add_done_callback(_consume_task)
    return await asyncio.shield(_validation_task)


async def _persist_async(status: MovementStatus) -> None:
    if status.persist_task is None or status.persist_task.done():
        status.persist_task = asyncio.create_task(asyncio.to_thread(status._persist))
        status.persist_task.add_done_callback(_consume_task)
    await asyncio.wait_for(asyncio.shield(status.persist_task), timeout=1.0)


def get_movement_status() -> MovementStatus:
    global _status
    path = get_config().obd_dir / "movement" / "last-observation.json"
    if _status is None or _status.path != path:
        _status = MovementStatus(path)
    return _status


async def inspect_before_plan(
    address: str, status_path: str, remote: list[RemoteFile], source: str
) -> None:
    """Optional bounded read. Its result cannot alter the footage list or run outcome."""
    status = get_movement_status()
    try:
        async with asyncio.timeout(INSPECTION_TIMEOUT_SECONDS):
            await _inspect_before_plan(status, address, status_path, remote, source)
    except TimeoutError:
        status.update_memory(None, inventory_summary(None, remote, source), "read_timeout")


async def _inspect_before_plan(
    status: MovementStatus, address: str, status_path: str, remote: list[RemoteFile], source: str
) -> None:
    doc, error = None, None
    try:
        raw = await read_remote_manifest(address, status_path)
        if raw is None:
            error = "manifest_missing"
        else:
            doc = await _validate_async(raw)
    except TimeoutError:
        error = "read_timeout"
    except ManifestError:
        error = "invalid_manifest"
    except Exception:
        error = "read_failed"
    card_identity = "unknown"
    if doc is not None and doc["card_id"] is not None:
        current_card_id = await read_remote_card_id(address, source)
        if current_card_id is not None:
            card_identity = "verified" if current_card_id == doc["card_id"] else "mismatch"
    summary = inventory_summary(doc, remote, source, card_identity=card_identity)
    await load_status(status)
    status.update_memory(doc, summary, error)
    try:
        # A slow local state volume must not become a new footage admission gate.
        # The worker may finish its atomic write later; it holds no status lock
        # during fsync and admits at most one persistence operation at a time.
        await _persist_async(status)
    except Exception:
        # Diagnostics persistence is optional, just like the camera-side report itself.
        return
