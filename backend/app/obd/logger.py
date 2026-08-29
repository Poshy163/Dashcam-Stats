"""Ownership and engine-state rules for the dashcam logger."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LoggerState(str, Enum):
    DISABLED = "disabled"
    PARKED = "parked"
    PROBING = "probing"
    ECU_ONLINE = "ecu_online"
    BACKOFF = "backoff"


class OwnershipNotTransferred(RuntimeError):
    """Direct BLE remains owned by Home Assistant or a phone."""


@dataclass(frozen=True, slots=True)
class LoggerSettings:
    ownership_transferred: bool = False
    voltage_on: float = 13.2
    voltage_off: float = 13.0
    off_grace_s: float = 30.0
    recent_rpm_s: float = 30.0

    def validate(self) -> None:
        if self.voltage_off >= self.voltage_on:
            raise ValueError("voltage_off must be lower than voltage_on")
        if self.off_grace_s < 1 or self.recent_rpm_s < 1:
            raise ValueError("grace periods must be positive")


class EngineGate:
    """Voltage-gated transitions with a recent RPM veto for temporary dips."""

    def __init__(self, settings: LoggerSettings) -> None:
        settings.validate()
        self.settings = settings
        self.state = LoggerState.DISABLED
        self._below_since: float | None = None
        self._last_running_rpm: float | None = None

    def enable(self) -> None:
        if not self.settings.ownership_transferred:
            raise OwnershipNotTransferred(
                "turn off Home Assistant's OBD connection and stop phone scanners first"
            )
        self.state = LoggerState.PARKED

    def parked_voltage(self, voltage: float | None) -> bool:
        """Return whether a checksum-valid 0100 probe should now be attempted."""
        if self.state is not LoggerState.PARKED or voltage is None:
            return False
        if voltage >= self.settings.voltage_on:
            self.state = LoggerState.PROBING
            return True
        return False

    def ecu_proved(self) -> None:
        if self.state is not LoggerState.PROBING:
            raise RuntimeError("ECU may only become online after an explicit probe")
        self.state = LoggerState.ECU_ONLINE
        self._below_since = None

    def probe_failed(self) -> None:
        self.state = LoggerState.BACKOFF

    def backoff_elapsed(self) -> None:
        self.state = LoggerState.PARKED

    def live_evidence(self, *, now_s: float, voltage: float | None, rpm: float | None) -> bool:
        """Return true while driving; false only after a sustained, unvetoed stop."""
        if self.state is not LoggerState.ECU_ONLINE:
            return False
        if rpm is not None and rpm > 300:
            self._last_running_rpm = now_s
            self._below_since = None
            return True
        if voltage is not None and voltage >= self.settings.voltage_off:
            self._below_since = None
            return True
        if (
            self._last_running_rpm is not None
            and now_s - self._last_running_rpm <= self.settings.recent_rpm_s
        ):
            return True
        if self._below_since is None:
            self._below_since = now_s
            return True
        if now_s - self._below_since < self.settings.off_grace_s:
            return True
        self.state = LoggerState.PARKED
        self._below_since = None
        return False
