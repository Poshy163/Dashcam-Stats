"""Only fresh, boot-bound on-unit evidence can recover an offline ignition edge."""

import json
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

from app.ingest import adb, obd_transfer
from app.ingest import status as status_module
from app.ingest.status import IngestStatus

BOOT = "01234567-1234-1234-1234-012345678901"
OTHER_BOOT = "01234567-1234-1234-1234-012345678902"
PATH = "/storage/emulated/0/Android/data/com.dashcamstats.obdlogger/files/status.json"


def evidence(**changes):
    value = {
        "schema_version": 1,
        "boot_id": BOOT,
        "boot_count": 12,
        "observed_elapsed_ms": 990_000,
        "ignition_on": False,
        "off_lower_elapsed_ms": 800_000,
        "off_upper_elapsed_ms": 806_000,
        "window_s": 1200,
    }
    return {**value, **changes}


def observation(value=None, **changes):
    return replace(
        adb.RuntimeObservation(
            BOOT,
            1000,
            "off",
            1200,
            12,
            adb.parse_sleep_deadline_evidence(evidence() if value is None else value),
        ),
        **changes,
    )


def reply(value=None, *, boot=BOOT, count="12", acc="0", final_acc=None, final_count=None):
    raw = json.dumps({"sleep_deadline_evidence": evidence() if value is None else value})
    return (
        f"{boot}\n{count}\n999.5 999\n{acc}\n1200\n__DASHCAM_STATUS_START__\n"
        f"{raw}\n__DASHCAM_STATUS_END__\n1000.0 999\n"
        f"{acc if final_acc is None else final_acc}\n{count if final_count is None else final_count}\n{boot}\n"
    )


@pytest.fixture
def clock(monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(status_module.time, "monotonic", lambda: now[0])
    return now


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"boot_id": "../boot"},
        {"boot_count": True},
        {"boot_count": -1},
        {"boot_id": None, "boot_count": None},
        {"observed_elapsed_ms": True},
        {"observed_elapsed_ms": float("nan")},
        {"observed_elapsed_ms": -1},
        {"ignition_on": 0},
        {"ignition_on": True},
        {"off_lower_elapsed_ms": 807_000},
        {"off_lower_elapsed_ms": -1},
        {"off_upper_elapsed_ms": 822_000},
        {"off_upper_elapsed_ms": 1_001_000},
        {"window_s": None},
        {"window_s": True},
        {"window_s": 0},
        {"window_s": 86401},
    ],
)
def test_malformed_evidence_is_rejected(changes):
    assert adb.parse_sleep_deadline_evidence(evidence(**changes)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"boot_id": OTHER_BOOT},
        {"boot_count": 13},
        {"observed_elapsed_ms": 1_000_001},
        {"observed_elapsed_ms": 979_999},
        {"off_lower_elapsed_ms": None, "off_upper_elapsed_ms": None, "window_s": None},
    ],
)
def test_stale_future_mismatched_or_unwitnessed_evidence_stays_unknown(clock, changes):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(**changes)))
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()


@pytest.mark.parametrize("changes", [{"ignition_state": "on"}, {"ignition_state": "unknown"}])
def test_live_acc_contradiction_prevents_deadline(clock, changes):
    status = IngestStatus()
    status.observe_unit_runtime(observation(**changes))
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()


@pytest.mark.parametrize("boot_id", [BOOT, None])
def test_valid_boot_uuid_or_count_fallback_recovers_arrival_deadline(clock, boot_id):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(boot_id=boot_id), boot_id=boot_id))
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_remaining_s"] == 997  # earliest OFF + 1200 - 1000 - probe3
    assert snapshot["sleep_countdown_source"] == "estimated"
    assert snapshot["sleep_countdown_evidence_source"] == "unit"
    assert "head-unit ignition-off evidence" in snapshot["sleep_countdown_reason"]
    assert snapshot["ignition_off_at"] is not None
    assert status.radio_quieting_allowed()
    assert status.sleep_countdown_elapsed_s() == 203


def test_deadline_expires_independently_of_live_power_snapshot(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation())
    clock[0] += 10.01  # sample was already10s old when read; evidence TTL20
    assert status.snapshot()["unit_observation_fresh"]
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()
    # Expired evidence must not lengthen the operational deadline for restoration.
    assert status.sleep_countdown_remaining_s() == pytest.approx(986.99)


def test_failed_read_clears_admission_but_new_live_evidence_reacquires_same_edge(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation())
    status.unit_observation_failed()
    assert not status.radio_quieting_allowed()
    clock[0] += 15
    status.observe_unit_runtime(observation(evidence(observed_elapsed_ms=1_014_000), uptime_s=1015))
    assert status.radio_quieting_allowed()
    assert status.snapshot()["sleep_countdown_remaining_s"] == 982


