"""Advisory setting/startup evidence cannot leak arbitrary data or affect admission."""

import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.ingest import obd_transfer
from app.ingest.logger_diagnostics import clean_restart_evidence, clean_trial_settings


def trial_settings():
    change = {
        "revision": 4,
        "enabled": False,
        "fallback_reason": "sustained_overrun",
        "source": "health",
        "changed_at_utc": "2026-10-25T04:00:00Z",
    }
    return {
        "schema_version": 1,
        **change,
        "storage_state": "ready",
        "error": None,
        "legacy_key_present": True,
        "recent_changes": [dict(change)],
    }


def restart_evidence():
    entry = {
        "transition": "kernel_boot",
        "observed_at_utc": "2026-10-25T04:00:00Z",
        "boot_count": 27,
        "boot_count_delta": 1,
        "boot_reason": "reboot",
        "boot_reason_source": "sys_boot_reason",
        "cause_confidence": "unknown",
        "previous_process_exit": "not_recorded",
        "previous_process_exit_at_utc": None,
        "app_version_code": 32,
        "build_git_sha": "13ce90916009",
    }
    return {"schema_version": 1, "storage_state": "ready", **entry, "history": [dict(entry)]}


def test_complete_bounded_trial_audit_and_generic_reboot_remain_honest():
    assert clean_trial_settings(trial_settings()) == trial_settings()
    assert clean_restart_evidence(restart_evidence()) == restart_evidence()
    imported = trial_settings()
    imported.update(
        enabled=True,
        fallback_reason=None,
        source="legacy_migration",
        changed_at_utc=None,
        recent_changes=[],
    )
    assert clean_trial_settings(imported) == imported


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("revision", True),
        ("revision", -1),
        ("revision", 2**31),
        ("enabled", 1),
        ("source", "unknown private payload"),
        ("fallback_reason", "a secret or raw exception"),
        ("changed_at_utc", "2026-10-25T04:00:00"),
        ("changed_at_utc", "2026-02-30T04:00:00Z"),
        ("storage_state", "healthy"),
        ("legacy_key_present", 1),
        ("error", "private/path"),
        ("recent_changes", {}),
    ],
)
def test_invalid_trial_fields_are_dropped_as_a_whole(key, value):
    document = trial_settings()
    document[key] = value
    assert clean_trial_settings(document) is None


def test_trial_audit_rejects_unbounded_duplicate_future_and_private_history():
    for mutate in (
        lambda d: d["recent_changes"].extend(copy.deepcopy(d["recent_changes"]) * 8),
        lambda d: d["recent_changes"].append(copy.deepcopy(d["recent_changes"][0])),
        lambda d: d["recent_changes"][0].update(revision=5),
        lambda d: d["recent_changes"][0].update(webhook_api_key="never echo me"),
        lambda d: d.update(enabled=True),
        lambda d: d.update(fallback_reason=None),
    ):
        document = trial_settings()
        mutate(document)
        assert clean_trial_settings(document) is None


def test_unavailable_trial_state_never_reports_armed():
    document = trial_settings()
    document.update(storage_state="unavailable", error="missing_record", source="unknown")
    assert clean_trial_settings(document) == document
    document.update(enabled=True, fallback_reason=None)
    assert clean_trial_settings(document) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("boot_count", True),
        ("boot_count", -1),
        ("boot_count_delta", 0),
        ("boot_count_delta", 1001),
        ("boot_count_delta", True),
        ("boot_reason", "reboot,password=private"),
        ("boot_reason_source", "raw property"),
        ("cause_confidence", "verified"),
        ("cause_confidence", "reported"),
        ("previous_process_exit", "uncaught secret stack trace"),
        ("previous_process_exit_at_utc", "2026-10-25T04:00:00"),
        ("app_version_code", 0),
        ("build_git_sha", "private/path"),
        ("transition", "thermal_caused_reboot"),
    ],
)
def test_invalid_or_overconfident_reboot_evidence_is_dropped(key, value):
    document = restart_evidence()
    document[key] = value
    assert clean_restart_evidence(document) is None


def test_app_restart_does_not_invent_kernel_boot_or_reuse_a_boot_delta():
    document = restart_evidence()
    document.update(
        transition="process_restart", boot_count_delta=None, previous_process_exit="crash"
    )
    assert clean_restart_evidence(document) == document
    document["boot_count_delta"] = 1
    assert clean_restart_evidence(document) is None


def test_restart_history_is_bounded_and_never_copies_private_identity():
    document = restart_evidence()
    document["history"] *= 9
    assert clean_restart_evidence(document) is None
    document = restart_evidence()
    document["history"][0]["boot_id"] = "private identity"
    assert clean_restart_evidence(document) is None


def test_missing_observation_and_zero_revision_do_not_masquerade_as_recorded_history():
    document = restart_evidence()
    document["observed_at_utc"] = None
    assert clean_restart_evidence(document) is None
    document.update(
        storage_state="pending", transition="unknown", boot_count_delta=None, history=[]
    )
    assert clean_restart_evidence(document) == document
    document = restart_evidence()
    document["history"][0]["observed_at_utc"] = None
    assert clean_restart_evidence(document) is None
    trial = trial_settings()
    trial["recent_changes"][0]["revision"] = 0
    assert clean_trial_settings(trial) is None
    trial = trial_settings()
    trial.update(revision=0, recent_changes=[])
    assert clean_trial_settings(trial) is None
    trial = trial_settings()
    trial["source"] = "operator"
    assert clean_trial_settings(trial) is None


@pytest.mark.asyncio
async def test_new_diagnostics_are_optional_and_do_not_change_core_sleep_or_ownership(monkeypatch):
    core = {"schema_version": 7, "state": "parked", "ownership_enabled": True, "acc_on": False}
    for trial, restart in [
        (trial_settings(), restart_evidence()),
        ({"secret": "no"}, {"boot_id": "no"}),
    ]:
        monkeypatch.setattr(
            obd_transfer.adb,
            "shell",
            AsyncMock(
                return_value=json.dumps(
                    {
                        **core,
                        "trial_settings": trial,
                        "restart_evidence": restart,
                    }
                )
            ),
        )
        result = await obd_transfer.read_logger_status("unit", "/safe/status.json")
        assert all(result[key] == value for key, value in core.items())
        if "schema_version" in trial:
            assert result["trial_settings"] == trial
            assert result["restart_evidence"] == restart
        else:
            assert "trial_settings" not in result and "restart_evidence" not in result
    monkeypatch.setattr(obd_transfer.adb, "shell", AsyncMock(return_value=json.dumps(core)))
    legacy = await obd_transfer.read_logger_status("unit", "/safe/status.json")
    assert all(legacy[key] == value for key, value in core.items())
    assert "trial_settings" not in legacy and "restart_evidence" not in legacy
