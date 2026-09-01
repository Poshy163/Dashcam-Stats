from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import OBDLoggerEvent
from app.db.session import session_scope
from app.ingest import obd_events, obd_transfer, puller

SOURCE_ID = "3f6ec970-5c92-4e86-a603-0f0b9cdf96e8"
SESSION_ID = "0bf5d722-4c9a-4d73-9c12-5daf8c67cdd2"


@pytest.fixture(autouse=True)
def isolated_event_status(monkeypatch) -> obd_events.LoggerEventStatus:
    """Keep process-local health assertions independent of test execution order."""
    status = obd_events.LoggerEventStatus()
    monkeypatch.setattr(obd_events, "_status", status)
    return status


def event_document(
    *,
    sequence: int = 1,
    kind: str = "obd.ble_connection",
    level: str = "info",
    outcome: str = "started",
    reason_code: str | None = "scheduled_connect",
    drive_id: str | None = None,
    metrics: dict[str, int | float] | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": 1,
        "source_id": SOURCE_ID,
        "generated_at_utc": now,
        "first_sequence": sequence,
        "last_sequence": sequence,
        "producer": {
            "app_version_name": "0.3.0",
            "app_version_code": 8,
            "build_git_sha": "0123456789ab",
        },
        "events": [
            {
                "sequence": sequence,
                "occurred_at_utc": now,
                "session_id": SESSION_ID,
                "kind": kind,
                "level": level,
                "outcome": outcome,
                "reason_code": reason_code,
                "drive_id": drive_id,
                "metrics": metrics or {},
            }
        ],
    }


def test_event_snapshot_accepts_only_bounded_codes_and_numeric_metrics() -> None:
    body = event_document(
        metrics={
            "connect_ms": 812,
            "consecutive_failures": 2,
            "queue_depth": 1,
        }
    )

    snapshot = obd_events.validate_event_snapshot(json.dumps(body))

    assert snapshot.source_id_hash != SOURCE_ID
    assert len(snapshot.source_id_hash) == 64
    assert snapshot.events[0].session_id_hash != SESSION_ID
    assert snapshot.events[0].metrics == {
        "connect_ms": 812,
        "consecutive_failures": 2,
        "queue_depth": 1,
    }


@pytest.mark.parametrize(
    "reason_code", ["screen_on", "user_present", "power_connected", "acc_on", "acc_off"]
)
def test_event_snapshot_accepts_bounded_wake_reason_codes(reason_code: str) -> None:
    snapshot = obd_events.validate_event_snapshot(
        json.dumps(event_document(reason_code=reason_code))
    )

    assert snapshot.events[0].reason_code == reason_code


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda body: body.update({"device_address": "private"}), "fields"),
        (
            lambda body: body["events"][0].update({"message": "free-form secret"}),
            "fields",
        ),
        (
            lambda body: body["events"][0]["metrics"].update({"adapter_address": 1}),
            "unsupported field",
        ),
        (
            lambda body: body["events"][0].update({"reason_code": "raw_exception_text"}),
            "reason code",
        ),
        (
            lambda body: body["events"][0].update({"connect_ms": float("inf")}),
            "fields",
        ),
    ],
)
def test_event_snapshot_rejects_unbounded_or_non_contract_fields(mutate, message: str) -> None:
    body = event_document()
    mutate(body)

    with pytest.raises(obd_events.EventStreamError, match=message):
        obd_events.validate_event_snapshot(json.dumps(body))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda body: body.update({"schema_version": True}), "schema version"),
        (lambda body: body.update({"schema_version": 1.0}), "schema version"),
        (lambda body: body["events"][0].update({"kind": []}), "kind"),
        (lambda body: body["events"][0].update({"level": {}}), "level"),
        (lambda body: body["events"][0].update({"outcome": []}), "outcome"),
        (lambda body: body["events"][0].update({"reason_code": {}}), "reason code"),
    ],
)
def test_event_snapshot_rejects_wrong_scalar_types(mutate, message: str) -> None:
    body = event_document()
    mutate(body)

    with pytest.raises(obd_events.EventStreamError, match=message):
        obd_events.validate_event_snapshot(json.dumps(body))


