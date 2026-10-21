"""Explicit, short lived maintenance admission after verified native timer cancellation.

The vendor's sleep-enable property selects sleep versus shutdown; it is never an
inhibit. Only a witnessed long press on the attested SystemUI countdown can acquire
this lease. Losing the lease stops backup and restores radios. It does not synthesize
an ACC edge, restart the native timer, or force the device to sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import get_config
from app.core.process_lock import ProcessFileLock, try_acquire
from app.ingest import adb

LEASE_PATH = "/data/local/tmp/.dashcam_stats_maintenance"
LEASE_PARTIAL_PATH = LEASE_PATH + ".partial"
SYSTEMUI_SHA256 = "f3f7e00d938c6c74a88c5db24c60a908fbcd7923a7098f2b65439465921d6f65"
EVIDENCE_TTL_S = 30.0
DEVICE_HEARTBEAT_TTL_S = 45
HEARTBEAT_INTERVAL_S = 10.0
RESTORE_RESERVE_S = 60
MAX_DURATION_S = 1800
MAX_UI_BYTES = 128 * 1024
_BOOT = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
_BOUNDS = re.compile(r"\[(\d{1,4}),(\d{1,4})\]\[(\d{1,4}),(\d{1,4})\]")
_POWER_COMMAND = (
    "cat /proc/sys/kernel/random/boot_id; cat /proc/uptime; "
    "settings get global acc_status; getprop persist.sys.state.system_sleep; "
    "settings get global system_sleep_status; cat /proc/uptime; "
    "settings get global acc_status; getprop persist.sys.state.system_sleep; "
    "settings get global system_sleep_status; cat /proc/sys/kernel/random/boot_id"
)


def _record_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cancellation field")
        result[key] = value
    return result


class MaintenanceRefused(Exception):
    """A bounded, safe public reason; raw device output never crosses the API."""


@dataclass(frozen=True)
class PowerProof:
    boot_id: str
    uptime_s: float


def parse_power(reply: str) -> PowerProof | None:
    lines = reply.strip().splitlines()
    if len(lines) != 10:
        return None
    boot, before, acc, policy, sleeping, after, acc2, policy2, sleeping2, boot2 = (
        part.strip() for part in lines
    )
    try:
        initial, final = float(before.split()[0]), float(after.split()[0])
    except (ValueError, IndexError):
        return None
    if (
        not _BOOT.fullmatch(boot)
        or boot != boot2
        or acc != "0"
        or acc2 != "0"
        or policy != "true"
        or policy2 != "true"
        or sleeping != "0"
        or sleeping2 != "0"
        or not math.isfinite(initial)
        or not math.isfinite(final)
        or not 0 <= initial <= final <= initial + 3
    ):
        return None
    return PowerProof(boot, final)


def countdown_target(xml: str) -> tuple[int, int] | None:
    """One exact SystemUI countdown, or absence. Malformed/ambiguous XML is refused."""
    if (
        len(xml.encode("utf-8")) > MAX_UI_BYTES
        or "<!DOCTYPE" in xml.upper()
        or "<!ENTITY" in xml.upper()
    ):
        raise MaintenanceRefused("The dashcam screen could not be safely identified.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise MaintenanceRefused("The dashcam screen could not be safely identified.") from exc
    nodes = list(root.iter("node"))
    if root.tag != "hierarchy" or not 1 <= len(nodes) <= 2000:
        raise MaintenanceRefused("The dashcam screen could not be safely identified.")
    targets = [
        node for node in nodes if node.get("resource-id") == "com.android.systemui:id/tv_countdown"
    ]
    if not targets:
        return None
    if len(targets) != 1:
        raise MaintenanceRefused("More than one native countdown was found.")
    node = targets[0]
    bounds = _BOUNDS.fullmatch(node.get("bounds", ""))
    seconds = re.match(r"\s*([0-9]{1,4})\b", node.get("text", ""))
    if (
        node.get("package") != "com.android.systemui"
        or node.get("class") != "android.widget.TextView"
        or node.get("enabled") != "true"
        or node.get("long-clickable") != "true"
        or bounds is None
        or seconds is None
        or not 30 <= int(seconds[1]) <= 3600
    ):
        raise MaintenanceRefused("The native countdown cannot be safely cancelled now.")
    left, top, right, bottom = (int(part) for part in bounds.groups())
    if not 0 <= left < right <= 8192 or not 0 <= top < bottom <= 8192:
        raise MaintenanceRefused("The native countdown bounds are invalid.")
    return (left + right) // 2, (top + bottom) // 2


async def _power(address: str) -> PowerProof:
    proof = parse_power(await adb.shell(address, _POWER_COMMAND, timeout=3.0))
    if proof is None:
        raise MaintenanceRefused(
            "Maintenance requires verified ignition OFF and normal sleep policy."
        )
    return proof


async def _screen(address: str) -> str:
    # A fixed helper path contains no footage or credentials. A failed/partial dump
    # cannot reuse yesterday's XML: remove it first and require the dump's success.
    path = "/data/local/tmp/.dashcam_stats_maintenance_ui.xml"
    return await adb.shell(
        address,
        f"rm -f '{path}'; if timeout 7 uiautomator dump --compressed '{path}' "
        f">/dev/null 2>&1; then head -c {MAX_UI_BYTES + 1} '{path}'; fi; rm -f '{path}'",
        timeout=9.0,
    )


async def _attest(address: str) -> None:
    reply = await adb.shell(
        address,
        'p="$(pm path com.android.systemui 2>/dev/null)"; '
        'case "$p" in package:/system/*|package:/system_ext/*|package:/product/*) '
        'sha256sum "${p#package:}" 2>/dev/null;; esac',
        timeout=6.0,
    )
    if not reply.strip().split() or reply.strip().split()[0] != SYSTEMUI_SHA256:
        raise MaintenanceRefused("This SystemUI build has not been verified for maintenance.")


class MaintenanceController:
    def __init__(self, tombstone_path: Path | None = None) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._boot: str | None = None
        self._token: str | None = None
        self._address: str | None = None
        self._end = 0.0
        self._device_end = 0
        self._verified_at = 0.0
        self._expires_at: datetime | None = None
        self._state = "inactive"
        self._reason: str | None = None
        self._cancelled = False
        self._cancellation_verified = False
        self._fence: ProcessFileLock | None = None
        self._tombstone_path = tombstone_path
        self._tombstone_loaded = False
        self._tombstone_error = False
        self._durable_boot: str | None = None
        self._generation: str | None = None
        self._tombstone_lock = threading.Lock()
        self._clear_task: asyncio.Task[None] | None = None

    def _record_path(self) -> Path:
        if self._tombstone_path is None:
            self._tombstone_path = get_config().data_dir / "ingest" / "countdown-cancelled.json"
        return self._tombstone_path

    @staticmethod
    def _read_record(path: Path) -> tuple[str, str] | None:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024:
                raise ValueError("invalid cancellation record")
            with path.open("rb") as handle:
                raw = handle.read(1025)
        except FileNotFoundError:
            return None
        if len(raw) > 1024:
            raise ValueError("invalid cancellation record")
        record = json.loads(raw, object_pairs_hook=_record_pairs)
        if (
            not isinstance(record, dict)
            or set(record) != {"schema_version", "boot_id", "cancelled", "generation"}
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["cancelled"] is not True
            or not isinstance(record["boot_id"], str)
            or not _BOOT.fullmatch(record["boot_id"])
            or not isinstance(record["generation"], str)
            or not _BOOT.fullmatch(record["generation"])
        ):
            raise ValueError("invalid cancellation record")
        return record["boot_id"], record["generation"]

    def load_once(self) -> None:
        # Lifespan performs this off the event loop before starting the poller.
        # The fallback covers direct library callers that do not run lifespan.
        if self._tombstone_loaded:
            return
        with self._tombstone_lock:
            if self._tombstone_loaded:
                return
            try:
                record = self._read_record(self._record_path())
                self._durable_boot, self._generation = (
                    record if record is not None else (None, None)
                )
            except (OSError, ValueError, TypeError, RecursionError):
                self._tombstone_error = True
            self._tombstone_loaded = True

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name != "nt":
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _record_cancellation(self, boot_id: str) -> None:
        self.load_once()
        path = self._record_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        generation = str(uuid.uuid4())
        raw = json.dumps(
            {"schema_version": 1, "boot_id": boot_id, "cancelled": True, "generation": generation}
        )
        with self._tombstone_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                self._sync_directory(path.parent)
                self._durable_boot, self._tombstone_error = boot_id, False
                self._generation = generation
            except (OSError, ValueError) as exc:
                self._tombstone_error = True
                raise MaintenanceRefused(
                    "The cancellation record could not be saved; the countdown was left unchanged."
                ) from exc
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _clear_record(self, expected_boot: str | None, expected_generation: str | None) -> bool:
        # Serialize with another process preparing a new cancellation. A stale
        # ON observation must never erase the successor's newer boot marker.
        fence = self._fence or try_acquire(get_config().data_dir / ".ingest-maintenance.lock")
        if fence is None:
            return False
        owned_fence = self._fence is fence
        try:
            with self._tombstone_lock:
                try:
                    current = self._read_record(self._record_path())
                except (OSError, ValueError, TypeError, RecursionError):
                    if expected_boot is not None:
                        return False
                    current = None
                if current is not None and current != (expected_boot, expected_generation):
                    return False
                self._record_path().unlink(missing_ok=True)
                if self._record_path().parent.exists():
                    self._sync_directory(self._record_path().parent)
                self._durable_boot, self._tombstone_error = None, False
                return True
        except OSError:
            return False
        finally:
            if not owned_fence:
                fence.release()

    async def _clear_after_observation(
        self, expected_boot: str | None, expected_generation: str | None
    ) -> None:
        try:
            if (
                await asyncio.to_thread(self._clear_record, expected_boot, expected_generation)
                and self._generation == expected_generation
            ):
                self._cancelled = False
                self._cancellation_verified = False
                self._generation = None
        except Exception:
            self._tombstone_error = True

    def remaining(self, boot_id: str | None) -> float | None:
        self.load_once()
        now = time.monotonic()
        if (
            self._state != "active"
            or self._tombstone_error
            or boot_id is None
            or boot_id != self._boot
            or now - self._verified_at >= EVIDENCE_TTL_S
        ):
            return None
        return max(0.0, self._end - now - RESTORE_RESERVE_S)

    def native_cancelled(self, boot_id: str | None) -> bool:
        self.load_once()
        # Missing boot identity cannot turn a known cancelled timer into a fresh
        # one. Only observe() may clear a marker after positive live evidence.
        return (
            self._tombstone_error
            or self._durable_boot is not None
            or (self._cancelled and (boot_id is None or boot_id == self._boot))
        )

    def observe(self, observation: adb.RuntimeObservation) -> None:
        self.load_once()
        known_new_boot = (
            observation.boot_id is not None
            and _BOOT.fullmatch(observation.boot_id) is not None
            and (self._durable_boot or self._boot) is not None
            and observation.boot_id != (self._durable_boot or self._boot)
        )
        if known_new_boot or observation.ignition_state == "on":
            if self._state == "active":
                self._state = "restoring"
                self._reason = "Maintenance ended after ignition or device boot changed."
            if self._cancelled or self._durable_boot is not None or self._tombstone_error:
                if self._clear_task is None or self._clear_task.done():
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        # A synchronous consumer cannot fsync under its status lock.
                        # Keep refusing until an asynchronous fresh observation arrives.
                        return
                    self._clear_task = loop.create_task(
                        self._clear_after_observation(self._durable_boot, self._generation),
                        name="maintenance-clear-cancellation",
                    )

    def snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        remaining = self.remaining(self._boot)
        verified = remaining is not None and remaining > 0
        return {
            "active": self._state == "active" and verified,
            "verified": verified,
            "state": self._state,
            "remaining_s": round(remaining, 1) if remaining is not None else 0,
            "expires_at": self._expires_at.isoformat() if self._expires_at else None,
            "evidence_valid_for_s": round(max(0.0, EVIDENCE_TTL_S - (now - self._verified_at)), 1)
            if verified
            else 0,
            "reason": (
                "Fresh on-device maintenance evidence is unavailable; backup admission is closed."
                if self._state == "active" and remaining is None
                else self._reason
            ),
            "countdown_cancelled": self._cancellation_verified,
            "requires_ignition_cycle": self._cancelled
            or self._durable_boot is not None
            or self._tombstone_error,
        }

    async def _publish(self, proof: PowerProof) -> None:
        assert self._token is not None and self._address is not None
        now = math.floor(proof.uptime_s)
        expiry = min(now + DEVICE_HEARTBEAT_TTL_S, self._device_end)
        record = f"{self._token} {proof.boot_id} {expiry} {self._device_end}"
        reply = await adb.shell(
            self._address,
            f"umask 077; printf '%s\\n' '{record}' > '{LEASE_PARTIAL_PATH}' && "
            f"chmod 600 '{LEASE_PARTIAL_PATH}' && "
            f"mv -f '{LEASE_PARTIAL_PATH}' '{LEASE_PATH}' && cat '{LEASE_PATH}'",
            timeout=3.0,
        )
        if reply.strip() != record:
            raise MaintenanceRefused("The on-device maintenance lease was not verified.")

    async def start(self, address: str, duration_s: int) -> dict[str, object]:
        if self._fence is not None:
            raise MaintenanceRefused("Maintenance is already active or restoring radios.")
        fence = try_acquire(get_config().data_dir / ".ingest-maintenance.lock")
        if fence is None:
            raise MaintenanceRefused("Another server process owns maintenance.")
        self._fence = fence
        try:
            return await self._start(address, duration_s)
        except BaseException:
            if self._fence is fence and self._state != "restoring":
                fence.release()
                self._fence = None
            raise

    async def _start(self, address: str, duration_s: int) -> dict[str, object]:
        if type(duration_s) is not int or not 60 <= duration_s <= MAX_DURATION_S:
            raise MaintenanceRefused("Maintenance duration must be between 60 and 1800 seconds.")
        async with self._lock:
            if self._task is not None and not self._task.done():
                raise MaintenanceRefused("Maintenance is already active or restoring radios.")
            if self._clear_task is not None and not self._clear_task.done():
                raise MaintenanceRefused("The previous cancellation record is still being cleared.")
            # No cached status or caller-provided boolean can authorize this action.
            await _attest(address)
            before = await _power(address)
            target = countdown_target(await _screen(address))
            if target is None:
                raise MaintenanceRefused(
                    "The native countdown is not visible. An already dismissed popup cannot prove a maintenance hold."
                )
            fresh = await _power(address)
            if fresh.boot_id != before.boot_id:
                raise MaintenanceRefused("The dashcam restarted during maintenance preparation.")
            x, y = target
            # Only the attested countdown's current bounds. Never a blind tap, property
            # rewrite, fabricated ignition edge, recorder stop or forced suspension.
            self._boot, self._address = fresh.boot_id, address
            self._cancelled = True
            self._cancellation_verified = False
            self._token = None
            self._state = "failed"
            self._reason = "Countdown cancellation was attempted but is not yet verified."
            try:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(self._record_cancellation, fresh.boot_id), timeout=3.0
                    )
                except TimeoutError as exc:
                    raise MaintenanceRefused(
                        "The cancellation record could not be saved in time; the countdown was left unchanged."
                    ) from exc
                await adb.shell(address, f"input swipe {x} {y} {x} {y} 1500", timeout=3.0)
                if countdown_target(await _screen(address)) is not None:
                    raise MaintenanceRefused("The native countdown did not disappear.")
                after = await _power(address)
                if after.boot_id != fresh.boot_id:
                    raise MaintenanceRefused("The dashcam restarted during countdown cancellation.")
                self._cancellation_verified = True
                now = time.monotonic()
                self._token = str(uuid.uuid4())
                self._end = now + duration_s
                self._device_end = math.floor(after.uptime_s) + duration_s
                self._expires_at = datetime.now(UTC) + timedelta(seconds=duration_s)
                await self._publish(after)
                self._verified_at = time.monotonic()
                self._state, self._reason = "active", None
                self._task = asyncio.create_task(self._heartbeat(), name="ingest-maintenance")
                return self.snapshot()
            except BaseException:
                # The gesture may have succeeded even if verification failed. Preserve
                # that uncertainty and close transfer admission; never claim a timer.
                await self._end_hold("failed", "Maintenance cancellation could not be verified.")
                raise

    async def _heartbeat(self) -> None:
        try:
            while self._state == "active":
                if self._end - time.monotonic() <= RESTORE_RESERVE_S:
                    break
                await asyncio.sleep(
                    min(
                        HEARTBEAT_INTERVAL_S,
                        max(0.0, self._end - time.monotonic() - RESTORE_RESERVE_S),
                    )
                )
                if self._state != "active":
                    break
                assert self._address is not None
                if countdown_target(await _screen(self._address)) is not None:
                    raise MaintenanceRefused("A new native countdown appeared; maintenance ended.")
                proof = await _power(self._address)
                if proof.boot_id != self._boot:
                    raise MaintenanceRefused("The device boot changed; maintenance ended.")
                await self._publish(proof)
                self._verified_at = time.monotonic()
            await self._end_hold(
                "expired", "Maintenance ended; radios are restored before backup can resume."
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._end_hold("failed", "Fresh on-device maintenance evidence was lost.")

    async def _end_hold(self, state: str, reason: str) -> None:
        self._state, self._reason = "restoring", reason
        if self._address is not None and self._token is not None:
            # Exact generation only. A timed-out write is allowed to expire on-device;
            # it cannot extend past its last verified 45-second heartbeat.
            with contextlib.suppress(Exception):
                await adb.shell(
                    self._address,
                    f"record=\"$(cat '{LEASE_PATH}' 2>/dev/null)\"; "
                    f"case \"$record\" in '{self._token} '*) rm -f '{LEASE_PATH}';; esac",
                    timeout=3.0,
                )
        from app.ingest import puller

        restored = False
        with contextlib.suppress(Exception):
            restored = await asyncio.wait_for(puller.stop_for_maintenance(), timeout=120.0)
        self._state = state if restored else "restoring"
        self._reason = (
            reason if restored else "Maintenance ended; radio restoration remains pending."
        )
        if restored and self._clear_task is not None and not self._clear_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._clear_task), timeout=3.0)
            except Exception:
                restored = False
                self._state, self._reason = (
                    "restoring",
                    "Maintenance cancellation cleanup remains pending.",
                )
        if restored and self._fence is not None:
            self._fence.release()
            self._fence = None

    async def stop(self) -> dict[str, object]:
        async with self._lock:
            task, self._task = self._task, None
            if task is not None and task is not asyncio.current_task() and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if self._state in {"active", "restoring"}:
                await self._end_hold("inactive", "Maintenance was stopped by the operator.")
            return self.snapshot()


_controller = MaintenanceController()


def get_controller() -> MaintenanceController:
    return _controller


async def shutdown() -> None:
    await _controller.stop()


async def initialize() -> None:
    try:
        await asyncio.wait_for(asyncio.to_thread(_controller.load_once), timeout=3.0)
    except TimeoutError:
        _controller._tombstone_error = True
        _controller._tombstone_loaded = True


def watchdog_maintenance_functions() -> str:
    """A stale/server-only claim cannot suppress the detached native-deadline guard."""
    return (
        "maintenance_valid() { "
        f"record=\"$(cat '{LEASE_PATH}' 2>/dev/null)\"; set -- $record; "
        '[ "$#" -eq 4 ] || return 1; '
        'case "$1" in ????????-????-????-????-????????????) ;; *) return 1;; esac; '
        'case "$1" in *[!0-9a-f-]*) return 1;; esac; '
        'boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"; '
        '[ "$2" = "$boot" ] || return 1; '
        'case "$3:$4" in *[!0-9:]*|:*|*:) return 1;; esac; '
        '[ "${#3}" -le 10 ] && [ "${#4}" -le 10 ] || return 1; '
        f'[ "$3" -gt "$now" ] && [ "$3" -le "$((now + {DEVICE_HEARTBEAT_TTL_S}))" ] '
        f'&& [ "$4" -ge "$3" ] && [ "$4" -le "$((now + {MAX_DURATION_S}))" ] || return 1; '
        'acc="$(settings get global acc_status 2>/dev/null)"; '
        'policy="$(getprop persist.sys.state.system_sleep 2>/dev/null)"; '
        'sleeping="$(settings get global system_sleep_status 2>/dev/null)"; '
        '[ "$acc:$policy:$sleeping" = "0:true:0" ] || return 1; '
        f'left="$(($4 - {RESTORE_RESERVE_S} - now))"; '
        '[ "$left" -lt "$remaining" ] && remaining="$left"; '
        '[ "$left" -gt 0 ] || reason=pre_sleep; return 0; }; '
        "maintenance_fold() { if maintenance_valid; then maintenance_seen=1; return 0; fi; "
        'if [ "${maintenance_seen:-0}" = 1 ]; then remaining=0; reason=lease_expired; return 0; fi; '
        "return 1; }; "
    )
