"""Durable ingest/radio state machine and restart reconciliation.

The ordinary ingest status is intentionally memory-only.  Radio ownership cannot be:
an unclean process exit may leave Bluetooth or a hotspot different from the operator's
baseline, so intent is committed before every side effect and verification afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_config
from app.core.logging import get_logger
from app.core.process_lock import ProcessFileLock, try_acquire
from app.db.models import IngestRadioTransition
from app.db.session import session_scope
from app.ingest import obd_control, radios
from app.ingest.ha_import_queue import redact

log = get_logger(__name__)

# Longer than SQLite's 30-second busy timeout: a healthy owner queued behind another
# writer must not be mistaken for a dead process and fenced out while it can still issue
# a device command.
LEASE_TTL_S = 45.0
HEARTBEAT_INTERVAL_S = 10.0
QUIET_RESTORE_MARGIN_S = 30.0
MAX_WATCHDOG_DEADLINE_S = 540
PROCESS_FENCE_NAME = ".ingest-radio-transition.lock"


class TransitionPhase(StrEnum):
    PREPARING = "preparing"
    FINALISING_OBD = "finalising_obd"
    TRANSFERRING_OBD = "transferring_obd"
    CAPTURING_RADIO_STATE = "capturing_radio_state"
    DISABLING_RADIOS = "disabling_radios"
    INGESTING = "ingesting"
    RESTORING_RADIOS = "restoring_radios"
    RESUMING_OBD = "resuming_obd"
    COMPLETE = "complete"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class TransitionBusy(RuntimeError):
    """Another process already owns radio control."""


class RadioTransitionError(RuntimeError):
    """A safe, fully-restorable radio transition could not be completed."""


def _now() -> datetime:
    return datetime.now(UTC)


def _lease_deadline() -> datetime:
    return _now() + timedelta(seconds=LEASE_TTL_S)


def _process_fence_path() -> Path:
    return get_config().data_dir / PROCESS_FENCE_NAME


def _error_text(error: object) -> str:
    text = " ".join(redact(error).splitlines()).strip()
    return text[:1000] or type(error).__name__


@dataclass(slots=True)
class RadioTransition:
    id: int
    transition_id: str
    lease_owner: str
    address: str
    logger_status_path: str | None
    controller: radios.RadioController
    watchdog_deadline_s: int
    process_fence: ProcessFileLock = field(repr=False)
    lease_loss_callback: Callable[[], object] | None = field(default=None, repr=False)
    _heartbeat_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _deadline_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _restore_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _restore_result: bool | None = field(default=None, repr=False)
    _baseline_restored: bool = field(default=False, repr=False)
    _baseline_error: object | None = field(default=None, repr=False)
    _closed: bool = False
    _hotspot_config: tuple[str, str] | None = field(default=None, repr=False)
    _untracked_capsule_ref: str | None = field(default=None, repr=False)
    _lease_lost_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _lease_loss_error: RadioTransitionError | None = field(default=None, repr=False)

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost_event.is_set()

    @property
    def lease_loss_error(self) -> RadioTransitionError | None:
        return self._lease_loss_error

    async def wait_for_lease_loss(self) -> RadioTransitionError:
        await self._lease_lost_event.wait()
        assert self._lease_loss_error is not None
        return self._lease_loss_error

    def raise_if_lease_lost(self) -> None:
        if self._lease_loss_error is not None:
            raise self._lease_loss_error

    def _signal_lease_loss(self, error: object) -> None:
        if self._lease_lost_event.is_set():
            return
        lease_error = RadioTransitionError(
            f"durable ingest radio lease was lost: {_error_text(error)}"
        )
        self._lease_loss_error = lease_error
        self._lease_lost_event.set()
        if self.lease_loss_callback is not None:
            try:
                self.lease_loss_callback()
            except Exception as exc:
                log.error(
                    "could not signal pull cancellation after lease loss", error=_error_text(exc)
                )

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(), name=f"ingest-radio-lease-{self.transition_id}"
            )

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            try:
                await self.checkpoint()
            except Exception as exc:
                # Signal the transfer's thread-safe cancellation event immediately. The
                # process fence remains held, so a slow commit/ADB call may finish without
                # an expired-row adopter issuing overlapping side effects.
                self._signal_lease_loss(exc)
                log.error("could not renew the ingest radio lease", error=_error_text(exc))
                return

    async def checkpoint(
        self,
        phase: TransitionPhase | str | None = None,
        **values: Any,
    ) -> None:
        now = _now()
        if phase is not None:
            values["phase"] = str(phase)
        values.update(updated_at=now, heartbeat_at=now)
        values.setdefault("lease_expires_at", _lease_deadline())
        async with session_scope() as session:
            result = await session.execute(
                update(IngestRadioTransition)
                .where(
                    IngestRadioTransition.id == self.id,
                    IngestRadioTransition.active.is_(True),
                    IngestRadioTransition.lease_owner == self.lease_owner,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                error = RadioTransitionError("the ingest radio lease is no longer owned")
                self._signal_lease_loss(error)
                raise error

    async def _row(self) -> IngestRadioTransition:
        async with session_scope() as session:
            row = await session.get(IngestRadioTransition, self.id)
            if row is None or not row.active or row.lease_owner != self.lease_owner:
                raise RadioTransitionError("the ingest radio transition is no longer active")
            return row

    async def prepare_logger(
        self, *, timeout_s: float = obd_control.DEFAULT_TIMEOUT_S
    ) -> obd_control.LoggerAck:
        if not self.logger_status_path:
            raise RadioTransitionError("logger status path is unavailable")
        request_id = str(uuid.uuid4())
        await self.checkpoint(
            TransitionPhase.FINALISING_OBD,
            logger_request_id=request_id,
            logger_quiesce_requested_at=_now(),
        )
        try:
            ack = await obd_control.request_quiesce(
                self.address,
                self.logger_status_path,
                timeout_s=timeout_s,
                hold_s=self.watchdog_deadline_s + timeout_s,
                request_id=request_id,
            )
        except Exception as exc:
            await self.checkpoint(last_error=_error_text(exc))
            raise RadioTransitionError(_error_text(exc)) from exc
        await self.checkpoint(logger_quiesce_acked_at=_now())
        return ack

    async def mark_obd_transfer_complete(self) -> None:
        await self.checkpoint(
            TransitionPhase.TRANSFERRING_OBD,
            obd_transfer_complete=True,
        )

    async def capture_and_quiet(self) -> None:
        await self.checkpoint(TransitionPhase.CAPTURING_RADIO_STATE)
        snapshot = await self.controller.capture()
        restore_ref = None
        if snapshot.hotspot_config is not None:
            restore_ref = await self.controller.persist_hotspot_capsule(
                self.transition_id, snapshot.hotspot_config
            )
            if restore_ref is None:
                raise RadioTransitionError(
                    "hotspot recovery state could not be protected; radios were left on"
                )
            self._hotspot_config = snapshot.hotspot_config
        try:
            await self.checkpoint(
                bluetooth_before=snapshot.bluetooth,
                hotspot_before=snapshot.hotspot,
                hotspot_interface=snapshot.hotspot_interface,
                transport_interface=snapshot.transport_interface,
                hotspot_restore_ref=restore_ref,
            )
        except BaseException:
            # No radio has been touched yet. If the durable reference cannot be committed,
            # deterministically erase the credential capsule instead of orphaning it.
            if restore_ref is not None:
                try:
                    removed = await asyncio.shield(
                        self.controller.remove_hotspot_capsule(restore_ref)
                    )
                except BaseException:
                    removed = False
                if not removed:
                    self._untracked_capsule_ref = restore_ref
            self._hotspot_config = None
            raise
        if snapshot.bluetooth == "unknown" or snapshot.hotspot == "unknown":
            raise RadioTransitionError("radio baseline could not be read; radios were left on")
        if snapshot.hotspot == "on" and snapshot.hotspot_config is None:
            raise RadioTransitionError(
                "hotspot configuration could not be recovered; radios were left on"
            )

        await self.checkpoint(TransitionPhase.DISABLING_RADIOS)
        if snapshot.bluetooth == "on":
            await self.checkpoint(bluetooth_disable_attempted=True)
            if not await self.controller.disable_bluetooth():
                raise RadioTransitionError("Bluetooth disable could not be verified")
            await self.checkpoint(bluetooth_disable_verified=True)
        else:
            await self.checkpoint(bluetooth_disable_verified=True)

        if snapshot.hotspot == "on":
            await self.checkpoint(hotspot_disable_attempted=True)
            if not await self.controller.disable_hotspot():
                raise RadioTransitionError("hotspot disable could not be verified")
            await self.checkpoint(hotspot_disable_verified=True)
        else:
            # OFF is already quiet; TRANSPORT is intentionally retained because taking
            # it down would destroy the ADB/TCP data path.
            await self.checkpoint(hotspot_disable_verified=True)
        await self.checkpoint(TransitionPhase.INGESTING)
        self._start_quiet_deadline()

    def _start_quiet_deadline(self) -> None:
        if self._deadline_task is None:
            self._deadline_task = asyncio.create_task(
                self._restore_at_quiet_deadline(),
                name=f"ingest-radio-deadline-{self.transition_id}",
            )

    async def _restore_at_quiet_deadline(self) -> None:
        delay = max(1.0, float(self.watchdog_deadline_s) - QUIET_RESTORE_MARGIN_S)
        await asyncio.sleep(delay)
        log.warning(
            "radio quiet deadline reached; restoring radios while ingest continues",
            transition_id=self.transition_id,
            quiet_seconds=round(delay),
        )
        await self.restore_radio_baseline(
            error=RadioTransitionError("radio quiet deadline reached")
        )

    async def restore_radio_baseline(self, *, error: object | None = None) -> bool:
        """Restore radios/logger at the safety deadline while retaining ingest ownership."""
        async with self._restore_lock:
            if self._baseline_restored:
                return True
            try:
                restored = await self._restore(error=error, finalize=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await self.checkpoint(
                        TransitionPhase.RECOVERY_REQUIRED,
                        recovery_required=True,
                        last_error=_error_text(exc),
                    )
                return False
            if restored:
                self._baseline_restored = True
                self._baseline_error = error
            return restored

    async def restore(self, *, error: object | None = None) -> bool:
        """Restore with process-local ownership released even if the caller cancels."""
        async with self._restore_lock:
            if self._restore_result is not None:
                return self._restore_result
            if self._closed:
                # A closed transition no longer owns the process-lifetime fence.  It may
                # never issue another controller side effect, even if an earlier restore
                # failed before assigning its cached result.
                self._restore_result = False
                return False
            try:
                # A deadline checkpoint deliberately retains the protected hotspot
                # capsule while the ingest fence remains active.  Re-run the idempotent
                # baseline verification here so finalisation can remove that last-resort
                # recovery material only after the long-running pull really exits.
                effective_error = self._baseline_error or error or self._lease_loss_error
                result = await self._restore(
                    error=effective_error,
                    finalize=True,
                )
                self._restore_result = result
                return result
            except BaseException as exc:
                # Make exceptional close terminal *before* releasing the OS fence.  The
                # puller's nested cleanup paths can otherwise call restore a second time
                # on this same object while an expired-row adopter owns the device.
                self._restore_result = False
                try:
                    await self._persist_restore_failure(exc)
                except BaseException as persist_exc:
                    # The active row and its finite lease still remain durable recovery
                    # authority if this last checkpoint is itself what failed.
                    log.error(
                        "could not persist exceptional radio restore recovery state",
                        transition_id=self.transition_id,
                        error=_error_text(persist_exc),
                    )
                raise
            finally:
                await self.close()

    async def _persist_restore_failure(self, error: object) -> None:
        """Expire an exceptional restore directly, before its process fence is released."""
        now = _now()
        async with session_scope() as session:
            result = await session.execute(
                update(IngestRadioTransition)
                .where(
                    IngestRadioTransition.id == self.id,
                    IngestRadioTransition.active.is_(True),
                    IngestRadioTransition.lease_owner == self.lease_owner,
                )
                .values(
                    phase=TransitionPhase.RECOVERY_REQUIRED.value,
                    recovery_required=True,
                    lease_expires_at=now,
                    updated_at=now,
                    heartbeat_at=now,
                    last_error=_error_text(error),
                )
            )
            if result.rowcount != 1:
                self._signal_lease_loss("exceptional restore lost durable ownership")

    async def _restore(self, *, error: object | None = None, finalize: bool) -> bool:
        """Restore exact baseline, then resume the logger and close the transition."""
        try:
            row = await self._row()
        except RadioTransitionError:
            return False

        errors: list[str] = []
        if error is not None:
            errors.append(_error_text(error))
        await self.checkpoint(TransitionPhase.RESTORING_RADIOS)

        radio_ok = True
        # Attempt restoration whenever a disable was *attempted*, not only when it was
        # verified: an ADB reply can be lost after the device applied the side effect.
        if row.bluetooth_disable_attempted:
            await self.checkpoint(bluetooth_restore_attempted=True)
            bluetooth_ok = await self.controller.restore_bluetooth(row.bluetooth_before)
            await self.checkpoint(bluetooth_restore_verified=bluetooth_ok)
            if not bluetooth_ok:
                errors.append("Bluetooth baseline restoration could not be verified")
                radio_ok = False

        # This is required even when the AP itself was never disabled.  Restoring an
        # originally-on Bluetooth stack can vendor-rearm an originally-off hotspot.
        if row.bluetooth_disable_attempted or row.hotspot_disable_attempted:
            await self.checkpoint(hotspot_restore_attempted=True)
            hotspot_config = self._hotspot_config
            if hotspot_config is None and row.hotspot_restore_ref:
                hotspot_config = await self.controller.read_hotspot_capsule(row.hotspot_restore_ref)
            hotspot_ok = await self.controller.restore_hotspot(
                row.hotspot_before,
                hotspot_config,
            )
            await self.checkpoint(hotspot_restore_verified=hotspot_ok)
            if not hotspot_ok:
                errors.append("hotspot baseline restoration could not be verified")
                radio_ok = False

        if radio_ok and (row.bluetooth_disable_attempted or row.hotspot_disable_attempted):
            watchdog_ok = await self.controller.stand_down_watchdog()
            if not watchdog_ok:
                errors.append("radio watchdog could not be safely disarmed")
                radio_ok = False

        resume_ok = True
        if row.logger_request_id:
            if radio_ok:
                await self.checkpoint(
                    TransitionPhase.RESUMING_OBD,
                    logger_resume_attempted=True,
                )
                resume_ok = bool(self.logger_status_path) and await obd_control.resume_logger(
                    self.address, self.logger_status_path or ""
                )
                await self.checkpoint(logger_resume_verified=resume_ok)
                if not resume_ok:
                    errors.append("OBD logger resume could not be verified")
            else:
                resume_ok = False
                errors.append("OBD logger remains quiesced until radios are restored")

        if radio_ok and resume_ok:
            if finalize:
                if row.hotspot_restore_ref is None and self._untracked_capsule_ref is not None:
                    await self.checkpoint(hotspot_restore_ref=self._untracked_capsule_ref)
                    row.hotspot_restore_ref = self._untracked_capsule_ref
                # Stop renewal before intentionally deactivating the row. Otherwise a
                # heartbeat racing the final UPDATE can observe active=False and report
                # a spurious lease loss during a successful close.
                await self._stop_heartbeat()
                clean = await self._finish_transition(error=error, errors=errors)
                # The active row is the recovery authority. Deactivate it only after the
                # logger resume is durable, then erase the capsule. A crash on either side
                # is recoverable: before deactivation startup repeats restore; afterwards
                # it finds the retained reference and performs idempotent cleanup only.
                if clean and row.hotspot_restore_ref:
                    await self._cleanup_capsule_after_finish(row.hotspot_restore_ref)
            else:
                await self.checkpoint(
                    TransitionPhase.INGESTING,
                    recovery_required=False,
                    last_error="; ".join(errors)[:2000] or None,
                )
                clean = True
        else:
            await self.checkpoint(
                TransitionPhase.RECOVERY_REQUIRED,
                recovery_required=True,
                lease_expires_at=_now(),
                last_error="; ".join(errors)[:2000],
            )
            clean = False
        return clean

    async def _cleanup_capsule_after_finish(self, path: str) -> None:
        try:
            removed = await self.controller.remove_hotspot_capsule(path)
        except Exception as exc:
            log.warning(
                "protected hotspot recovery capsule cleanup remains pending",
                transition_id=self.transition_id,
                error=_error_text(exc),
            )
            return
        if not removed:
            log.warning(
                "protected hotspot recovery capsule cleanup remains pending",
                transition_id=self.transition_id,
            )
            return
        try:
            async with session_scope() as session:
                await session.execute(
                    update(IngestRadioTransition)
                    .where(
                        IngestRadioTransition.id == self.id,
                        IngestRadioTransition.active.is_(False),
                        IngestRadioTransition.hotspot_restore_ref == path,
                    )
                    .values(hotspot_restore_ref=None, updated_at=_now())
                )
        except Exception as exc:
            # Removal is idempotent. Keeping the reference merely makes the next startup
            # prove absence and clear it again.
            log.warning(
                "could not mark the hotspot recovery capsule cleaned",
                transition_id=self.transition_id,
                error=_error_text(exc),
            )

    async def _finish_transition(
        self,
        *,
        error: object | None = None,
        errors: list[str] | None = None,
    ) -> bool:
        messages = list(errors or [])
        if error is not None and not messages:
            messages.append(_error_text(error))
        now = _now()
        async with session_scope() as session:
            result = await session.execute(
                update(IngestRadioTransition)
                .where(
                    IngestRadioTransition.id == self.id,
                    IngestRadioTransition.active.is_(True),
                    IngestRadioTransition.lease_owner == self.lease_owner,
                )
                .values(
                    active=False,
                    phase=(
                        TransitionPhase.FAILED.value
                        if error is not None
                        else TransitionPhase.COMPLETE.value
                    ),
                    completed_at=now,
                    updated_at=now,
                    heartbeat_at=now,
                    recovery_required=False,
                    last_error="; ".join(messages)[:2000] or None,
                )
            )
            finished = result.rowcount == 1
            if not finished:
                self._signal_lease_loss("transition finalisation lost durable ownership")
            return finished

    async def require_recovery(self, error: object) -> None:
        """Expire this owner after a bounded/cancelled restore so startup can adopt it."""
        with contextlib.suppress(Exception):
            await self.checkpoint(
                TransitionPhase.RECOVERY_REQUIRED,
                recovery_required=True,
                lease_expires_at=_now(),
                last_error=_error_text(error),
            )
        await self.close()

    async def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_heartbeat()
        deadline_task = self._deadline_task
        if deadline_task is not None and deadline_task is not asyncio.current_task():
            deadline_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await deadline_task
        try:
            await self.controller.release()
        finally:
            self.process_fence.release()


async def begin(
    *,
    trigger: str,
    address: str,
    logger_status: dict[str, Any] | None,
    logger_status_path: str | None,
    watchdog_deadline_s: int,
    lease_loss_callback: Callable[[], object] | None = None,
) -> RadioTransition:
    """Atomically claim the only active transition across all server processes."""
    watchdog_deadline_s = max(60, min(int(watchdog_deadline_s), MAX_WATCHDOG_DEADLINE_S))
    process_fence = try_acquire(_process_fence_path())
    if process_fence is None:
        raise TransitionBusy("another ingest process is still executing radio side effects")
    transition_id = str(uuid.uuid4())
    owner = str(uuid.uuid4())
    now = _now()
    capabilities = (
        list(logger_status.get("capabilities", []))
        if isinstance(logger_status, dict) and isinstance(logger_status.get("capabilities"), list)
        else []
    )
    controller = radios.RadioController(address, watchdog_deadline_s=watchdog_deadline_s)
    controller.claim()
    try:
        device_boot_id = await radios.read_device_boot_id(address)
        if device_boot_id is None:
            raise RadioTransitionError(
                "device boot identity could not be verified; radios were left on"
            )
        row = IngestRadioTransition(
            transition_id=transition_id,
            trigger=trigger[:32],
            phase=TransitionPhase.PREPARING.value,
            active=True,
            created_at=now,
            updated_at=now,
            heartbeat_at=now,
            lease_owner=owner,
            lease_expires_at=_lease_deadline(),
            device_address=address[:255],
            device_boot_id=device_boot_id,
            transport_host=address.partition(":")[0][:255],
            capabilities_json=capabilities,
            logger_status_path=logger_status_path,
            logger_quiesce_capable=obd_control.supports_quiesce(logger_status),
        )
        async with session_scope() as session:
            session.add(row)
            await session.flush()
            row_id = row.id
    except IntegrityError as exc:
        await controller.release()
        process_fence.release()
        raise TransitionBusy("another ingest process owns the device radios") from exc
    except BaseException:
        await controller.release()
        process_fence.release()
        raise

    transition = RadioTransition(
        id=row_id,
        transition_id=transition_id,
        lease_owner=owner,
        address=address,
        logger_status_path=logger_status_path,
        controller=controller,
        watchdog_deadline_s=watchdog_deadline_s,
        process_fence=process_fence,
        lease_loss_callback=lease_loss_callback,
    )
    transition.start_heartbeat()
    return transition


async def _active_row() -> IngestRadioTransition | None:
    async with session_scope() as session:
        return await session.scalar(
            select(IngestRadioTransition)
            .where(
                or_(
                    IngestRadioTransition.active.is_(True),
                    IngestRadioTransition.recovery_required.is_(True),
                )
            )
            .order_by(IngestRadioTransition.created_at.asc())
            .limit(1)
        )


async def pending_recovery_address() -> str | None:
    """Return the durable transition endpoint without contacting the head unit."""
    row = await _active_row()
    return row.device_address if row is not None else None


async def _adopt_expired(
    row: IngestRadioTransition,
    *,
    address: str,
    device_boot_id: str | None,
) -> RadioTransition | None:
    process_fence = try_acquire(_process_fence_path())
    if process_fence is None:
        # The lease can expire while its owner is blocked in a DB write, commit thread or
        # device syscall. The OS fence is the authoritative process-liveness signal.
        return None
    owner = str(uuid.uuid4())
    now = _now()
    try:
        async with session_scope() as session:
            result = await session.execute(
                update(IngestRadioTransition)
                .where(
                    IngestRadioTransition.id == row.id,
                    IngestRadioTransition.active.is_(True),
                    IngestRadioTransition.lease_expires_at <= now,
                )
                .values(
                    lease_owner=owner,
                    lease_expires_at=_lease_deadline(),
                    heartbeat_at=now,
                    updated_at=now,
                    device_address=address[:255],
                    device_boot_id=device_boot_id or row.device_boot_id,
                    transport_host=address.partition(":")[0][:255],
                )
            )
            if result.rowcount != 1:
                process_fence.release()
                return None
    except BaseException:
        process_fence.release()
        raise
    controller = radios.RadioController(address, watchdog_deadline_s=120)
    controller.claim()
    transition = RadioTransition(
        id=row.id,
        transition_id=row.transition_id,
        lease_owner=owner,
        address=address,
        logger_status_path=row.logger_status_path or get_config().obd_remote_status_file,
        controller=controller,
        watchdog_deadline_s=120,
        process_fence=process_fence,
    )
    transition.start_heartbeat()
    return transition


async def _verified_recovery_identity(
    row: IngestRadioTransition,
    address: str | None,
) -> tuple[str, str | None] | None:
    candidate = address or row.device_address
    boot_id = await radios.read_device_boot_id(candidate)
    if row.device_boot_id is not None:
        if not radios.same_device_identity(row.device_boot_id, boot_id):
            log.warning(
                "refusing radio recovery because the stable device identity changed",
                transition_id=row.transition_id,
            )
            return None
    elif candidate != row.device_address:
        # Legacy/unreadable identities may recover at the exact recorded endpoint only;
        # following them to another DHCP address could operate on a different device.
        return None
    return candidate, boot_id


async def _cleanup_inactive_capsules(address: str | None) -> None:
    """Best-effort cleanup for a crash after durable transition deactivation."""

    if address is None:
        return
    process_fence = try_acquire(_process_fence_path())
    if process_fence is None:
        return
    try:
        async with session_scope() as session:
            rows = list(
                (
                    await session.scalars(
                        select(IngestRadioTransition)
                        .where(
                            IngestRadioTransition.active.is_(False),
                            IngestRadioTransition.hotspot_restore_ref.is_not(None),
                        )
                        .order_by(IngestRadioTransition.completed_at.asc())
                        .limit(25)
                    )
                ).all()
            )
        for row in rows:
            identity = await _verified_recovery_identity(row, address)
            if identity is None or not row.hotspot_restore_ref:
                continue
            recovery_address, _boot_id = identity
            controller = radios.RadioController(recovery_address, watchdog_deadline_s=120)
            if await controller.remove_hotspot_capsule(row.hotspot_restore_ref):
                async with session_scope() as session:
                    await session.execute(
                        update(IngestRadioTransition)
                        .where(
                            IngestRadioTransition.id == row.id,
                            IngestRadioTransition.active.is_(False),
                            IngestRadioTransition.hotspot_restore_ref == row.hotspot_restore_ref,
                        )
                        .values(hotspot_restore_ref=None, updated_at=_now())
                    )
    finally:
        process_fence.release()


async def reconcile_pending(
    *,
    address: str | None = None,
    wait_for_lease: bool = False,
) -> bool:
    """Restore one interrupted transition. Safe before settings/poller enable checks."""
    await _cleanup_inactive_capsules(address)
    row = await _active_row()
    if row is None:
        return True
    identity = await _verified_recovery_identity(row, address)
    if identity is None:
        return False
    recovery_address, device_boot_id = identity

    if row.lease_expires_at > _now() and wait_for_lease:
        remaining = (row.lease_expires_at - _now()).total_seconds()
        await asyncio.sleep(max(0.0, min(LEASE_TTL_S + 1.0, remaining + 0.1)))
        row = await _active_row()
        if row is None:
            return True
        identity = await _verified_recovery_identity(row, address)
        if identity is None:
            return False
        recovery_address, device_boot_id = identity
    transition = await _adopt_expired(
        row,
        address=recovery_address,
        device_boot_id=device_boot_id,
    )
    if transition is None:
        return False

    # A crash between protecting the credentials and checkpointing their opaque path
    # leaves no radio side effect, but it can leave a mode-0600 capsule. Its path is a
    # pure function of the transition UUID, so track it before any recovery finalises.
    if (
        row.hotspot_restore_ref is None
        and row.hotspot_before == "unknown"
        and not row.bluetooth_disable_attempted
        and not row.hotspot_disable_attempted
    ):
        candidate = radios.hotspot_capsule_path(row.transition_id)
        if candidate is not None:
            await transition.checkpoint(hotspot_restore_ref=candidate)

    log.warning(
        "reconciling an interrupted ingest radio transition",
        transition_id=row.transition_id,
        phase=row.phase,
    )
    try:
        return await transition.restore(error="server restarted during radio transition")
    except Exception as exc:
        with contextlib.suppress(Exception):
            await transition.checkpoint(
                TransitionPhase.RECOVERY_REQUIRED,
                recovery_required=True,
                lease_expires_at=_now(),
                last_error=_error_text(exc),
            )
        await transition.close()
        log.warning("radio transition recovery remains pending", error=_error_text(exc))
        return False


async def reconcile_startup() -> bool:
    """Bounded startup reconciliation, intentionally independent of ingest.enabled."""
    # Do not hold the application's health endpoint behind a lease that may belong to a
    # second healthy process. An expired row is restored now; a still-live lease blocks
    # new radio ownership, and the arrival path retries as soon as it expires.
    return await reconcile_pending()


__all__ = [
    "RadioTransition",
    "RadioTransitionError",
    "TransitionBusy",
    "TransitionPhase",
    "begin",
    "pending_recovery_address",
    "reconcile_pending",
    "reconcile_startup",
]
