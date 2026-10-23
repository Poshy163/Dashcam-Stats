"""A newly live companion may refresh an original OFF proof, never its timer."""

import json
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb
from app.ingest import status as status_module
from app.ingest.status import IngestStatus

BOOT = "01234567-1234-1234-1234-012345678901"
STATUS_PATH = "/storage/emulated/0/Android/data/com.dashcamstats.obdlogger/files/status.json"


def _runtime_reply(uptime_s, *, evidence_elapsed_ms=None, current_window=300):
    # The edge and latched duration are immutable. Current native policy can be
    # shorter after the app switched back to idle policy, including across updates.
    value = None
    if evidence_elapsed_ms is not None:
        value = {
            "schema_version": 1,
            "boot_id": BOOT,
            "boot_count": 12,
            "observed_elapsed_ms": evidence_elapsed_ms,
            "ignition_on": False,
            "off_lower_elapsed_ms": 800_000,
            "off_upper_elapsed_ms": 806_000,
            "window_s": 1200,
        }
    raw = json.dumps({"sleep_deadline_evidence": value})
    return (
        f"{BOOT}\n12\n{uptime_s - 0.5} 0\n0\n{current_window}\n"
        f"__DASHCAM_STATUS_START__\n{raw}\n__DASHCAM_STATUS_END__\n"
        f"{uptime_s} 0\n0\n12\n{BOOT}\n"
    )


@pytest.fixture
def clock(monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(status_module.time, "monotonic", lambda: now[0])
    return now


async def _observe(status):
    runtime = await adb.runtime_observation("unit", logger_status_path=STATUS_PATH)
    assert runtime is not None
    status.observe_unit_runtime(runtime)


@pytest.mark.parametrize("during_restart_elapsed_ms", [None, 970_000])
async def test_process_restart_requires_new_live_proof_and_preserves_original_ceiling(
    monkeypatch, clock, during_restart_elapsed_ms
):
    shell = AsyncMock(
        side_effect=[
            _runtime_reply(1000, evidence_elapsed_ms=999_000),
            _runtime_reply(1005, evidence_elapsed_ms=during_restart_elapsed_ms),
            _runtime_reply(1010, evidence_elapsed_ms=1009_000),
        ]
    )
    monkeypatch.setattr(adb, "shell", shell)
    status = IngestStatus()
    await _observe(status)
    assert status.radio_quieting_allowed()
    assert status.sleep_deadline_uptime_s() == 1997

    clock[0] += 5
    await _observe(status)
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()
    assert status.sleep_countdown_remaining_s() == 992

    clock[0] += 5
    await _observe(status)
    assert status.radio_quieting_allowed()
    assert status.snapshot()["sleep_countdown_remaining_s"] == 987
    assert status.sleep_deadline_uptime_s() == 1997
    # Updating the application/read time is not an ignition transition or a
    # restart of either the old 1200-second timer or current 300-second policy.
    assert shell.await_count == 3


async def test_server_restart_can_admit_freshly_restored_original_device_edge(monkeypatch, clock):
    monkeypatch.setattr(
        adb, "shell", AsyncMock(return_value=_runtime_reply(1110, evidence_elapsed_ms=1109_000))
    )
    status = IngestStatus()  # No server-side observed ON edge or cached countdown.
    await _observe(status)
    assert status.radio_quieting_allowed()
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_evidence_source"] == "unit"
    assert snapshot["sleep_countdown_remaining_s"] == 887
    assert status.sleep_deadline_uptime_s() == 1997


async def test_republished_checkpoint_cannot_turn_expired_original_window_into_new_budget(
    monkeypatch, clock
):
    monkeypatch.setattr(
        adb, "shell", AsyncMock(return_value=_runtime_reply(2100, evidence_elapsed_ms=2099_000))
    )
    status = IngestStatus()
    await _observe(status)
    assert status.snapshot()["sleep_countdown_remaining_s"] == 0
    assert not status.radio_quieting_allowed()
    assert status.sleep_countdown_remaining_s() == 0


@pytest.mark.parametrize("current_window", [None, 300, 1200])
async def test_fresh_power_alone_during_update_does_not_recover_missing_edge(
    monkeypatch, clock, current_window
):
    monkeypatch.setattr(
        adb, "shell", AsyncMock(return_value=_runtime_reply(1110, current_window=current_window))
    )
    status = IngestStatus()
    await _observe(status)
    assert status.snapshot()["unit_observation_fresh"]
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()
    assert status.sleep_deadline_uptime_s() is None