def test_reboot_clears_old_evidence_and_keeps_active_pull_ownership(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation())
    status.try_begin()
    status.observe_unit_runtime(observation(boot_id=OTHER_BOOT, boot_count=13, uptime_s=25))
    assert status.running and status.cancel_event.is_set()
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()


@pytest.mark.parametrize("remaining,allowed", [(0, False), (59, False), (60, False), (61, True)])
def test_new_quieting_requires_strictly_more_than_sixty_usable_seconds(clock, remaining, allowed):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(window_s=203 + remaining)))
    assert status.radio_quieting_allowed() is allowed


def test_later_property_and_cached_logger_cannot_extend_valid_deadline(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(window_s=300)))
    assert status.sleep_countdown_remaining_s() == 97
    status.set_sleep_window(1200, restarted=True)
    status.set_ignition_state("off")
    assert status.sleep_countdown_remaining_s() == 97
    assert status.sleep_countdown_elapsed_s() == 1103


@pytest.mark.parametrize("current_window", [300, 60, None])
def test_current_policy_property_does_not_replace_latched_off_window(clock, current_window):
    status = IngestStatus()
    status.observe_unit_runtime(observation(sleep_window_s=current_window))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 997
    assert status.radio_quieting_allowed()
    clock[0] += 5
    status.observe_unit_runtime(
        observation(
            evidence(observed_elapsed_ms=1_004_000),
            uptime_s=1005,
            sleep_window_s=1200,
        )
    )
    assert status.snapshot()["sleep_countdown_remaining_s"] == 992
    assert status.sleep_deadline_uptime_s() == 1997


def test_missing_unit_evidence_cannot_reactivate_retired_server_timer(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(ignition_state="on", sleep_deadline_evidence=None))
    clock[0] += 15
    status.observe_unit_runtime(
        observation(
            evidence(
                window_s=100,
                off_lower_elapsed_ms=1000_000,
                off_upper_elapsed_ms=1006_000,
                observed_elapsed_ms=1010_000,
            ),
            uptime_s=1015,
        )
    )
    assert status.snapshot()["sleep_countdown_remaining_s"] == 82
    clock[0] += 10
    status.observe_unit_runtime(observation(uptime_s=1025, sleep_deadline_evidence=None))
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()
    assert status.sleep_countdown_remaining_s() == 72  # Restoration keeps its frozen ceiling.
    clock[0] += 12
    assert not status.radio_quieting_allowed()


def test_unit_evidence_lease_is_published_separately_from_power_freshness(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(observed_elapsed_ms=981_000)))
    snapshot = status.snapshot()
    assert snapshot["unit_observation_ttl_s"] == 30
    assert snapshot["sleep_countdown_valid_for_s"] == 1
    clock[0] += 1
    assert status.snapshot()["sleep_countdown_valid_for_s"] is None


def test_ttl_crossing_between_countdown_checks_uses_one_snapshot_time(monkeypatch, clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation())
    original = status._unit_countdown_remaining
    calls = 0

    def crossing(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            clock[0] += 20
        return original(**kwargs)

    monkeypatch.setattr(status, "_unit_countdown_remaining", crossing)
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_remaining_s"] == 997
    assert snapshot["sleep_countdown_valid_for_s"] == 10
    assert status.snapshot()["sleep_countdown_remaining_s"] is None


def test_next_trip_on_same_boot_replaces_expired_previous_trip_ceiling(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(window_s=300)))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 97
    status.set_unit_online(False)
    clock[0] += 3600
    status.observe_unit_runtime(
        observation(
            evidence(
                observed_elapsed_ms=4599_000,
                off_lower_elapsed_ms=4500_000,
                off_upper_elapsed_ms=4506_000,
            ),
            uptime_s=4600,
        )
    )
    assert status.snapshot()["sleep_countdown_remaining_s"] == 1097
    assert status.radio_quieting_allowed()
    assert status.sleep_deadline_uptime_s() == 5697


def test_same_edge_cannot_extend_after_transient_disconnect(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(window_s=300)))
    status.set_unit_online(False)
    clock[0] += 5
    status.observe_unit_runtime(observation(evidence(observed_elapsed_ms=1004_000), uptime_s=1005))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 92