def test_event_snapshot_rejects_oversized_integer_metric_as_invalid_input() -> None:
    body = event_document(metrics={"connect_ms": 10**1000})

    with pytest.raises(obd_events.EventStreamError, match="outside its supported range"):
        obd_events.validate_event_snapshot(json.dumps(body))


def test_event_snapshot_requires_exact_sorted_sequence_bounds() -> None:
    body = event_document(sequence=3)
    second = dict(body["events"][0])
    second["sequence"] = 2
    body["events"].append(second)
    body["last_sequence"] = 2

    with pytest.raises(obd_events.EventStreamError, match="strictly increasing"):
        obd_events.validate_event_snapshot(json.dumps(body))


async def test_optional_remote_event_file_is_regular_and_size_bounded(monkeypatch) -> None:
    observed: dict[str, object] = {}

    async def shell(address: str, command: str, *, timeout: float) -> str:
        observed.update(address=address, command=command, timeout=timeout)
        return obd_events._EVENT_FILE_MISSING

    monkeypatch.setattr(obd_events.adb, "shell", shell)

    assert await obd_events.read_remote_event_snapshot("unit", "/safe/events.json") is None
    assert "[ ! -L '/safe/events.json' ]" in str(observed["command"])
    assert str(obd_events.MAX_REMOTE_EVENT_BYTES + 1) in str(observed["command"])
    assert observed["timeout"] == 5.0


