"""Explicit, short lived maintenance admission after verified native timer cancellation.

The vendor's sleep-enable property selects sleep versus shutdown; it is never an
inhibit. Only a witnessed long press on the attested SystemUI countdown can acquire
this lease. Losing the lease stops backup and restores radios. It does not synthesize
an ACC edge, restart the native timer, or force the device to sleep.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
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
HEARTBEAT_REFRESH_TIMEOUT_S = 14.0
UI_DUMP_TIMEOUT_S = 5
UI_SCREEN_TIMEOUT_S = 8.0
UI_HELPER_PATH = "/data/local/tmp/.dashcam_stats_fresh_ui_v1.dex"
UI_HELPER_ASSET = Path(__file__).with_name("assets") / "fresh-ui.dex"
UI_HELPER_SHA256 = "0f331af612480bd7fa63ee65b16b3dd9a8cda4a5308daecf484a1a473cc8f5e4"
UI_HELPER_CLASS = "com.dashcamstats.maintenance.FreshUiSnapshot"
RESTORE_RESERVE_S = 60
MAX_DURATION_S = 1800
MAX_UI_BYTES = 128 * 1024
_ELAPSED_MAX_MS = 2**53 - 1
_ELAPSED_PADDING_MS = 1000
_RECORD_IO_TIMEOUT_S = 3.0
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


@dataclass(frozen=True)
class CancellationRecord:
    boot_id: str
    generation: str
    cycle_after_elapsed_ms: int | None
    legacy: bool = False

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "boot_id": self.boot_id,
            "cancelled": True,
            "generation": self.generation,
            "cycle_after_elapsed_ms": self.cycle_after_elapsed_ms,
        }


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


async def _ensure_ui_helper(address: str) -> None:
    # This small generated asset has tracked source and a reproducible local build.
    # Refuse a damaged server package before uploading or executing anything.
    metadata = UI_HELPER_ASSET.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not 1 <= metadata.st_size <= 16 * 1024
        or hashlib.sha256(UI_HELPER_ASSET.read_bytes()).hexdigest() != UI_HELPER_SHA256
    ):
        raise MaintenanceRefused("The verified maintenance screen helper is unavailable.")
    reply = await adb.shell(
        address,
        f"if [ -f '{UI_HELPER_PATH}' ] && [ ! -L '{UI_HELPER_PATH}' ]; "
        f"then sha256sum '{UI_HELPER_PATH}' 2>/dev/null; fi; exit 0",
        timeout=1.0,
    )
    if reply.split() == [UI_HELPER_SHA256, UI_HELPER_PATH]:
        return
    partial = f"{UI_HELPER_PATH}.{uuid.uuid4().hex}.partial"
    result = await adb._adb("-s", address, "push", str(UI_HELPER_ASSET), partial, timeout=3.0)
    if result.returncode:
        raise MaintenanceRefused("The verified maintenance screen helper could not be installed.")
    reply = await adb.shell(
        address,
        f"chmod 600 '{partial}' && [ ! -d '{UI_HELPER_PATH}' ] && "
        f"mv -f '{partial}' '{UI_HELPER_PATH}' && sha256sum '{UI_HELPER_PATH}'; "
        f"result=$?; rm -f '{partial}'; exit \"$result\"",
        timeout=1.0,
    )
    if reply.split() != [UI_HELPER_SHA256, UI_HELPER_PATH]:
        raise MaintenanceRefused("The on-device maintenance screen helper was not verified.")


async def _screen(address: str) -> str:
    # The standard dump waits for one idle second: a one-second native countdown
    # can prevent it from ever producing XML. A fresh accessibility connection gets
    # the current root directly. Its output remains subject to the exact parser.
    path = f"/data/local/tmp/.dashcam_stats_maintenance_ui.{uuid.uuid4().hex}.xml"
    try:
        async with asyncio.timeout(UI_SCREEN_TIMEOUT_S):
            await _ensure_ui_helper(address)
            return await adb.shell(
                address,
                f"umask 077; trap \"rm -f '{path}'\" EXIT HUP INT TERM; "
                f"rm -f '{path}' && [ -f '{UI_HELPER_PATH}' ] && [ ! -L '{UI_HELPER_PATH}' ] && "
                f"digest=\"$(sha256sum '{UI_HELPER_PATH}' 2>/dev/null)\" && "
                f"[ \"${{digest%% *}}\" = '{UI_HELPER_SHA256}' ] && "
                f"CLASSPATH='{UI_HELPER_PATH}:/system/framework/uiautomator.jar' "
                f"timeout {UI_DUMP_TIMEOUT_S} app_process /data/local/tmp {UI_HELPER_CLASS} '{path}' "
                f">/dev/null 2>&1 && [ -s '{path}' ] && [ ! -L '{path}' ] && "
                f"head -c {MAX_UI_BYTES + 1} '{path}'; "
                f"result=$?; rm -f '{path}'; exit \"$result\"",
                timeout=6.0,
            )
    except (OSError, TimeoutError, adb.AdbError) as exc:
        raise MaintenanceRefused("The dashcam screen could not be safely identified.") from exc


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
        self._anchor_task: asyncio.Task[None] | None = None
        self._record: CancellationRecord | None = None
        self._persistence_task: asyncio.Task[object] | None = None

    def _record_path(self) -> Path:
        if self._tombstone_path is None:
            self._tombstone_path = get_config().data_dir / "ingest" / "countdown-cancelled.json"
        return self._tombstone_path

    @staticmethod
    def _read_record(path: Path) -> CancellationRecord | None:
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
        if not isinstance(record, dict):
            raise ValueError("invalid cancellation record")
        version = record.get("schema_version")
        fields = {"schema_version", "boot_id", "cancelled", "generation"}
        if type(version) is int and version == 2:
            fields.add("cycle_after_elapsed_ms")
        if (
            set(record) != fields
            or type(version) is not int
            or version not in (1, 2)
            or record["cancelled"] is not True
            or not isinstance(record["boot_id"], str)
            or not _BOOT.fullmatch(record["boot_id"])
            or not isinstance(record["generation"], str)
            or not _BOOT.fullmatch(record["generation"])
        ):
            raise ValueError("invalid cancellation record")
        legacy = version == 1
        boundary = record.get("cycle_after_elapsed_ms")
        if boundary is not None and (
            type(boundary) is not int or not 0 <= boundary <= _ELAPSED_MAX_MS
        ):
            raise ValueError("invalid cancellation record")
        return CancellationRecord(record["boot_id"], record["generation"], boundary, legacy)

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
                self._record = record
                self._durable_boot = record.boot_id if record else None
                self._generation = record.generation if record else None
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
        record = CancellationRecord(boot_id, str(uuid.uuid4()), None)
        with self._tombstone_lock:
            try:
                self._write_record(record)
                self._durable_boot, self._tombstone_error = boot_id, False
                self._record, self._generation = record, record.generation
            except (OSError, ValueError) as exc:
                self._tombstone_error = True
                raise MaintenanceRefused(
                    "The cancellation record could not be saved; the countdown was left unchanged."
                ) from exc

    async def _persist(self, operation: Callable[..., object], *args: object) -> object:
        # wait_for(to_thread(...)) only cancels its await, not the disk worker.
        # Keep that worker tracked and shielded so cleanup retains its process fence
        # until it has actually exited, including after the request's bounded timeout.
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        self._persistence_task = task
        task.add_done_callback(
            lambda finished: None if finished.cancelled() else finished.exception()
        )
        return await asyncio.wait_for(asyncio.shield(task), timeout=_RECORD_IO_TIMEOUT_S)

    def _write_record(self, record: CancellationRecord) -> None:
        """Caller owns the tombstone lock; publish only after file and directory fsync."""
        path = self._record_path()
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(record.document(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _record_cycle_boundary(self, expected: CancellationRecord, upper_ms: int) -> bool:
        """Complete this exact cancellation, or anchor a legacy marker for FUTURE cycles."""
        if (
            type(upper_ms) is not int
            or not 0 <= upper_ms <= _ELAPSED_MAX_MS
            or expected.cycle_after_elapsed_ms is not None
        ):
            return False
        # Legacy bootstrap is independent of start(): it must never borrow a
        # newly acquired controller fence that start() could release on refusal.
        fence = (self._fence if not expected.legacy else None) or try_acquire(
            get_config().data_dir / ".ingest-maintenance.lock"
        )
        if fence is None:
            return False
        owned_fence = self._fence is fence
        try:
            with self._tombstone_lock:
                if self._read_record(self._record_path()) != expected:
                    return False
                updated = CancellationRecord(expected.boot_id, expected.generation, upper_ms)
                self._write_record(updated)
                self._record = updated
                self._durable_boot, self._generation = updated.boot_id, updated.generation
                return True
        except (OSError, ValueError, TypeError, RecursionError):
            self._tombstone_error = True
            return False
        finally:
            if not owned_fence:
                fence.release()

    def _clear_record(
        self,
        expected_boot: str | None,
        expected_generation: str | None,
        expected_record: CancellationRecord | None = None,
        borrowed_fence: ProcessFileLock | None = None,
    ) -> bool:
        # Serialize with another process preparing a new cancellation. A stale
        # ON observation must never erase the successor's newer boot marker.
        # Borrow only the fence captured when this clear was scheduled. Reading
        # self._fence here could borrow a later start()'s short-lived refusal fence.
        fence = borrowed_fence or try_acquire(get_config().data_dir / ".ingest-maintenance.lock")
        if fence is None:
            return False
        owned_fence = borrowed_fence is fence
        try:
            with self._tombstone_lock:
                try:
                    current = self._read_record(self._record_path())
                except (OSError, ValueError, TypeError, RecursionError):
                    if expected_boot is not None:
                        return False
                    current = None
                if expected_record is not None and current != expected_record:
                    return False
                if current is not None and (
                    (current.boot_id, current.generation) != (expected_boot, expected_generation)
                ):
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
        self,
        expected_boot: str | None,
        expected_generation: str | None,
        expected_record: CancellationRecord | None = None,
        borrowed_fence: ProcessFileLock | None = None,
    ) -> None:
        try:
            if (
                await asyncio.to_thread(
                    self._clear_record,
                    expected_boot,
                    expected_generation,
                    expected_record,
                    borrowed_fence,
                )
                and self._generation == expected_generation
                and (expected_record is None or self._record == expected_record)
            ):
                self._cancelled = False
                self._cancellation_verified = False
                self._generation = None
                self._record = None
        except Exception:
            self._tombstone_error = True

    async def _anchor_legacy_after_observation(
        self, record: CancellationRecord, uptime_s: float
    ) -> None:
        # No wall clock/mtime conversion and no retrospective acceptance of the edge
        # already present at upgrade. A later camera-recorded cycle must cross this bound.
        await asyncio.to_thread(
            self._record_cycle_boundary,
            record,
            math.ceil(uptime_s * 1000) + _ELAPSED_PADDING_MS,
        )

    def _recorded_cycle(self, observation: adb.RuntimeObservation) -> bool:
        record = self._record
        if (
            self._tombstone_error
            or record is None
            or record.legacy
            or record.cycle_after_elapsed_ms is None
            or observation.boot_id != record.boot_id
        ):
            return False
        evidence = observation.validated_sleep_deadline()
        return (
            evidence is not None
            and evidence.boot_id == record.boot_id
            and evidence.off_lower_elapsed_ms > record.cycle_after_elapsed_ms
            and evidence.off_lower_elapsed_ms / 1000
            + evidence.window_s
            - observation.uptime_s
            - adb.RUNTIME_OBSERVATION_TIMEOUT_S
            > 0
        )

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
        # Never clear/anchor underneath an in-flight cancellation gesture or stop.
        if self._lock.locked():
            return
        known_new_boot = (
            observation.boot_id is not None
            and _BOOT.fullmatch(observation.boot_id) is not None
            and (self._durable_boot or self._boot) is not None
            and observation.boot_id != (self._durable_boot or self._boot)
        )
        recorded_cycle = self._recorded_cycle(observation)
        if known_new_boot or observation.ignition_state == "on" or recorded_cycle:
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
                        self._clear_after_observation(
                            self._durable_boot,
                            self._generation,
                            self._record if recorded_cycle else None,
                            self._fence,
                        ),
                        name="maintenance-clear-cancellation",
                    )
        elif (
            not self._tombstone_error
            and self._record is not None
            and self._record.legacy
            and self._state in {"inactive", "failed", "expired"}
            and self._fence is None
            and observation.boot_id == self._record.boot_id
            and observation.ignition_state == "off"
            and math.isfinite(observation.uptime_s)
            and 0 <= observation.uptime_s <= (_ELAPSED_MAX_MS - _ELAPSED_PADDING_MS) / 1000
            and (self._anchor_task is None or self._anchor_task.done())
        ):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._anchor_task = loop.create_task(
                self._anchor_legacy_after_observation(self._record, observation.uptime_s),
                name="maintenance-anchor-legacy-cancellation",
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
            if self._anchor_task is not None and not self._anchor_task.done():
                raise MaintenanceRefused("The previous cancellation record is still being updated.")
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
                    await self._persist(self._record_cancellation, fresh.boot_id)
                except TimeoutError as exc:
                    raise MaintenanceRefused(
                        "The cancellation record could not be saved in time; the countdown was left unchanged."
                    ) from exc
                await adb.shell(address, f"input swipe {x} {y} {x} {y} 1500", timeout=3.0)
                if countdown_target(await _screen(address)) is not None:
                    raise MaintenanceRefused("The native countdown did not disappear.")
                after = await _power(address)
                if after.boot_id != fresh.boot_id or after.uptime_s < fresh.uptime_s:
                    raise MaintenanceRefused("The dashcam restarted during countdown cancellation.")
                record = self._record
                assert record is not None
                # The pre-gesture record is deliberately provisional. Only completed
                # input + fresh post-gesture OFF/boot proof can permit recorded recovery.
                if not await self._persist(
                    self._record_cycle_boundary,
                    record,
                    math.ceil(after.uptime_s * 1000) + _ELAPSED_PADDING_MS,
                ):
                    raise MaintenanceRefused("The verified cancellation time could not be saved.")
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
                async with asyncio.timeout(HEARTBEAT_REFRESH_TIMEOUT_S):
                    if countdown_target(await _screen(self._address)) is not None:
                        raise MaintenanceRefused(
                            "A new native countdown appeared; maintenance ended."
                        )
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
        for task in (self._persistence_task, self._clear_task):
            if restored and task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=_RECORD_IO_TIMEOUT_S)
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
    for task in (_controller._persistence_task, _controller._clear_task, _controller._anchor_task):
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (OSError, TimeoutError):
                _controller._tombstone_error = True


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
