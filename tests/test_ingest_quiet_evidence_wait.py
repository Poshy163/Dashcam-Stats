"""A first logger heartbeat may race automatic radio preparation after ACC-off."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ingest_preparation_lifecycle import isolated_mirror as _isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as _prepared_unit

from app.ingest import adb, puller
from app.ingest.models import RunState
from app.ingest.status import get_status, reset_status_for_tests

isolated_mirror = _isolated_mirror
prepared_unit = _prepared_unit
BOOT = "09255025-8fa6-4dd4-bede-607e8a2603bc"
LOGGER = {
    "state": "parked",
    "ownership_enabled": True,
    "capabilities": [puller.QUIET_EVIDENCE_CAPABILITY, puller.obd_control.CAPABILITY],
}


def power(*, evidence=False, window=1200):
    return adb.RuntimeObservation(
        BOOT,
        120,
        "off",
        window,
        20,
        adb.SleepDeadlineEvidence(1, BOOT, 20, 119_000, False, 100_000, 105_000, window)
        if evidence
        else None,
    )


@pytest.fixture
def grace(monkeypatch):
    reset_status_for_tests()
    status = get_status()
    assert status.try_begin()
    status.observe_unit_runtime(power())
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_WAIT_S", 0.16)
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_RETRY_S", 0.01)
    monkeypatch.setattr(puller, "_QUIET_EVIDENCE_PROBE_BUDGET_S", 0.005)
    monkeypatch.setattr(
        puller, "get_config", lambda: SimpleNamespace(obd_remote_status_file="/status")
    )
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=LOGGER.copy()))
    return status


@pytest.mark.parametrize("window,allowed", [(1200, True), (60, False)])
async def test_known_deadline_never_waits_or_weakens_minimum(grace, monkeypatch, window, allowed):
    grace.observe_unit_runtime(power(evidence=True, window=window))
    read = AsyncMock(side_effect=AssertionError("already has a deadline"))
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    assert await puller._await_quieting_evidence("unit", LOGGER) == LOGGER
    assert grace.radio_quieting_allowed() is allowed
    read.assert_not_awaited()
    puller.read_logger_status.assert_not_awaited()


async def test_delayed_publication_is_accepted_with_latest_ownership(grace, monkeypatch):
    read = AsyncMock(side_effect=[power(), power(evidence=True)])
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    latest = {**LOGGER, "ownership_enabled": False}
    puller.read_logger_status.side_effect = [LOGGER, latest]
    assert await puller._await_quieting_evidence("unit", LOGGER) == latest
    assert grace.radio_quieting_allowed()
    assert read.await_count == 2
    read.assert_awaited_with("unit", logger_status_path="/status")
    assert grace.snapshot()["sleep_countdown_evidence_source"] == "unit"


@pytest.mark.parametrize("missing_after_error", [False, True])
async def test_disappearing_logger_never_erases_observed_bluetooth_ownership(
    grace, monkeypatch, missing_after_error
):
    reads = [power(), power(evidence=True)] if missing_after_error else [power(evidence=True)]
    logger_reads = [RuntimeError("unreadable"), None] if missing_after_error else [None]
    monkeypatch.setattr(puller.adb, "runtime_observation", AsyncMock(side_effect=reads))
    puller.read_logger_status.side_effect = logger_reads
    latest = await puller._await_quieting_evidence("unit", LOGGER)
    assert grace.radio_quieting_allowed(), "the timer alone must not authorize radio ownership"
    assert not puller._obd_logger_status_is_authoritative(latest)


@pytest.mark.parametrize("capabilities", [None, False, 7, "sleep_deadline_evidence_v1", {}])
async def test_malformed_capabilities_do_not_start_the_grace(grace, monkeypatch, capabilities):
    read = AsyncMock(side_effect=AssertionError("malformed capability advertisement"))
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    logger = {**LOGGER, "capabilities": capabilities}
    assert await puller._await_quieting_evidence("unit", logger) == logger
    read.assert_not_awaited()


async def test_unpublished_edge_times_out_without_inventing_deadline(grace, monkeypatch):
    read = AsyncMock(return_value=power())
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    started = time.monotonic()
    await puller._await_quieting_evidence("unit", LOGGER)
    elapsed = time.monotonic() - started
    assert 0.10 <= elapsed < 0.5
    assert read.await_count > 1
    assert grace.snapshot()["sleep_countdown_source"] == "unknown"
    assert not grace.radio_quieting_allowed()


@pytest.mark.parametrize("kind", ["on", "unknown", "reboot", "failed", "short"])
async def test_changed_authority_ends_grace_without_quieting(grace, monkeypatch, kind):
    observation = {
        "on": replace(power(), ignition_state="on"),
        "unknown": replace(power(), ignition_state="unknown"),
        "reboot": replace(power(), boot_count=21, uptime_s=1),
        "failed": None,
        "short": power(evidence=True, window=60),
    }[kind]
    read = AsyncMock(return_value=observation)
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    await puller._await_quieting_evidence("unit", LOGGER)
    assert read.await_count == 1
    assert not grace.radio_quieting_allowed()
    assert grace.cancel_event.is_set() is (kind == "reboot")


@pytest.mark.parametrize("task_cancel", [False, True])
async def test_cancel_interrupts_retry_without_another_probe(grace, monkeypatch, task_cancel):
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_WAIT_S", 15)
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_RETRY_S", 2)
    read = AsyncMock(return_value=power())
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    task = asyncio.create_task(puller._await_quieting_evidence("unit", LOGGER))
    while read.await_count == 0:
        await asyncio.sleep(0)
    # Let both bounded reads settle, then stop during the retry delay.
    await asyncio.sleep(0.01)
    if task_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.25)
    else:
        grace.cancel()
        await asyncio.wait_for(task, 0.25)
    assert read.await_count == 1


async def test_old_logger_skips_evidence_grace(grace, monkeypatch):
    logger = {**LOGGER, "capabilities": []}
    read = AsyncMock(side_effect=AssertionError("older logger cannot supply evidence"))
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    await puller._await_quieting_evidence("unit", logger)
    read.assert_not_awaited()


@pytest.mark.parametrize("case", ["failed_observation", "ignition_on", "stale"])
async def test_unknown_admission_refreshes_even_when_cached_power_is_not_fresh_off(
    grace, monkeypatch, case
):
    if case == "failed_observation":
        grace.unit_observation_failed()
    elif case == "ignition_on":
        grace.observe_unit_runtime(replace(power(), ignition_state="on"))
        # The ordinary parked gate already proved OFF, but must not manufacture a
        # full timer or allow this older runtime snapshot to suppress the refresh.
        grace.set_ignition(held=False, reason=None, state="off")
    else:
        grace._unit_observed_monotonic -= 31
    read = AsyncMock(return_value=power(evidence=True))
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    latest = {**LOGGER, "ownership_enabled": False}
    puller.read_logger_status.return_value = latest
    assert await puller._await_quieting_evidence("unit", LOGGER) == latest
    read.assert_awaited_once()
    assert grace.radio_quieting_allowed()


async def test_fresh_read_still_on_does_not_wait_or_authorize_quieting(grace, monkeypatch):
    grace.observe_unit_runtime(replace(power(), ignition_state="on"))
    read = AsyncMock(return_value=replace(power(), ignition_state="on"))
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    await puller._await_quieting_evidence("unit", LOGGER)
    read.assert_awaited_once()
    assert not grace.radio_quieting_allowed()


async def test_automatic_run_waits_before_claim_and_uses_refreshed_ownership(
    prepared_unit, monkeypatch
):
    prepared_unit.options["quiet_radios"] = True
    status = get_status()
    status.observe_unit_runtime(power())
    latest = {**LOGGER, "ownership_enabled": False}
    monkeypatch.setattr(
        puller, "read_logger_status", AsyncMock(side_effect=[LOGGER, latest, latest])
    )
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_RETRY_S", 0.01)
    read = AsyncMock(side_effect=[power(), power(evidence=True)])
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    monkeypatch.setattr(puller.radios, "QUIET_AFTER_ONLINE_S", 0)

    async def claim(**kwargs):
        assert read.await_count == 2
        assert status.radio_quieting_allowed()
        assert kwargs["logger_status"] == latest
        raise puller.radio_coordinator.RadioTransitionError("test stops before any radio control")

    begin = AsyncMock(side_effect=claim)
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    result = await asyncio.wait_for(puller.run_pull(info=prepared_unit.info), 1)
    assert result.state is RunState.OK
    begin.assert_awaited_once()
    assert prepared_unit.path.read_bytes() == prepared_unit.payload


async def test_pending_recovery_precedes_first_evidence_wait(prepared_unit, monkeypatch):
    prepared_unit.options["quiet_radios"] = True
    get_status().observe_unit_runtime(power())
    monkeypatch.setattr(puller, "_recover_before_run", AsyncMock(return_value=False))
    probe = AsyncMock(side_effect=AssertionError("recovery must precede new preparation"))
    monkeypatch.setattr(puller, "_await_quieting_evidence", probe)
    begin = AsyncMock(side_effect=AssertionError("no new transition before recovery"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    result = await puller.run_pull(info=prepared_unit.info)
    assert result.state is RunState.IDLE
    assert not prepared_unit.transferred.is_set()
    probe.assert_not_awaited()
    begin.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_unresolved_grace_never_claims_radios_and_respects_stop(
    prepared_unit, monkeypatch, cancel
):
    prepared_unit.options["quiet_radios"] = True
    status = get_status()
    status.observe_unit_runtime(power())
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_WAIT_S", 0.10)
    monkeypatch.setattr(puller, "QUIET_EVIDENCE_RETRY_S", 0.01)
    monkeypatch.setattr(puller, "_QUIET_EVIDENCE_PROBE_BUDGET_S", 0.005)
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=LOGGER))

    async def probe(*args, **kwargs):
        if cancel:
            status.cancel()
        return power()

    read = AsyncMock(side_effect=probe)
    monkeypatch.setattr(puller.adb, "runtime_observation", read)
    begin = AsyncMock(side_effect=AssertionError("no usable deadline"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    result = await asyncio.wait_for(puller.run_pull(info=prepared_unit.info), 1)
    assert result.state is (RunState.CANCELLED if cancel else RunState.OK)
    assert prepared_unit.transferred.is_set() is (not cancel)
    assert status.radio_quieting_hold is (not cancel)
    assert read.await_count >= 1
    begin.assert_not_awaited()