def test_unknown_acc_cannot_erase_accepted_ceiling_and_extend_same_edge(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(window_s=300)))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 97
    clock[0] += 5
    status.observe_unit_runtime(observation(ignition_state="unknown", uptime_s=1005))
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()
    clock[0] += 5
    status.observe_unit_runtime(observation(evidence(observed_elapsed_ms=1009_000), uptime_s=1010))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 87
    assert status.sleep_deadline_uptime_s() == 1097


@pytest.mark.parametrize("interrupt", [None, "unknown", "offline"])
def test_recent_previous_trip_document_cannot_override_new_observed_on(clock, interrupt):
    status = IngestStatus()
    status.observe_unit_runtime(observation())
    clock[0] += 5
    status.observe_unit_runtime(observation(ignition_state="on", uptime_s=1005, sleep_window_s=300))
    if interrupt == "unknown":
        status.observe_unit_runtime(observation(ignition_state="unknown", uptime_s=1006))
    elif interrupt == "offline":
        status.set_unit_online(False)
    clock[0] += 5
    status.observe_unit_runtime(
        observation(evidence(observed_elapsed_ms=1004_000), uptime_s=1010, sleep_window_s=300)
    )
    snapshot = status.snapshot()
    if interrupt is None:
        assert snapshot["sleep_countdown_remaining_s"] == 267
        assert snapshot["sleep_countdown_evidence_source"] == "server"
    else:
        assert snapshot["sleep_countdown_remaining_s"] is None
        assert not status.radio_quieting_allowed()


def test_new_local_off_bracket_can_straddle_latest_server_on(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(ignition_state="on", uptime_s=1005, sleep_window_s=300))
    clock[0] += 10
    status.observe_unit_runtime(
        observation(
            evidence(
                off_lower_elapsed_ms=1000_000,
                off_upper_elapsed_ms=1010_000,
                observed_elapsed_ms=1014_000,
            ),
            uptime_s=1015,
            sleep_window_s=300,
        )
    )
    assert status.snapshot()["sleep_countdown_remaining_s"] == 1182
    assert status.snapshot()["sleep_countdown_evidence_source"] == "unit"


def test_overlapping_changed_bounds_cannot_manufacture_new_timer(clock):
    status = IngestStatus()
    status.observe_unit_runtime(observation(evidence(window_s=300)))
    clock[0] += 5
    status.observe_unit_runtime(
        observation(
            evidence(
                observed_elapsed_ms=1004_000,
                off_lower_elapsed_ms=800_001,
            ),
            uptime_s=1005,
        )
    )
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()


def test_selected_source_and_lease_are_structural_not_derived_from_reason(clock):
    status = IngestStatus()
    status.observe_unit_runtime(
        observation(ignition_state="on", sleep_deadline_evidence=None, sleep_window_s=300)
    )
    clock[0] += 5
    status.observe_unit_runtime(
        observation(
            evidence(
                off_lower_elapsed_ms=995_000,
                off_upper_elapsed_ms=1000_000,
                observed_elapsed_ms=1004_000,
            ),
            uptime_s=1005,
            sleep_window_s=300,
        )
    )
    # Unit bound is shorter here; prove structural source survives wording changes.
    countdown = status._countdown()
    from dataclasses import replace

    status._countdown = lambda: replace(countdown, reason="Any translated presentation text")
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_evidence_source"] == countdown.evidence_source
    assert snapshot["ignition_off_at"] == countdown.off_at.isoformat()
    assert snapshot["sleep_countdown_valid_for_s"] == countdown.valid_for_s


def test_unit_edge_replaces_weaker_server_timestamp_and_property_window(clock):
    status = IngestStatus()
    status.observe_unit_runtime(
        observation(ignition_state="on", sleep_deadline_evidence=None, sleep_window_s=300)
    )
    clock[0] += 15
    status.observe_unit_runtime(
        observation(uptime_s=1015, sleep_deadline_evidence=None, sleep_window_s=300)
    )
    server_edge = status.snapshot()["ignition_off_at"]
    clock[0] += 5
    status.observe_unit_runtime(
        observation(
            evidence(
                off_lower_elapsed_ms=1019_000,
                off_upper_elapsed_ms=1020_000,
                observed_elapsed_ms=1020_000,
                window_s=300,
            ),
            uptime_s=1020,
            sleep_window_s=300,
        )
    )
    snapshot = status.snapshot()
    assert snapshot["sleep_countdown_evidence_source"] == "unit"
    assert snapshot["ignition_off_at"] != server_edge
    assert snapshot["sleep_countdown_valid_for_s"] == 20


