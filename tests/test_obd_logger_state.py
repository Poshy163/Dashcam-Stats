from __future__ import annotations

import pytest

from app.obd.logger import (
    EngineGate,
    LoggerSettings,
    LoggerState,
    OwnershipNotTransferred,
)


def test_obd_logger_requires_explicit_ownership_transfer() -> None:
    gate = EngineGate(LoggerSettings())
    with pytest.raises(OwnershipNotTransferred, match="Home Assistant"):
        gate.enable()
    assert gate.state is LoggerState.DISABLED


def test_obd_logger_start_requires_voltage_then_explicit_ecu_proof() -> None:
    gate = EngineGate(LoggerSettings(ownership_transferred=True))
    gate.enable()
    assert gate.parked_voltage(12.7) is False
    assert gate.state is LoggerState.PARKED
    assert gate.parked_voltage(13.2) is True
    assert gate.state is LoggerState.PROBING
    gate.ecu_proved()
    assert gate.state is LoggerState.ECU_ONLINE


def test_obd_logger_recent_rpm_vetoes_temporary_voltage_dip() -> None:
    gate = EngineGate(
        LoggerSettings(
            ownership_transferred=True,
            off_grace_s=30,
            recent_rpm_s=30,
        )
    )
    gate.enable()
    assert gate.parked_voltage(13.3)
    gate.ecu_proved()
    assert gate.live_evidence(now_s=0, voltage=13.5, rpm=850)
    assert gate.live_evidence(now_s=10, voltage=12.8, rpm=None)
    assert gate.live_evidence(now_s=31, voltage=12.8, rpm=None)
    assert gate.live_evidence(now_s=60, voltage=12.8, rpm=None)
    assert gate.live_evidence(now_s=91, voltage=12.8, rpm=None) is False
    assert gate.state is LoggerState.PARKED


def test_obd_logger_failed_probe_enters_bounded_backoff_state() -> None:
    gate = EngineGate(LoggerSettings(ownership_transferred=True))
    gate.enable()
    gate.parked_voltage(13.4)
    gate.probe_failed()
    assert gate.state is LoggerState.BACKOFF
    gate.backoff_elapsed()
    assert gate.state is LoggerState.PARKED


def test_obd_logger_missing_voltage_and_rpm_eventually_stops() -> None:
    gate = EngineGate(LoggerSettings(ownership_transferred=True, off_grace_s=30, recent_rpm_s=30))
    gate.enable()
    gate.parked_voltage(13.4)
    gate.ecu_proved()
    assert gate.live_evidence(now_s=0, voltage=13.4, rpm=900)
    assert gate.live_evidence(now_s=31, voltage=None, rpm=None)
    assert gate.live_evidence(now_s=60, voltage=None, rpm=None)
    assert gate.live_evidence(now_s=62, voltage=None, rpm=None) is False
