"""Resource refusals explain upload fallback without creating exclusion authority."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from test_parked_filter import SOURCE, make_deferred_policy, make_policy, policy_inventory

from app.ingest import clip_exclusions, movement, parked_filter


def test_older_producer_without_diagnostics_remains_compatible():
    doc = parked_filter.validate_policy_manifest(json.dumps(make_policy()))
    assert all(parked_filter.eligible_skip(entry) for entry in doc["decisions"])
    doc["decisions"][0]["visual"]["status"] = "unavailable"
    summary = parked_filter.analysis_summary(doc, "ready")
    assert summary["reason_counts"] == {"reason_not_reported": 1}


def test_new_hard_stop_and_historical_guard_remain_distinct_upload_diagnostics():
    doc = make_policy()
    for entry, reason in zip(
        doc["decisions"], ("cpu_temperature_guard", "cpu_temperature_hard_limit"), strict=True
    ):
        entry.update(decision="upload", reason="visual_incomplete")
        entry["visual"].update(status="unavailable", reason=reason)
    parsed = parked_filter.validate_policy_manifest(json.dumps(doc))
    assert parked_filter.analysis_summary(parsed, "ready")["reason_counts"] == {
        "cpu_temperature_guard": 1,
        "cpu_temperature_hard_limit": 1,
    }
    assert all(not parked_filter.eligible_skip(entry) for entry in parsed["decisions"])


@pytest.mark.parametrize("reason", sorted(parked_filter.VISUAL_REASONS))
def test_reason_can_never_turn_a_quiet_proof_into_exclusion_authority(reason):
    doc = make_policy()
    entry = doc["decisions"][0]
    entry["visual"]["reason"] = reason
    parsed = parked_filter.validate_policy_manifest(json.dumps(doc))
    assert not parked_filter.eligible_skip(parsed["decisions"][0])
    assert parked_filter.eligible_skip(parsed["decisions"][1])


def test_unknown_and_untrusted_diagnostics_are_bounded_and_never_exposed():
    for unsafe in [
        "/private/secret.ts",
        "Bearer secret",
        "x" * 10_000,
        {"raw_error": "private"},
        4,
    ]:
        doc = make_policy()
        doc["decisions"][0]["visual"]["reason"] = unsafe
        parsed = parked_filter.validate_policy_manifest(json.dumps(doc))
        assert parsed["decisions"][0]["visual"]["reason"] == "unrecognized_reason"
        assert not parked_filter.eligible_skip(parsed["decisions"][0])
        summary = parked_filter.analysis_summary(parsed, "ready")
        assert summary["reason_counts"] == {"unrecognized_reason": 1}
        assert "private" not in json.dumps(summary)


def test_pending_diagnostics_may_not_hide_a_refusal_by_deferring():
    doc = make_deferred_policy()
    entry = doc["decisions"][0]
    now = datetime.now(UTC)
    for reason, allowed in [
        (None, True),
        ("analysis_pending", True),
        ("cpu_temperature_guard", False),
        ("unrecognized_reason", False),
    ]:
        entry["visual"]["reason"] = reason
        assert parked_filter.eligible_defer(entry, now=now, native_remaining_s=1_000) is allowed


def test_summary_survives_restart_without_publishing_paths_or_raw_proofs(tmp_path):
    doc = make_policy()
    for entry in doc["decisions"]:
        entry["visual"].update(status="unavailable", reason="cpu_temperature_guard")
    summary = parked_filter.analysis_summary(doc, "ready")
    store = movement.MovementStatus(tmp_path / "last-observation.json")
    store.update_policy_analysis(summary)
    store._persist()
    restored = movement.MovementStatus(store.path)
    restored.load_once()
    result = restored.snapshot()["policy_analysis"]
    assert result["reason_counts"] == {"cpu_temperature_guard": 2}
    assert result["status_counts"] == {"unavailable": 2}
    assert result["stale"] is False
    assert "relative_path" not in json.dumps(result)
    assert "fingerprint" not in json.dumps(result)
    later = datetime.now(UTC) + timedelta(seconds=181)
    assert restored.snapshot(now=later)["policy_analysis"]["stale"] is True
    assert restored.snapshot()["state"] == "unavailable"


def test_invalid_saved_summary_cannot_supply_arbitrary_categories(tmp_path):
    summary = parked_filter.analysis_summary(make_policy(), "ready")
    for change in [
        {"reason_counts": {"/private/file": 1}},
        {"clip_count": 513},
        {"status_counts": {"quiet": True}},
    ]:
        invalid = {**copy.deepcopy(summary), **change}
        with pytest.raises(movement.ManifestError):
            movement.MovementStatus(tmp_path / "last.json").update_policy_analysis(invalid)


async def test_movement_api_exposes_diagnostics_without_contacting_camera(
    client, monkeypatch, tmp_path
):
    doc = make_policy()
    doc["decisions"][0]["visual"].update(status="error", reason="decoder_failure")
    store = movement.MovementStatus(tmp_path / "last.json")
    store.update_policy_analysis(parked_filter.analysis_summary(doc, "ready"))
    store.loaded = True
    monkeypatch.setattr("app.api.routes.ingest_movement.get_movement_status", lambda: store)
    response = await client.get("/api/ingest/movement-status")
    assert response.status_code == 200
    assert response.json()["policy_analysis"]["reason_counts"] == {"decoder_failure": 1}


@pytest.mark.parametrize("blocked_stage", ["load", "persist"])
@pytest.mark.parametrize("audit_failed", [False, True])
async def test_stalled_optional_diagnostics_never_block_the_independent_exclusion_audit(
    monkeypatch, tmp_path, blocked_stage, audit_failed
):
    doc = make_policy()
    inventory = policy_inventory(doc)
    logger = {
        "capabilities": [parked_filter.CAPABILITY],
        "transfer_policy_enabled": True,
        "transfer_policy_state": "ready",
        "movement_state": "ready",
        "updated_at_utc": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(parked_filter, "_read_policy", AsyncMock(return_value=doc))
    monkeypatch.setattr(movement, "read_remote_card_id", AsyncMock(return_value=doc["card_id"]))
    monkeypatch.setattr(
        parked_filter,
        "_verify_fingerprints",
        AsyncMock(return_value={entry["clip"]["clip_id"] for entry in doc["decisions"]}),
    )
    audit = AsyncMock(side_effect=OSError("audit unavailable") if audit_failed else None)
    monkeypatch.setattr(clip_exclusions, "record_evaluation", audit)
    monkeypatch.setattr(parked_filter, "DIAGNOSTICS_TIMEOUT_S", 0.02)
    store = movement.MovementStatus(tmp_path / "last.json")
    store.loaded = blocked_stage == "persist"
    entered, release = threading.Event(), threading.Event()

    def stalled_file_worker():
        entered.set()
        assert release.wait(2), "test must release the retained file worker"

    monkeypatch.setattr(
        store, "load_once" if blocked_stage == "load" else "_persist", stalled_file_worker
    )
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    try:
        result = await asyncio.wait_for(
            parked_filter.filter_inventory(
                "unit", "/obd/status.json", inventory, SOURCE, logger, enabled=True
            ),
            timeout=0.5,
        )
        assert entered.is_set()
        task = store.load_task if blocked_stage == "load" else store.persist_task
        assert task is not None and not task.done() and not task.cancelled()
        audit.assert_awaited_once()
        assert audit.await_args.kwargs["excluded"] == doc["decisions"]
        if audit_failed:
            assert result.files == inventory and result.skipped == []
        else:
            assert result.files == [] and result.skipped == inventory
    finally:
        release.set()
        task = store.load_task if blocked_stage == "load" else store.persist_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
