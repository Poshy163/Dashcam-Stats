"""A deferred radio shutdown remains visible and does not spend a backup pass."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.ingest import poller, puller
from app.ingest.models import RunResult, RunState, UnitInfo, UnitState
from app.ingest.status import IngestStatus


@pytest.mark.parametrize("online", [False, True])
@pytest.mark.parametrize("state", list(RunState))
def test_radio_hold_visibility_matches_current_run_state(online, state):
    status = IngestStatus()
    status.set_unit_online(online)
    assert status.try_begin()
    reason = "Waiting for verified radio shutdown: hotspot still on"
    status.set_radio_quieting_hold(reason)
    if state is not RunState.RUNNING:
        status.finish(RunResult(state=state))
    visible = online and state in {RunState.RUNNING, RunState.IDLE}
    snapshot = status.snapshot()
    assert snapshot["radio_quieting_hold"] is visible
    assert snapshot["radio_quieting_hold_reason"] == (reason if visible else None)


def test_starting_next_attempt_clears_previous_hold():
    status = IngestStatus()
    status.set_unit_online(True)
    assert status.try_begin()
    status.set_radio_quieting_hold("Waiting for verified radio shutdown")
    status.finish(RunResult(state=RunState.IDLE))
    assert status.snapshot()["radio_quieting_hold"]
    assert status.try_begin()
    assert not status.snapshot()["radio_quieting_hold"]
    assert status.snapshot()["radio_quieting_hold_reason"] is None


@pytest.fixture
async def auto_run(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(poller, "time", SimpleNamespace(monotonic=lambda: now[0]))
    outcome = asyncio.get_running_loop().create_future()

    async def run():
        return await outcome

    tasks = []

    def start(**_kwargs):
        task = asyncio.create_task(run())
        tasks.append(task)
        return task

    start_mock = Mock(side_effect=start)
    monkeypatch.setattr(puller, "start_run", start_mock)
    return SimpleNamespace(
        poller=poller.IngestPoller(),
        now=now,
        outcome=outcome,
        tasks=tasks,
        start=start_mock,
        info=UnitInfo("unit:5555", UnitState.DEVICE, "/card"),
    )


async def settle():
    # One turn finishes the task; the next invokes its done callback.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.parametrize("continuation", [False, True])
async def test_held_automatic_attempt_refunds_once_and_waits_from_completion(
    auto_run, continuation
):
    current = auto_run.poller
    current._backups_this_visit = 1
    current._idle_since = 10.0
    current._start_auto_pull(auto_run.info, continuation=continuation)
    assert current._backups_this_visit == 2
    auto_run.start.assert_called_once_with(
        trigger="auto", info=auto_run.info, continuation=continuation
    )
    auto_run.now[0] = 200.0  # Preparation itself was slow.
    auto_run.outcome.set_result(RunResult(state=RunState.IDLE))
    await settle()
    assert current._backups_this_visit == 1
    assert current._idle_since == 200.0
    idle_status = SimpleNamespace(state=RunState.IDLE)
    assert not current._should_drain_again(idle_status)
    auto_run.now[0] += poller.IDLE_RECHECK_S - 0.01
    assert not current._should_drain_again(idle_status)
    auto_run.now[0] += 0.01
    assert current._should_drain_again(idle_status)
    await settle()
    assert current._backups_this_visit == 1


@pytest.mark.parametrize(
    "result",
    [
        RunResult(state=RunState.OK, files=1, bytes=100),
        RunResult(state=RunState.PARTIAL, files=1),
        RunResult(state=RunState.ERROR, error="transport failed"),
        RunResult(state=RunState.CANCELLED),
        RunResult(state=RunState.IDLE, files=1),
        RunResult(state=RunState.IDLE, bytes=100),
    ],
)
async def test_material_or_failed_attempt_is_not_refunded(auto_run, result):
    auto_run.poller._start_auto_pull(auto_run.info)
    auto_run.outcome.set_result(result)
    await settle()
    assert auto_run.poller._backups_this_visit == 1
    assert auto_run.poller._idle_since == 0


async def test_old_visit_completion_cannot_refund_new_visit(auto_run):
    auto_run.poller._start_auto_pull(auto_run.info)
    auto_run.poller._backup_visit_token = object()
    auto_run.poller._backups_this_visit = 2
    auto_run.poller._idle_since = 80.0
    auto_run.outcome.set_result(RunResult(state=RunState.IDLE))
    await settle()
    assert auto_run.poller._backups_this_visit == 2
    assert auto_run.poller._idle_since == 80.0


@pytest.mark.parametrize("failure", ["cancel", "exception"])
async def test_unfinished_automatic_attempt_never_refunds(auto_run, failure):
    auto_run.poller._start_auto_pull(auto_run.info)
    if failure == "cancel":
        auto_run.tasks[0].cancel()
    else:
        auto_run.outcome.set_exception(RuntimeError("unexpected crash"))
    await settle()
    assert auto_run.poller._backups_this_visit == 1
    assert auto_run.poller._idle_since == 0


async def test_manual_hold_does_not_change_automatic_budget(auto_run):
    auto_run.poller._backups_this_visit = 2
    task = puller.start_run(trigger="manual", info=auto_run.info)
    auto_run.outcome.set_result(RunResult(state=RunState.IDLE))
    await task
    await settle()
    assert auto_run.poller._backups_this_visit == 2
    assert auto_run.poller._idle_since == 0