async def test_store_deduplicates_by_hashed_source_and_sequence_and_api_filters(
    db_session, client
) -> None:
    snapshot = obd_events.validate_event_snapshot(
        json.dumps(
            event_document(
                sequence=10,
                kind="drive.lifecycle",
                outcome="started",
                reason_code="engine_detected",
                drive_id="drive-event-10",
                metrics={"first_sample_ms": 940},
            )
        )
    )

    assert await obd_events.store_event_snapshot(snapshot) == (1, 0, 9)
    assert await obd_events.store_event_snapshot(snapshot) == (0, 1, 0)

    async with session_scope() as session:
        rows = (await session.execute(select(OBDLoggerEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].source_id_hash != SOURCE_ID
    assert rows[0].session_id_hash != SESSION_ID

    response = await client.get(
        "/api/obd/events",
        params={"drive_id": "drive-event-10", "kind": "drive.lifecycle"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["reason_code"] == "engine_detected"
    assert payload["items"][0]["metrics"] == {"first_sample_ms": 940}
    encoded = response.text
    assert SOURCE_ID not in encoded
    assert SESSION_ID not in encoded
    assert "source_id_hash" not in encoded
    assert "session_id_hash" not in encoded


async def test_store_counts_sequence_holes_inside_a_snapshot(db_session) -> None:
    body = event_document(sequence=3)
    second = dict(body["events"][0])
    second["sequence"] = 5
    body["events"].append(second)
    body["last_sequence"] = 5
    snapshot = obd_events.validate_event_snapshot(json.dumps(body))

    assert await obd_events.store_event_snapshot(snapshot) == (2, 0, 3)


async def test_event_api_rejects_unknown_filters(client) -> None:
    bad_kind = await client.get("/api/obd/events", params={"kind": "arbitrary.text"})
    bad_level = await client.get("/api/obd/events", params={"level": "debug"})
    bad_drive = await client.get("/api/obd/events", params={"drive_id": "../private"})
    extreme_time = await client.get(
        "/api/obd/events", params={"since": "0001-01-01T00:00:00+14:00"}
    )

    assert bad_kind.status_code == 422
    assert bad_level.status_code == 422
    assert bad_drive.status_code == 422
    assert extreme_time.status_code == 422


async def test_server_event_retention_is_row_bounded(db_session, monkeypatch) -> None:
    monkeypatch.setattr(obd_events, "MAX_SERVER_EVENTS", 2)

    for sequence in (1, 2, 3):
        snapshot = obd_events.validate_event_snapshot(json.dumps(event_document(sequence=sequence)))
        accepted, _, _ = await obd_events.store_event_snapshot(snapshot)
        assert accepted == 1

    async with session_scope() as session:
        rows = (
            (await session.execute(select(OBDLoggerEvent).order_by(OBDLoggerEvent.sequence.asc())))
            .scalars()
            .all()
        )
    assert [row.sequence for row in rows] == [2, 3]


async def test_logger_status_accepts_acc_evidence_and_event_capability(monkeypatch) -> None:
    body = {
        "schema_version": 6,
        "state": "parked",
        "capabilities": [
            "adaptive_sleep_window_v1",
            "adaptive_sleep_window_v2",
            "app_event_stream_v1",
        ],
        "acc_state_known": True,
        "acc_on": False,
    }

    async def shell(*_args, **_kwargs) -> str:
        return obd_transfer._STATUS_FILE_PREFIX + json.dumps(body)

    monkeypatch.setattr(obd_transfer.adb, "shell", shell)

    status = await obd_transfer.read_logger_status("unit", "/safe/status.json")

    assert status is not None
    assert status["acc_state_known"] is True
    assert status["acc_on"] is False
    assert status["capabilities"] == body["capabilities"]


async def test_invalid_event_stream_fails_soft_and_updates_health(monkeypatch, db_session) -> None:
    async def read(*_args, **_kwargs) -> str:
        return '{"schema_version":1,"private":"not accepted"}'

    monkeypatch.setattr(obd_events, "read_remote_event_snapshot", read)

    result = await obd_events.sync_remote_events("unit", "/safe/events.json")

    assert result.available is True
    assert result.accepted == 0
    assert result.error == "invalid_snapshot"
    assert obd_events.get_logger_event_status().snapshot()["last_error"] == result.error
    assert "private" not in str(obd_events.get_logger_event_status().snapshot())
    assert "not accepted" not in str(obd_events.get_logger_event_status().snapshot())


@pytest.mark.parametrize("stall_stage", ["read", "store"])
async def test_event_mirror_has_one_fail_soft_deadline_for_every_stage(
    monkeypatch, stall_stage: str
) -> None:
    cancelled = asyncio.Event()

    async def stall(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def read(*_args, **_kwargs) -> str:
        return json.dumps(event_document())

    monkeypatch.setattr(obd_events, "read_remote_event_snapshot", read)
    if stall_stage == "read":
        monkeypatch.setattr(obd_events, "read_remote_event_snapshot", stall)
    else:
        monkeypatch.setattr(obd_events, "store_event_snapshot", stall)

    started = time.monotonic()
    result = await obd_events.sync_remote_events("unit", "/safe/events.json", timeout_seconds=0.02)

    assert time.monotonic() - started < 0.5
    assert cancelled.is_set()
    assert result.error == "timeout"
    assert result.accepted == 0
    assert obd_events.get_logger_event_status().snapshot()["last_error"] == "timeout"


def test_sequence_gap_health_accumulates_until_explicit_reset(
    isolated_event_status: obd_events.LoggerEventStatus,
) -> None:
    isolated_event_status.finish(
        obd_events.EventSyncResult(available=True, accepted=1, sequence_gap=4)
    )
    isolated_event_status.finish(
        obd_events.EventSyncResult(available=True, duplicates=1, sequence_gap=0)
    )
    isolated_event_status.finish(
        obd_events.EventSyncResult(available=True, accepted=1, sequence_gap=3)
    )

    assert isolated_event_status.snapshot()["sequence_gap"] == 7

    isolated_event_status.reset()

    assert isolated_event_status.snapshot() == {
        "available": False,
        "checked_at": None,
        "last_received_at": None,
        "accepted": 0,
        "duplicates": 0,
        "sequence_gap": 0,
        "last_error": None,
    }


async def test_puller_event_await_uses_the_original_preflight_deadline() -> None:
    cancelled = asyncio.Event()

    async def never_finishes() -> obd_events.EventSyncResult:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return obd_events.EventSyncResult()

    task = asyncio.create_task(never_finishes())
    started = time.monotonic()

    await puller._await_event_mirror(
        task,
        deadline=asyncio.get_running_loop().time() + 0.02,
    )

    assert time.monotonic() - started < 0.5
    assert cancelled.is_set()
    assert task.cancelled()
