"""Bounded, advisory companion diagnostics; never input to backup or power admission."""

from datetime import UTC, datetime
from typing import Any

TRIAL_REASONS = frozenset(
    {
        "required_pids_unavailable",
        "sustained_overrun",
        "critical_readings_missing",
        "transport_fault",
    }
)
TRIAL_SOURCES = frozenset({"legacy_migration", "operator", "health", "unknown"})
TRIAL_ERRORS = frozenset(
    {
        "missing_record",
        "corrupt_record",
        "read_failed",
        "write_failed",
        "legacy_invalid",
        "mirror_failed",
        "revision_exhausted",
    }
)


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid diagnostic shape")
    return value


def _integer(value: Any, maximum: int = 2_147_483_647) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid diagnostic count")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid diagnostic flag")
    return value


def _choice(value: Any, allowed: frozenset[str], *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("invalid diagnostic category")
    return value


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 20 <= len(value) <= 40:
        raise ValueError("invalid diagnostic timestamp")
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None or not 2020 <= at.astimezone(UTC).year <= 2100:
        raise ValueError("invalid diagnostic timestamp")
    return value


def _trial_change(value: Any) -> dict[str, Any]:
    row = _object(value, {"revision", "enabled", "fallback_reason", "source", "changed_at_utc"})
    result = {
        "revision": _integer(row["revision"]),
        "enabled": _boolean(row["enabled"]),
        "fallback_reason": _choice(row["fallback_reason"], TRIAL_REASONS, nullable=True),
        "source": _choice(row["source"], TRIAL_SOURCES),
        "changed_at_utc": _timestamp(row["changed_at_utc"]),
    }
    if result["enabled"] and result["fallback_reason"] is not None:
        raise ValueError("contradictory trial state")
    if result["source"] == "health" and result["fallback_reason"] is None:
        raise ValueError("missing fallback reason")
    if result["source"] == "operator" and result["fallback_reason"] is not None:
        raise ValueError("operator setting is not an automatic fallback")
    return result


def clean_trial_settings(value: Any) -> dict[str, Any] | None:
    """Drop malformed advisory evidence without rejecting the logger's core status."""
    try:
        row = _object(
            value,
            {
                "schema_version",
                "enabled",
                "fallback_reason",
                "revision",
                "source",
                "changed_at_utc",
                "storage_state",
                "error",
                "legacy_key_present",
                "recent_changes",
            },
        )
        if type(row["schema_version"]) is not int or row["schema_version"] != 1:
            return None
        current = _trial_change(
            {
                key: row[key]
                for key in ("revision", "enabled", "fallback_reason", "source", "changed_at_utc")
            }
        )
        state = _choice(row["storage_state"], frozenset({"ready", "unavailable"}))
        error = _choice(row["error"], TRIAL_ERRORS, nullable=True)
        history = row["recent_changes"]
        if not isinstance(history, list) or len(history) > 8:
            return None
        changes = [_trial_change(item) for item in history]
        revisions = [item["revision"] for item in changes]
        if revisions != sorted(set(revisions)) or any(
            not 1 <= item <= current["revision"] for item in revisions
        ):
            return None
        if state == "ready" and current["revision"] == 0:
            return None
        if state == "unavailable" and current["enabled"]:
            return None
        return {
            "schema_version": 1,
            **current,
            "storage_state": state,
            "error": error,
            "legacy_key_present": _boolean(row["legacy_key_present"]),
            "recent_changes": changes,
        }
    except (ValueError, TypeError, OverflowError):
        return None


BOOT_STATES = frozenset(
    {"pending", "ready", "invalid", "read_failed", "write_failed", "probe_unavailable"}
)
BOOT_TRANSITIONS = frozenset(
    {
        "first_observation",
        "same_process",
        "process_restart",
        "package_update",
        "kernel_boot",
        "unknown",
    }
)
BOOT_REASONS = frozenset(
    {
        "unknown",
        "reboot",
        "cold",
        "power_key",
        "usb",
        "watchdog",
        "kernel_panic",
        "thermal",
        "hard_reset",
        "adb",
        "user_requested",
        "software_update",
        "recovery",
        "bootloader",
    }
)
BOOT_REASON_SOURCES = frozenset({"sys_boot_reason", "ro_boot_bootreason", "unavailable"})
EXIT_REASONS = frozenset(
    {
        "unavailable",
        "not_recorded",
        "unknown",
        "exit_self",
        "signaled",
        "low_memory",
        "crash",
        "native_crash",
        "anr",
        "initialization_failure",
        "permission_change",
        "excessive_resource_usage",
        "user_requested",
        "user_stopped",
        "dependency_died",
        "other",
        "freezer",
        "package_state_change",
        "package_updated",
    }
)
BOOT_OBSERVATION_KEYS = {
    "transition",
    "observed_at_utc",
    "boot_count",
    "boot_count_delta",
    "boot_reason",
    "boot_reason_source",
    "cause_confidence",
    "previous_process_exit",
    "previous_process_exit_at_utc",
    "app_version_code",
    "build_git_sha",
}


def _boot_observation(value: Any) -> dict[str, Any]:
    row = _object(value, BOOT_OBSERVATION_KEYS)
    build = row["build_git_sha"]
    if not isinstance(build, str) or not (
        build == "unknown"
        or (len(build) == 12 and all(char in "0123456789abcdef" for char in build))
    ):
        raise ValueError("invalid build identity")
    delta = row["boot_count_delta"]
    if delta is not None and _integer(delta, 1000) == 0:
        raise ValueError("invalid boot count delta")
    code = _integer(row["app_version_code"], 1_000_000)
    if code == 0:
        raise ValueError("invalid app version")
    result = {
        "transition": _choice(row["transition"], BOOT_TRANSITIONS),
        "observed_at_utc": _timestamp(row["observed_at_utc"]),
        "boot_count": None if row["boot_count"] is None else _integer(row["boot_count"]),
        "boot_count_delta": delta,
        "boot_reason": _choice(row["boot_reason"], BOOT_REASONS),
        "boot_reason_source": _choice(row["boot_reason_source"], BOOT_REASON_SOURCES),
        "cause_confidence": _choice(row["cause_confidence"], frozenset({"reported", "unknown"})),
        "previous_process_exit": _choice(row["previous_process_exit"], EXIT_REASONS),
        "previous_process_exit_at_utc": _timestamp(row["previous_process_exit_at_utc"]),
        "app_version_code": code,
        "build_git_sha": build,
    }
    if result["boot_reason"] in {"unknown", "reboot"} and result["cause_confidence"] != "unknown":
        raise ValueError("generic reboot is not cause evidence")
    if result["transition"] != "kernel_boot" and delta is not None:
        raise ValueError("boot delta requires a recorded new kernel boot")
    return result


def clean_restart_evidence(value: Any) -> dict[str, Any] | None:
    try:
        row = _object(value, BOOT_OBSERVATION_KEYS | {"schema_version", "storage_state", "history"})
        if type(row["schema_version"]) is not int or row["schema_version"] != 1:
            return None
        current = _boot_observation({key: row[key] for key in BOOT_OBSERVATION_KEYS})
        state = _choice(row["storage_state"], BOOT_STATES)
        if state == "ready" and current["observed_at_utc"] is None:
            return None
        history = row["history"]
        if not isinstance(history, list) or len(history) > 8:
            return None
        entries = [_boot_observation(item) for item in history]
        if any(item["observed_at_utc"] is None for item in entries):
            return None
        return {
            "schema_version": 1,
            "storage_state": state,
            **current,
            "history": entries,
        }
    except (ValueError, TypeError, OverflowError):
        return None
