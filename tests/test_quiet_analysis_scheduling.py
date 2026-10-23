"""Give the real publisher a bounded unquieted opportunity before bulk backup."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from test_ingest_preparation_lifecycle import isolated_mirror as isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as prepared_unit
from test_parked_filter import (
    SOURCE,
    make_deferred_policy,
    make_policy,
    policy_inventory,
)
from test_parked_filter import (
    policy as policy,
)

from app.ingest import movement, parked_filter, puller, radios, transport
from app.ingest.adb import RuntimeObservation, SleepDeadlineEvidence
from app.ingest.models import Phase, RemoteFile, RunState, UnitInfo, UnitState
from app.ingest.status import get_status, reset_status_for_tests


def ready_logger(**changes):
    return {
        "updated_at_utc": datetime.now(UTC).isoformat(),
        "capabilities": [parked_filter.CAPABILITY],
        "ownership_enabled": True,
        "movement_state": "ready",
        "transfer_policy_enabled": True,
        "transfer_policy_state": "ready",
        "acc_state_known": True,
        "acc_on": False,
        "engine_running": False,
        "head_unit_state": "awake",
        "current_drive_id": None,
        "ingestion_sleep_hold_known": True,
        "ingestion_sleep_hold": False,
        **changes,
    }


def native_observation(*, age=300, remaining=900):
    return RuntimeObservation(
        "boot",
        1000,
        "off",
        age + remaining,
        sleep_deadline_evidence=SleepDeadlineEvidence(
            1,
            "boot",
            None,
            1_000_000,
            False,
            (1000 - age) * 1000,
            (1000 - age) * 1000 + 1000,
            age + remaining,
        ),
    )


def current_policy():
    # The complete past/post guards already exist; only visual analysis is pending.
    doc = make_deferred_policy(now=datetime.now(UTC) - timedelta(seconds=150))
    doc["generated_at_utc"] = datetime.now(UTC).isoformat()
    doc["producer_version"] = "0.3.15"
    return doc


def complete_policy(pending):
    doc = copy.deepcopy(pending)
    for entry, template in zip(doc["decisions"], make_policy()["decisions"], strict=True):
        entry.update(decision="skip", reason="parked_visual_quiet", defer_until_utc=None)
        entry["clip"]["duration_ms"] = 60_000
        entry["visual"] = template["visual"]
        entry["visual"]["analysis_completed_at_utc"] = datetime.now(UTC).isoformat()
        guard = entry["guard"]
        last = movement._timestamp(entry["clip"]["end_utc"]) + timedelta(seconds=122)
        guard["end_utc"] = last.isoformat()
        guard["window_ms"] = guard["acc_off_ms"] = round(
            (last - movement._timestamp(guard["start_utc"])).total_seconds() * 1000
        )
        guard["observation_count"] = guard["window_ms"] // 5000 + 2
    return doc


@pytest.mark.parametrize(
    "changes",
    [
        {"capabilities": []},
        {"movement_state": "unavailable"},
        {"transfer_policy_enabled": False},
        {"transfer_policy_state": "disabled"},
        {"acc_state_known": False},
        {"acc_on": True},
        {"engine_running": True},
        {"head_unit_state": "sleeping"},
        {"current_drive_id": "active"},
        {"ingestion_sleep_hold_known": False},
        {"ingestion_sleep_hold": True},
        {"updated_at_utc": (datetime.now(UTC) - timedelta(seconds=31)).isoformat()},
    ],
)
def test_only_fresh_parked_unquieted_logger_can_receive_an_opportunity(changes):
    assert puller._logger_can_analyze(ready_logger())
    assert not puller._logger_can_analyze(ready_logger(**changes))


@pytest.mark.parametrize("remaining", [None, 0, 60, 240, float("nan"), float("inf")])
def test_unknown_or_short_native_window_cannot_fund_analysis(remaining):
    snapshot = {
        "unit_observation_fresh": True,
        "ignition_state": "off",
        "sleep_countdown_source": "estimated",
        "sleep_countdown_remaining_s": remaining,
        "ignition_off_at": datetime.now(UTC).isoformat(),
    }
    assert puller._analysis_native_budget(snapshot) is None


async def test_actual_pending_publisher_contract_finishes_before_any_quiesce_or_bulk(
    prepared_unit, policy, monkeypatch
):
    unit = prepared_unit
    unit.info = UnitInfo("unit:5555", UnitState.DEVICE, SOURCE)
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    pending = current_policy()
    for entry in pending["decisions"]:
        entry["clip"]["size_bytes"] = len(unit.payload)
    quiet = complete_policy(pending)
    remote = policy_inventory(pending)
    unit.inventory.return_value = remote
    state = get_status()
    state.observe_unit_runtime(native_observation())
    begin = AsyncMock(side_effect=AssertionError("analysis must not claim/quiesce radios"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    monkeypatch.setattr(puller.movement, "inspect_before_plan", AsyncMock())
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=ready_logger()))
    monkeypatch.setattr(
        puller.adb, "runtime_observation", AsyncMock(return_value=native_observation())
    )
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_RECHECK_S", 0.001)
    observations = 0

    async def publisher(*_args):
        nonlocal observations
        observations += 1
        if observations == 1:
            return pending
        # This is the production canAnalyze predicate's public state: no drive,
        # parked, ingestion request absent. Neither a lease nor bulk has started.
        assert puller._logger_can_analyze(ready_logger())
        if observations == 2:
            assert state.phase is Phase.ANALYSING
        assert not unit.transferred.is_set()
        begin.assert_not_awaited()
        return quiet

    monkeypatch.setattr(parked_filter, "_read_policy", AsyncMock(side_effect=publisher))
    monkeypatch.setattr(movement, "read_remote_card_id", AsyncMock(return_value=pending["card_id"]))
    policy.fingerprints.return_value = {entry["clip"]["clip_id"] for entry in quiet["decisions"]}
    result = await asyncio.wait_for(puller.run_pull(info=unit.info), timeout=2)
    assert result.state is RunState.IDLE
    assert not unit.transferred.is_set()
    assert policy.record.await_args.kwargs["excluded"] == quiet["decisions"]
    assert state.snapshot()["policy_deferred_files"] == 0
    assert state.snapshot()["analysis_wait_until_utc"] is None
    begin.assert_not_awaited()


@pytest.mark.parametrize(
    "reason",
    ["system_thermal_guard", "cpu_temperature_unavailable", "frame_quality", "decoder_failure"],
)
async def test_terminal_resource_probe_image_or_decoder_refusal_does_not_wait(
    prepared_unit, policy, monkeypatch, reason
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    state = get_status()
    state.observe_unit_runtime(native_observation(age=400, remaining=800))
    doc = current_policy()
    doc["decisions"][0]["visual"].update(
        status="error" if reason == "decoder_failure" else "unavailable", reason=reason
    )
    doc["decisions"][0].update(
        decision="upload",
        reason="analysis_error" if reason == "decoder_failure" else "visual_incomplete",
        defer_until_utc=None,
    )
    remote = policy_inventory(doc)
    monkeypatch.setattr(parked_filter, "_read_policy", AsyncMock(return_value=doc))
    monkeypatch.setattr(movement, "read_remote_card_id", AsyncMock(return_value=doc["card_id"]))
    filtered = await parked_filter.filter_inventory(
        "unit:5555",
        "/status",
        remote,
        SOURCE,
        ready_logger(),
        enabled=True,
        native_remaining_s=1000,
    )
    assert filtered.analysis_terminal
    refresh = AsyncMock(side_effect=AssertionError("terminal refusal must not consume a wait"))
    monkeypatch.setattr(puller.adb, "runtime_observation", refresh)
    result = await puller._quiet_analysis_handoff(
        unit.info,
        [unit.info.source],
        unit.inventory.return_value,
        ready_logger(),
        filtered,
        footage=unit.path.parent,
        seen=set(),
    )
    assert result[-1] is False
    refresh.assert_not_awaited()


async def test_first_park_opportunity_uses_original_off_anchor_and_short_window_falls_back(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    state = get_status()
    state.observe_unit_runtime(native_observation(age=481, remaining=719))
    filtered = parked_filter.FilterResult(unit.inventory.return_value, analysis_ready=True)
    refresh = AsyncMock(side_effect=AssertionError("old OFF edge must not earn a new first window"))
    monkeypatch.setattr(puller.adb, "runtime_observation", refresh)
    for _ in range(2):
        result = await puller._quiet_analysis_handoff(
            unit.info,
            [unit.info.source],
            unit.inventory.return_value,
            ready_logger(),
            filtered,
            footage=unit.path.parent,
            seen=set(),
        )
        assert result[-1] is False
    refresh.assert_not_awaited()
    # A fresh short native timer has no spare budget after restoration reserve.
    state.observe_unit_runtime(native_observation(age=10, remaining=239))
    assert (
        await puller._quiet_analysis_handoff(
            unit.info,
            [unit.info.source],
            unit.inventory.return_value,
            ready_logger(),
            filtered,
            footage=unit.path.parent,
            seen=set(),
        )
    )[-1] is False


async def test_first_pair_staggered_gap_at_off_250_keeps_original_off_480_deadline(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    state = get_status()
    state.observe_unit_runtime(native_observation(age=250, remaining=950))
    original_off = movement._timestamp(state.snapshot()["ignition_off_at"])
    filtered = parked_filter.FilterResult(unit.inventory.return_value, analysis_ready=True)
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_RECHECK_S", 0.001)

    async def interrupted_probe(*_args, **_kwargs):
        snapshot = state.snapshot()
        assert snapshot["phase"] == "analysing"
        assert snapshot["analysis_wait_reason"] == "awaiting_parked_clip"
        until = movement._timestamp(snapshot["analysis_wait_until_utc"])
        assert until == original_off + timedelta(seconds=480)
        return None  # The optional wait fails open if the next camera read is absent.

    refresh = AsyncMock(side_effect=interrupted_probe)
    monkeypatch.setattr(puller.adb, "runtime_observation", refresh)
    result = await asyncio.wait_for(
        puller._quiet_analysis_handoff(
            unit.info,
            [unit.info.source],
            unit.inventory.return_value,
            ready_logger(),
            filtered,
            footage=unit.path.parent,
            seen=set(),
        ),
        timeout=1,
    )
    assert result[-1] is True
    refresh.assert_awaited_once()
    assert state.snapshot()["analysis_wait_until_utc"] is None


async def test_operator_stop_during_analysis_never_claims_or_starts_bulk(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    state = get_status()
    state.observe_unit_runtime(native_observation(age=10, remaining=1190))
    filtered = parked_filter.FilterResult(unit.inventory.return_value, analysis_ready=True)
    state.try_begin()
    pending = asyncio.create_task(
        puller._quiet_analysis_handoff(
            unit.info,
            [unit.info.source],
            unit.inventory.return_value,
            ready_logger(),
            filtered,
            footage=unit.path.parent,
            seen=set(),
        )
    )
    readiness_deadline = asyncio.get_running_loop().time() + 1
    while state.phase is not Phase.ANALYSING and not pending.done():
        assert asyncio.get_running_loop().time() < readiness_deadline
        await asyncio.sleep(0)
    assert state.phase is Phase.ANALYSING, pending.result() if pending.done() else None
    state.cancel()
    assert (await asyncio.wait_for(pending, timeout=1))[-1] is True
    assert not unit.transferred.is_set()
    assert state.snapshot()["analysis_wait_until_utc"] is None


async def test_completed_chunk_can_yield_without_launching_the_next_listener(tmp_path, monkeypatch):
    reset_status_for_tests()
    from app.ingest.models import RemoteFile

    files = [RemoteFile(f"2026102300000{i}_camera_0.ts", 10, 0) for i in range(3)]
    monkeypatch.setattr(puller, "_get", lambda _key, default=None: default)
    launch = AsyncMock(return_value=object())
    monkeypatch.setattr(puller.adb, "clear_listener", AsyncMock())
    monkeypatch.setattr(puller.adb, "launch_listener", launch)
    monkeypatch.setattr(puller.adb, "stop_listener", AsyncMock(return_value=True))
    monkeypatch.setattr(
        puller.transport,
        "receive",
        Mock(
            return_value=transport.TransferResult(
                files=[files[0].name],
                bytes_received=10,
                complete=True,
            )
        ),
    )
    completed = AsyncMock()
    result = await puller._move(
        UnitInfo("unit:5555", UnitState.DEVICE, SOURCE),
        files,
        staging=tmp_path,
        host="unit",
        port=9000,
        timeout_s=10,
        chunk_size=1,
        on_chunk_completed=completed,
        yield_after_chunk=AsyncMock(return_value=True),
    )
    assert result.complete and result.files == [files[0].name]
    assert result.error is None
    launch.assert_awaited_once()
    completed.assert_awaited_once_with([files[0]])


@pytest.mark.parametrize("pending_pair", [False, True])
async def test_unrelated_terminal_or_already_local_activity_cannot_starve_an_opportunity(
    prepared_unit, monkeypatch, pending_pair
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    state = get_status()
    state.observe_unit_runtime(native_observation(age=10 if not pending_pair else 300))
    terminal = RemoteFile("20261023000100_camera_0.ts", 3, 0)
    if not pending_pair:
        (unit.path.parent / terminal.name).write_bytes(b"old")
    pair = [RemoteFile(f"20261023000200_camera_{camera}.ts", 3, 0) for camera in (0, 1)]
    cutoff = datetime.now(UTC) + timedelta(seconds=120)
    filtered = parked_filter.FilterResult(
        [*unit.inventory.return_value, terminal],
        deferred=[(item, cutoff) for item in pair] if pending_pair else [],
        analysis_ready=True,
        analysis_terminal=True,
        analysis_terminal_files=[terminal],
        analysis_terminal_starts={terminal.name: datetime.now(UTC) - timedelta(seconds=140)},
    )
    refresh = AsyncMock(return_value=native_observation(age=10 if not pending_pair else 300))
    monkeypatch.setattr(puller.adb, "runtime_observation", refresh)
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=ready_logger()))
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_RECHECK_S", 0.001)
    monkeypatch.setattr(
        puller, "_filter_parked_inventory", AsyncMock(return_value=parked_filter.FilterResult([]))
    )
    result = await asyncio.wait_for(
        puller._quiet_analysis_handoff(
            unit.info,
            [unit.info.source],
            filtered.files,
            ready_logger(),
            filtered,
            footage=unit.path.parent,
            seen=set(),
        ),
        timeout=1,
    )
    assert result[-1] is True
    refresh.assert_awaited_once()
    assert state.snapshot()["analysis_wait_until_utc"] is None


async def test_never_ready_pair_hits_bounded_deadline_and_becomes_normal_upload(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    state = get_status()
    state.observe_unit_runtime(native_observation(age=500))
    pair = [RemoteFile(f"20261023000200_camera_{camera}.ts", 3, 0) for camera in (0, 1)]
    original_deadline = datetime.now(UTC) + timedelta(seconds=120)
    pending = parked_filter.FilterResult(
        [], deferred=[(item, original_deadline) for item in pair], analysis_ready=True
    )
    lifecycle = puller._RunLifecycle(datetime.now(UTC))
    token = puller._run_lifecycle.set(lifecycle)
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_MAX_WAIT_S", 0.05)
    refresh = AsyncMock(side_effect=AssertionError("deadline must end before another probe"))
    monkeypatch.setattr(puller.adb, "runtime_observation", refresh)
    monkeypatch.setattr(parked_filter, "filter_inventory", AsyncMock(return_value=pending))
    try:
        result = await asyncio.wait_for(
            puller._quiet_analysis_handoff(
                unit.info,
                [unit.info.source],
                pair,
                ready_logger(),
                pending,
                footage=unit.path.parent,
                seen=set(),
            ),
            timeout=1,
        )
        assert result[-1] is True
        assert {item.name for item in pair} == lifecycle.analysis_released
        assert all(until == original_deadline for _, until in pending.deferred)
        fallback = await puller._filter_parked_inventory(unit.info, pair, ready_logger())
        assert fallback.files == pair and not fallback.deferred and not fallback.skipped
        refresh.assert_not_awaited()
    finally:
        puller._run_lifecycle.reset(token)


@pytest.mark.parametrize("camera", ["both", "0"])
async def test_run_ends_completed_batch_and_restores_radios_for_new_pending_pair(
    prepared_unit, monkeypatch, camera
):
    from test_ingest_quiet_required import Transition

    unit = prepared_unit
    unit.options.update(
        skip_parked_footage=True, quiet_radios=True, sweep_passes=2, camera_filter=camera
    )
    original = unit.inventory.return_value[0]
    later = RemoteFile("20260812120100_camera_0.ts", original.size, 0)
    pair = [RemoteFile(f"20261023000200_camera_{camera}.ts", 3, 0) for camera in (0, 1)]
    # Initially unselected camera 1 later becomes deferred; it must not count twice.
    unit.inventory.return_value = [original, later, pair[1]]
    new_opposite = RemoteFile("20261023000300_camera_1.ts", 7, 0)
    until = datetime.now(UTC) + timedelta(seconds=120)
    events = []
    transition = Transition(events)
    monkeypatch.setattr(puller.radio_coordinator, "begin", AsyncMock(return_value=transition))
    monkeypatch.setattr(radios, "new_quieting_allowed", lambda: True)
    monkeypatch.setattr(radios, "QUIET_AFTER_ONLINE_S", 0)
    monkeypatch.setattr(
        puller, "read_logger_status", AsyncMock(return_value={"ownership_enabled": False})
    )
    monkeypatch.setattr(puller.movement, "inspect_before_plan", AsyncMock())
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_BATCH_CHECK_S", 0)
    get_status().observe_unit_runtime(native_observation())

    async def inventory_policy(*_args, **_kwargs):
        return parked_filter.FilterResult(
            [original, later, new_opposite] if "copy" in events else [original, later, pair[1]],
            deferred=[(item, until) for item in pair] if "copy" in events else [],
            analysis_ready="copy" in events,
        )

    monkeypatch.setattr(puller, "_filter_parked_inventory", inventory_policy)

    async def move(_info, _files, *, staging, on_chunk_completed, yield_after_chunk, **_kwargs):
        events.append("copy")
        staging.mkdir(exist_ok=True)
        (staging / original.name).write_bytes(unit.payload)
        await on_chunk_completed([original])
        assert await yield_after_chunk()
        return transport.TransferResult(
            files=[original.name], bytes_received=original.size, complete=True
        )

    moved = AsyncMock(side_effect=move)
    monkeypatch.setattr(puller, "_move", moved)
    result = await asyncio.wait_for(puller.run_pull(info=unit.info), timeout=2)
    assert result.state is RunState.OK and result.files == 1
    assert unit.path.read_bytes() == unit.payload
    assert not (unit.path.parent / later.name).exists()
    moved.assert_awaited_once()
    assert events[-1] == "restore"
    assert get_status().snapshot()["policy_deferred_files"] == (2 if camera == "both" else 1)
    assert get_status().snapshot()["backlog_files"] == 4
    assert get_status().snapshot()["backlog_bytes"] == (
        later.size + sum(item.size for item in pair) + new_opposite.size
    )


@pytest.mark.parametrize("current_reason", ["visual_incomplete", "ignition_buffer"])
async def test_older_or_drive_boundary_terminal_result_cannot_end_new_off_opportunity(
    prepared_unit, policy, monkeypatch, current_reason
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    get_status().observe_unit_runtime(native_observation(age=10, remaining=1190))
    doc = current_policy()
    for row in doc["decisions"]:
        row.update(decision="upload", reason=current_reason, defer_until_utc=None)
        row["visual"].update(status="unavailable", reason="system_thermal_guard")
    remote = policy_inventory(doc)
    monkeypatch.setattr(parked_filter, "_read_policy", AsyncMock(return_value=doc))
    monkeypatch.setattr(movement, "read_remote_card_id", AsyncMock(return_value=doc["card_id"]))
    filtered = await parked_filter.filter_inventory(
        "unit:5555",
        "/status",
        remote,
        SOURCE,
        ready_logger(),
        enabled=True,
        native_remaining_s=1190,
    )
    assert filtered.analysis_terminal is (current_reason == "visual_incomplete")
    refresh = AsyncMock(return_value=native_observation(age=10, remaining=1190))
    monkeypatch.setattr(puller.adb, "runtime_observation", refresh)
    monkeypatch.setattr(puller, "read_logger_status", AsyncMock(return_value=ready_logger()))
    monkeypatch.setattr(puller, "QUIET_ANALYSIS_RECHECK_S", 0.001)
    monkeypatch.setattr(
        puller, "_filter_parked_inventory", AsyncMock(return_value=parked_filter.FilterResult([]))
    )
    result = await asyncio.wait_for(
        puller._quiet_analysis_handoff(
            unit.info,
            [unit.info.source],
            remote,
            ready_logger(),
            filtered,
            footage=unit.path.parent,
            seen=set(),
        ),
        timeout=1,
    )
    assert result[-1] is True
    refresh.assert_awaited_once()