def test_first_unit_proof_supersedes_conservative_server_property_bound(clock):
    status = IngestStatus()
    status.observe_unit_runtime(
        observation(sleep_deadline_evidence=None, ignition_state="on", sleep_window_s=300)
    )
    clock[0] += 15
    status.observe_unit_runtime(
        observation(
            evidence(
                off_lower_elapsed_ms=1000_000,
                off_upper_elapsed_ms=1005_000,
                observed_elapsed_ms=1015_000,
            ),
            uptime_s=1015,
        )
    )
    assert status.radio_quieting_allowed()
    # The local recorder witnessed the actual OFF window after the last server ON
    # observation. Its immutable edge supersedes the earlier sampled property.
    assert status.sleep_countdown_remaining_s() == 1182


def test_join_wifi_wait_then_off_uses_off_edge_not_arrival_or_later_idle_policy(clock):
    status = IngestStatus()
    for elapsed in range(0, 301, 15):
        clock[0] = 5000 + elapsed
        status.observe_unit_runtime(
            observation(ignition_state="on", uptime_s=1000 + elapsed, sleep_deadline_evidence=None)
        )
        assert status.snapshot()["sleep_countdown_remaining_s"] is None
    clock[0] += 10
    local_edge = evidence(
        off_lower_elapsed_ms=1304_000,
        off_upper_elapsed_ms=1309_000,
        observed_elapsed_ms=1309_000,
    )
    status.observe_unit_runtime(observation(local_edge, uptime_s=1310, sleep_window_s=300))
    assert status.snapshot()["sleep_countdown_remaining_s"] == 1191
    assert status.sleep_deadline_uptime_s() == 2501
    # Still awake nine minutes later: idle policy300 is for the next countdown.
    clock[0] += 540
    status.observe_unit_runtime(
        observation(
            {**local_edge, "observed_elapsed_ms": 1849_000}, uptime_s=1850, sleep_window_s=300
        )
    )
    assert status.snapshot()["sleep_countdown_remaining_s"] == 651
    assert status.radio_quieting_allowed()
    assert status.sleep_deadline_uptime_s() == 2501


async def test_fresh_small_file_is_read_in_same_bounded_identity_bracket(monkeypatch):
    shell = AsyncMock(return_value=reply())
    monkeypatch.setattr(adb, "shell", shell)
    result = await adb.runtime_observation("unit", logger_status_path=PATH)
    assert result.validated_sleep_deadline() == adb.parse_sleep_deadline_evidence(evidence())
    command = shell.await_args.args[1]
    assert command.count("settings get global boot_count") == 2
    assert command.count("settings get global acc_status") == 2
    assert "head -c 65537" in command
    assert shell.await_args.kwargs == {"timeout": 3.0}
    assert not any(word in command for word in ("setprop", "connect ", "disconnect", "su "))


async def test_unreadable_kernel_uuid_can_use_bracketed_boot_count(monkeypatch):
    monkeypatch.setattr(
        adb, "shell", AsyncMock(return_value=reply(evidence(boot_id=None), boot=""))
    )
    result = await adb.runtime_observation("unit", logger_status_path=PATH)
    assert result.boot_id is None and result.boot_count == 12
    assert result.validated_sleep_deadline() is not None


@pytest.mark.parametrize(
    "kwargs", [{"final_count": "13"}, {"final_acc": "1"}, {"boot": "", "count": "null"}]
)
async def test_mixed_or_missing_runtime_identity_rejects_entire_observation(monkeypatch, kwargs):
    monkeypatch.setattr(adb, "shell", AsyncMock(return_value=reply(**kwargs)))
    assert await adb.runtime_observation("unit", logger_status_path=PATH) is None


async def test_unsafe_path_never_reaches_shell(monkeypatch):
    shell = AsyncMock()
    monkeypatch.setattr(adb, "shell", shell)
    assert await adb.runtime_observation("unit", logger_status_path="/data/x'; reboot") is None
    shell.assert_not_called()


async def test_sanitized_cached_logger_evidence_never_creates_live_deadline(monkeypatch, clock):
    status = IngestStatus()
    monkeypatch.setattr(status_module, "get_status", lambda: status)
    monkeypatch.setattr(
        adb,
        "shell",
        AsyncMock(
            return_value=json.dumps(
                {"sleep_deadline_evidence": evidence(), "acc_on": False, "acc_state_known": True}
            )
        ),
    )
    clean = await obd_transfer.read_logger_status("unit", PATH)
    assert clean["sleep_deadline_evidence"] == asdict(adb.parse_sleep_deadline_evidence(evidence()))
    obd_transfer.OBDTransferStatus().set_logger(clean)
    assert status.snapshot()["sleep_countdown_remaining_s"] is None
    assert not status.radio_quieting_allowed()
