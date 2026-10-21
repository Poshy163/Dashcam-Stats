"""Movement reports are bounded observations, never instructions to discard footage."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ingest import movement, obd_transfer, puller
from app.ingest.models import RemoteFile

START = datetime(2026, 10, 21, 2, 30, tzinfo=UTC)


@pytest.fixture
def manifest():
    return {
        "schema_version": 1,
        "source_id": str(uuid.uuid4()),
        "card_id": "a" * 64,
        "session_id": str(uuid.uuid4()),
        "boot_id": str(uuid.uuid4()),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "producer_version": "0.3.9",
        "classifier_version": 1,
        "report_only": True,
        "truncated": False,
        "coverage_start_utc": START.isoformat(),
        "coverage_end_utc": (START + timedelta(minutes=1)).isoformat(),
        "status": "ready",
        "clips": [
            {
                "clip_id": str(uuid.uuid4()),
                "relative_path": "DCIM/Video/20261021130000_camera_0.ts",
                "camera_key": "camera_0",
                "locked": False,
                "size_bytes": 61_700_000,
                "mtime_ms": 1_792_546_260_123,
                "fingerprint": None,
                "local_start": "2026-10-21T13:00:00",
                "timezone": "Australia/Adelaide",
                "start_utc": START.isoformat(),
                "end_utc": (START + timedelta(minutes=1)).isoformat(),
                "duration_ms": None,
                "time_quality": "bounded",
                "classification": "sampled_stationary",
                "reasons": ["zero_speed_samples", "report_only"],
                "analysis_status": "complete",
                "coverage": {
                    "window_ms": 60_000,
                    "acc_ms": 0,
                    "speed_ms": 60_000,
                    "gps_ms": 0,
                    "unknown_ms": 0,
                    "max_gap_ms": 5000,
                },
                "evidence": {
                    "acc_on_ms": 0,
                    "acc_off_ms": 0,
                    "valid_speed_samples": 12,
                    "moving_samples": 0,
                    "max_speed_kmh": 0,
                    "gps_speed_samples": 0,
                    "gps_moving_samples": 0,
                    "max_gps_speed_m_s": None,
                },
            }
        ],
    }


def remote_for(clip):
    return RemoteFile(
        clip["relative_path"].split("/")[-1],
        clip["size_bytes"],
        clip["mtime_ms"] // 1000,
        "/storage/Tfcard/" + clip["relative_path"].rsplit("/", 1)[0],
    )


def verified_inventory(doc, remote, source):
    return movement.inventory_summary(doc, remote, source, card_identity="verified")


def test_valid_report_matches_identity_without_changing_transfer_plan(manifest, tmp_path):
    doc = movement.validate_manifest(json.dumps(manifest))
    remote = [remote_for(doc["clips"][0])]
    original = copy.deepcopy(remote)
    before = puller.delta(remote, tmp_path, skip_active_s=0, camera="both")
    summary = verified_inventory(doc, remote, "/storage/Tfcard/DCIM/Video")
    after = puller.delta(remote, tmp_path, skip_active_s=0, camera="both")
    assert summary["matched_clips"] == 1
    assert summary["class_counts"]["sampled_stationary"] == 1
    assert remote == original and after == before and after.files == remote


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(schema_version=2),
        lambda d: d.update(schema_version=True),
        lambda d: d.update(classifier_version=2),
        lambda d: d.update(report_only=False),
        lambda d: d.update(untrusted_coordinates=[1.0, 2.0]),
        lambda d: d.update(card_id="private storage URI"),
        lambda d: d.update(status=[]),
        lambda d: d["clips"][0].update(relative_path="DCIM/Video/../../private.ts"),
        lambda d: d["clips"][0].update(camera_key="camera_1"),
        lambda d: d["clips"][0].update(locked=True),
        lambda d: d["clips"][0].update(size_bytes=True),
        lambda d: d["clips"][0].update(size_bytes=1024**4 + 1),
        lambda d: d["clips"][0].update(reasons=["Authorization: Bearer secret"]),
        lambda d: d["clips"][0].update(time_quality=[]),
        lambda d: d["clips"][0].update(timezone="private arbitrary text"),
        lambda d: d["clips"][0].update(local_start="2026-10-22T13:00:00"),
        lambda d: d["clips"][0]["evidence"].update(latitude=1.0),
        lambda d: d["clips"][0]["evidence"].update(max_speed_kmh=float("nan")),
        lambda d: d["clips"][0]["coverage"].update(speed_ms=-1),
        lambda d: d["clips"][0].update(
            fingerprint={"algorithm": "md5", "digest": "b" * 64, "sample_bytes": 16384}
        ),
    ],
)
def test_malformed_or_privacy_expanding_reports_are_rejected(manifest, mutation):
    mutation(manifest)
    with pytest.raises(movement.ManifestError):
        movement.validate_manifest(json.dumps(manifest))


def test_duplicate_json_keys_paths_and_size_limits_are_rejected(manifest):
    raw = json.dumps(manifest)
    with pytest.raises(movement.ManifestError):
        movement.validate_manifest(
            raw.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        )
    with pytest.raises(movement.ManifestError):
        movement.validate_manifest(" " * (movement.MAX_BYTES + 1))
    manifest["clips"].append(copy.deepcopy(manifest["clips"][0]))
    with pytest.raises(movement.ManifestError, match="duplicate_clip"):
        movement.validate_manifest(json.dumps(manifest))


@pytest.mark.parametrize("change", ["size", "mtime", "directory", "filename"])
def test_reused_or_changed_remote_identity_is_unknown(manifest, change):
    item = remote_for(manifest["clips"][0])
    item = RemoteFile(
        "20261021130100_camera_0.ts" if change == "filename" else item.name,
        item.size + (change == "size"),
        item.mtime + (change == "mtime"),
        "/storage/Tfcard/DCIM/LockVideo" if change == "directory" else item.directory,
    )
    summary = verified_inventory(manifest, [item], "")
    assert summary["matched_clips"] == 0
    assert summary["class_counts"]["unknown"] == 1
    assert summary["class_bytes"]["unknown"] == item.size


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(time_quality="unknown"),
        lambda c: c.update(analysis_status="unknown"),
        lambda c: c.update(timezone="Unknown/Timezone"),
        lambda c: c.update(
            start_utc=(START + timedelta(minutes=1)).isoformat(),
            end_utc=(START + timedelta(minutes=2)).isoformat(),
        ),
        lambda c: c["coverage"].update(speed_ms=50_000),
        lambda c: c["coverage"].update(max_gap_ms=20_000),
        lambda c: c["coverage"].update(speed_ms=70_000),
        lambda c: c["evidence"].update(max_speed_kmh=None),
        lambda c: c["evidence"].update(moving_samples=1),
        lambda c: c["evidence"].update(gps_moving_samples=2, gps_speed_samples=2),
    ],
)
def test_stationary_advice_with_missing_or_conflicting_evidence_becomes_unknown(manifest, mutation):
    clip = manifest["clips"][0]
    mutation(clip)
    summary = verified_inventory(manifest, [remote_for(clip)], "")
    assert summary["matched_clips"] == 1
    assert summary["class_counts"]["unknown"] == 1


def test_gps_cannot_establish_stationarity_and_acc_off_is_only_a_candidate(manifest):
    clip = manifest["clips"][0]
    clip["coverage"].update(speed_ms=0, gps_ms=60_000, acc_ms=60_000)
    clip["evidence"].update(
        valid_speed_samples=0,
        max_speed_kmh=None,
        gps_speed_samples=12,
        max_gps_speed_m_s=0,
        acc_off_ms=60_000,
    )
    assert verified_inventory(manifest, [remote_for(clip)], "")["class_counts"]["unknown"] == 1
    clip["classification"] = "acc_off_candidate"
    assert (
        verified_inventory(manifest, [remote_for(clip)], "")["class_counts"]["acc_off_candidate"]
        == 1
    )
    clip["evidence"]["acc_on_ms"] = 10_000
    assert verified_inventory(manifest, [remote_for(clip)], "")["class_counts"]["unknown"] == 1


def test_orphan_is_never_matched_or_treated_as_a_finalized_clip(manifest):
    clip = manifest["clips"][0]
    clip.update(
        relative_path="DCIM/pre_20261021130000_camera_0.ts",
        classification="unknown",
        time_quality="unknown",
    )
    movement.validate_manifest(json.dumps(manifest))
    summary = verified_inventory(manifest, [remote_for(clip)], "/storage/Tfcard/DCIM/Video")
    assert summary["matched_clips"] == 0
    clip["classification"] = "sampled_stationary"
    with pytest.raises(movement.ManifestError, match="orphan"):
        movement.validate_manifest(json.dumps(manifest))


@pytest.mark.parametrize("age_seconds", [601, -121])
def test_stale_or_future_observation_retains_history_but_current_advice_is_unknown(
    manifest, tmp_path, age_seconds
):
    now = datetime.now(UTC)
    manifest["generated_at_utc"] = (now - timedelta(seconds=age_seconds)).isoformat()
    store = movement.MovementStatus(tmp_path / "last.json")
    store.observe(
        manifest, verified_inventory(manifest, [remote_for(manifest["clips"][0])], ""), None
    )
    snapshot = store.snapshot(now=now)
    assert snapshot["state"] == "stale"
    assert snapshot["inventory"]["class_counts"]["unknown"] == 1
    assert snapshot["manifest"]["class_counts"]["sampled_stationary"] == 1
    assert snapshot["transfer_filtering"] is False


def test_snapshot_survives_restart_without_exposing_private_identity(manifest, tmp_path):
    path = tmp_path / "movement" / "last.json"
    store = movement.MovementStatus(path)
    inventory = verified_inventory(manifest, [remote_for(manifest["clips"][0])], "")
    store.observe(manifest, inventory, None)
    restored = movement.MovementStatus(path)
    restored.load_once()
    now = datetime.now(UTC)
    assert restored.snapshot(now=now)["manifest"] == store.snapshot(now=now)["manifest"]
    assert restored.snapshot(now=now)["state"] == "stale"
    assert restored.snapshot(now=now)["requires_live_verification"]
    assert not restored.snapshot(now=now)["card_identity_verified"]
    assert restored.snapshot(now=now)["inventory"]["class_counts"]["unknown"] == 1
    body = json.dumps(restored.snapshot())
    for private in (
        manifest["source_id"],
        manifest["card_id"],
        manifest["clips"][0]["relative_path"],
    ):
        assert private not in body
    assert path.stat().st_size <= movement.MAX_STORED_BYTES
    assert not list(path.parent.glob("*.partial"))


def test_invalid_latest_observation_preserves_last_valid_snapshot_as_history(manifest, tmp_path):
    store = movement.MovementStatus(tmp_path / "last.json")
    remote = [remote_for(manifest["clips"][0])]
    store.observe(manifest, verified_inventory(manifest, remote, ""), None)
    received = store.received_at
    store.observe(None, verified_inventory(None, remote, ""), "invalid_manifest")
    snapshot = store.snapshot()
    assert snapshot["state"] == "invalid"
    assert snapshot["retained_snapshot"] and snapshot["received_at"] == received
    assert snapshot["inventory"]["class_counts"]["unknown"] == 1


def test_corrupt_or_oversized_persisted_snapshot_cannot_enter_api(tmp_path):
    path = tmp_path / "last.json"
    for raw in ('{"secret":"bearer token"}', "x" * (movement.MAX_STORED_BYTES + 1)):
        path.write_text(raw)
        store = movement.MovementStatus(path)
        store.load_once()
        assert store.snapshot()["state"] == "unavailable"
        assert "secret" not in json.dumps(store.snapshot())


async def test_reader_is_bounded_regular_file_only_and_uses_configured_obd_root(monkeypatch):
    shell = AsyncMock(return_value=movement._MISSING)
    monkeypatch.setattr(movement.adb, "shell", shell)
    assert await movement.read_remote_manifest("unit", "/safe/obd/status.json") is None
    command = shell.call_args.args[1]
    assert "head -c 524289 '/safe/obd/movement/report.json'" in command
    assert "[ ! -L '" in command and "[ -f '" in command
    assert shell.call_args.kwargs["timeout"] == 3.0
    shell.return_value = movement._PREFIX + "x" * (movement.MAX_BYTES + 1)
    with pytest.raises(movement.ManifestError, match="too_large"):
        await movement.read_remote_manifest("unit", "/safe/obd/status.json")
    shell.reset_mock()
    with pytest.raises(ValueError):
        await movement.read_remote_manifest("unit", "/bad'path/status.json")
    shell.assert_not_awaited()


@pytest.mark.parametrize("card_identity", ["unknown", "mismatch"])
def test_card_identity_must_be_verified_before_inventory_advice(manifest, card_identity):
    clip = manifest["clips"][0]
    summary = movement.inventory_summary(
        manifest, [remote_for(clip)], "", card_identity=card_identity
    )
    assert summary["matched_clips"] == 0
    assert summary["class_counts"]["unknown"] == 1
    assert summary["card_identity"] == card_identity


async def test_card_token_is_read_from_resolved_mount_not_the_alias_or_report(monkeypatch):
    shell = AsyncMock(return_value="/storage/ABCD-1234/DCIM/Video")
    monkeypatch.setattr(movement.adb, "shell", shell)
    expected = hashlib.sha256(b"ABCD-1234").hexdigest()
    assert await movement.read_remote_card_id("unit", "/storage/Tfcard/DCIM/Video") == expected
    assert shell.call_args.args[1] == "readlink -f '/storage/Tfcard/DCIM/Video'"
    shell.return_value = "/storage/Tfcard/DCIM/Video"
    assert await movement.read_remote_card_id("unit", "/storage/Tfcard/DCIM/Video") is None
    shell.side_effect = RuntimeError("private output")
    assert await movement.read_remote_card_id("unit", "/storage/Tfcard/DCIM/Video") is None


def test_one_qualifying_gps_pair_represents_two_fixes_and_only_proves_movement(manifest):
    clip = manifest["clips"][0]
    clip["classification"] = "moving_observed"
    clip["evidence"].update(gps_moving_samples=1, gps_speed_samples=2, max_gps_speed_m_s=4.0)
    assert (
        verified_inventory(manifest, [remote_for(clip)], "")["class_counts"]["moving_observed"] == 1
    )
    clip["evidence"]["gps_speed_samples"] = 1
    assert verified_inventory(manifest, [remote_for(clip)], "")["class_counts"]["unknown"] == 1


async def test_optional_read_timeout_fails_open_without_altering_remote_list(monkeypatch, tmp_path):
    store = movement.MovementStatus(tmp_path / "last.json")
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr(movement, "READ_TIMEOUT_SECONDS", 0.01)

    async def blocked(*args, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(movement.adb, "shell", blocked)
    remote = [RemoteFile("20261021130000_camera_0.ts", 123, 1, "/card/DCIM/Video")]
    original = copy.deepcopy(remote)
    began = time.monotonic()
    await movement.inspect_before_plan("unit", "/obd/status.json", remote, "")
    assert time.monotonic() - began < 0.5
    assert remote == original
    assert store.snapshot()["error"] == "read_timeout"
    assert store.snapshot()["inventory"]["class_counts"]["unknown"] == 1


async def test_cancellation_is_not_swallowed_by_optional_reader(monkeypatch, tmp_path):
    store = movement.MovementStatus(tmp_path / "last.json")
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr(
        movement, "read_remote_manifest", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await movement.inspect_before_plan("unit", "/obd/status.json", [], "")
    assert store.checked_at is None


async def test_valid_reader_matches_live_card_and_retains_aggregate_snapshot(
    manifest, monkeypatch, tmp_path
):
    store = movement.MovementStatus(tmp_path / "last.json")
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr(
        movement, "read_remote_manifest", AsyncMock(return_value=json.dumps(manifest))
    )
    monkeypatch.setattr(
        movement, "read_remote_card_id", AsyncMock(return_value=manifest["card_id"])
    )
    await movement.inspect_before_plan(
        "unit", "/obd/status.json", [remote_for(manifest["clips"][0])], "/card/DCIM/Video"
    )
    snapshot = store.snapshot()
    assert snapshot["state"] == "ready" and snapshot["card_identity_verified"]
    assert snapshot["inventory"]["class_counts"]["sampled_stationary"] == 1
    assert snapshot["fingerprints_verified"] is False
    assert snapshot["transfer_filtering"] is False


async def test_actual_android_indexer_export_round_trips_through_backend(monkeypatch, tmp_path):
    # Produced by the Android ClosedClipIndexerTest's real SAF fake-provider,
    # stable-file/fingerprint path and atomic report publisher. Entirely synthetic.
    fixture = Path(__file__).parent / "fixtures" / "movement-report-v1-android.json"
    doc = movement.validate_manifest(fixture.read_text(encoding="utf-8"))
    assert len(doc["clips"]) == 6
    assert {clip["camera_key"] for clip in doc["clips"]} == {"camera_0", "camera_1"}
    assert any(clip["locked"] for clip in doc["clips"])
    assert any(clip["relative_path"].startswith("DCIM/pre_") for clip in doc["clips"])
    assert any(clip["fingerprint"] is not None for clip in doc["clips"])
    # Avoid coupling a permanent wire-contract fixture to the test runner's date.
    doc["generated_at_utc"] = datetime.now(UTC).isoformat()
    remote = [remote_for(clip) for clip in doc["clips"] if "/pre_" not in clip["relative_path"]]
    store = movement.MovementStatus(tmp_path / "last.json")
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr(movement, "read_remote_manifest", AsyncMock(return_value=json.dumps(doc)))
    monkeypatch.setattr(movement, "read_remote_card_id", AsyncMock(return_value=doc["card_id"]))
    await movement.inspect_before_plan(
        "unit", "/obd/status.json", remote, "/storage/Tfcard/DCIM/Video"
    )
    snapshot = store.snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["inventory"]["matched_clips"] == len(remote)
    assert snapshot["inventory"]["class_counts"]["unknown"] == len(remote)
    assert not snapshot["transfer_filtering"] and not snapshot["fingerprints_verified"]
    restored = movement.MovementStatus(store.path)
    restored.load_once()
    assert restored.snapshot()["manifest"]["clip_count"] == 6


async def test_slow_persistence_does_not_hold_backup_or_the_public_snapshot(
    manifest, monkeypatch, tmp_path
):
    store = movement.MovementStatus(tmp_path / "last.json")
    entered, release = threading.Event(), threading.Event()

    def slow_persist():
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(store, "_persist", slow_persist)
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr(
        movement, "read_remote_manifest", AsyncMock(return_value=json.dumps(manifest))
    )
    monkeypatch.setattr(
        movement, "read_remote_card_id", AsyncMock(return_value=manifest["card_id"])
    )
    started = time.monotonic()
    task = asyncio.create_task(movement.inspect_before_plan("unit", "/obd/status.json", [], ""))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(task, timeout=1.5)
        assert time.monotonic() - started < 1.5
        assert store.snapshot()["state"] == "ready"
    finally:
        release.set()
        if store.persist_task is not None:
            await asyncio.wait_for(asyncio.shield(store.persist_task), timeout=1)


async def test_whole_observation_timeout_bounds_validator_and_admits_only_one_worker(
    manifest, monkeypatch, tmp_path
):
    store = movement.MovementStatus(tmp_path / "last.json")
    entered, release = threading.Event(), threading.Event()
    calls = 0
    original = movement.validate_manifest

    def blocked(raw):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(5)
        return original(raw)

    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr(movement, "validate_manifest", blocked)
    monkeypatch.setattr(movement, "INSPECTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        movement, "read_remote_manifest", AsyncMock(return_value=json.dumps(manifest))
    )
    began = time.monotonic()
    try:
        await movement.inspect_before_plan("unit", "/obd/status.json", [], "")
        assert entered.is_set()
        assert time.monotonic() - began < 0.5
        assert store.snapshot()["error"] == "read_timeout"
        await movement.inspect_before_plan("unit", "/obd/status.json", [], "")
        assert calls == 1
        assert not movement._validation_task.done()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.shield(movement._validation_task), timeout=1)
    assert store.snapshot()["state"] == "error"
    assert store.doc is None


def test_older_concurrent_write_is_historical_after_restart(manifest, monkeypatch, tmp_path):
    store = movement.MovementStatus(tmp_path / "last.json")
    remote = [remote_for(manifest["clips"][0])]
    entered, release = threading.Event(), threading.Event()
    original = movement.os.fsync

    def blocked(descriptor):
        entered.set()
        assert release.wait(5)
        original(descriptor)

    monkeypatch.setattr(movement.os, "fsync", blocked)
    worker = threading.Thread(
        target=store.observe, args=(manifest, verified_inventory(manifest, remote, ""), None)
    )
    worker.start()
    try:
        assert entered.wait(1)
        store.observe(None, verified_inventory(None, remote, ""), "invalid_manifest")
        assert store.snapshot()["state"] == "invalid"
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    restored = movement.MovementStatus(store.path)
    restored.load_once()
    assert restored.snapshot()["state"] == "stale"
    assert restored.snapshot()["requires_live_verification"]
    assert restored.snapshot()["inventory"]["class_counts"]["unknown"] == 1
    assert not restored.snapshot()["card_identity_verified"]


@pytest.fixture
async def blocked_manifest_disk_load(manifest, monkeypatch, tmp_path):
    path = tmp_path / "last.json"
    seeded = movement.MovementStatus(path)
    seeded.observe(
        manifest, verified_inventory(manifest, [remote_for(manifest["clips"][0])], ""), None
    )
    old_raw = path.read_text(encoding="utf-8")
    store = movement.MovementStatus(path)
    entered, release = threading.Event(), threading.Event()
    original_read = Path.read_text

    def blocked(candidate, *args, **kwargs):
        if candidate == path:
            entered.set()
            assert release.wait(5)
            return old_raw
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked)
    monkeypatch.setattr(movement, "get_movement_status", lambda: store)
    monkeypatch.setattr("app.api.routes.ingest_movement.get_movement_status", lambda: store)
    try:
        yield SimpleNamespace(store=store, entered=entered, release=release)
    finally:
        release.set()
        if store.load_task is not None:
            await asyncio.wait_for(asyncio.shield(store.load_task), timeout=1)
        if store.persist_task is not None:
            await asyncio.wait_for(asyncio.shield(store.persist_task), timeout=1)


async def test_get_timeout_never_waits_for_disk_lock_and_late_load_cannot_replace_live_ram(
    client, blocked_manifest_disk_load, manifest, monkeypatch
):
    blocked = blocked_manifest_disk_load
    began = time.monotonic()
    request = asyncio.create_task(client.get("/api/ingest/movement-status"))
    assert await asyncio.to_thread(blocked.entered.wait, 1)
    response = await asyncio.wait_for(request, timeout=1)
    assert time.monotonic() - began < 1.5
    assert response.status_code == 200 and response.json()["state"] == "unavailable"
    manifest["producer_version"] = "0.3.10"
    monkeypatch.setattr(
        movement, "read_remote_manifest", AsyncMock(return_value=json.dumps(manifest))
    )
    monkeypatch.setattr(
        movement, "read_remote_card_id", AsyncMock(return_value=manifest["card_id"])
    )
    await asyncio.wait_for(
        movement.inspect_before_plan("unit", "/obd/status.json", [], ""), timeout=1
    )
    assert blocked.store.snapshot()["producer_version"] == "0.3.10"
    blocked.release.set()
    await asyncio.wait_for(asyncio.shield(blocked.store.load_task), timeout=1)
    assert blocked.store.snapshot()["producer_version"] == "0.3.10"
    assert not blocked.store.snapshot()["requires_live_verification"]


async def test_inspection_timeout_can_update_unknown_ram_while_initial_disk_load_is_blocked(
    blocked_manifest_disk_load, manifest, monkeypatch
):
    blocked = blocked_manifest_disk_load
    monkeypatch.setattr(movement, "INSPECTION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        movement, "read_remote_manifest", AsyncMock(return_value=json.dumps(manifest))
    )
    monkeypatch.setattr(
        movement, "read_remote_card_id", AsyncMock(return_value=manifest["card_id"])
    )
    began = time.monotonic()
    await asyncio.wait_for(
        movement.inspect_before_plan("unit", "/obd/status.json", [], ""), timeout=0.5
    )
    assert blocked.entered.is_set() and time.monotonic() - began < 0.5
    assert blocked.store.snapshot()["error"] == "read_timeout"
    assert blocked.store.doc is None
    blocked.release.set()
    await asyncio.wait_for(asyncio.shield(blocked.store.load_task), timeout=1)
    assert blocked.store.doc is None and blocked.store.snapshot()["error"] == "read_timeout"


async def test_api_exposes_directory_requirement_and_only_reads_cached_state(
    client, monkeypatch, tmp_path, manifest
):
    manifest.update(card_id=None, status="directory_required", clips=[])
    movement.validate_manifest(json.dumps(manifest))
    store = movement.MovementStatus(tmp_path / "last.json")
    store.observe(manifest, verified_inventory(manifest, [], ""), None)
    monkeypatch.setattr("app.api.routes.ingest_movement.get_movement_status", lambda: store)
    shell = AsyncMock(side_effect=AssertionError("GET must never contact device"))
    monkeypatch.setattr(movement.adb, "shell", shell)
    response = await client.get("/api/ingest/movement-status")
    assert response.status_code == 200
    body = response.json()
    assert body["report_status"] == "directory_required"
    assert body["report_only"] and not body["transfer_filtering"]
    shell.assert_not_awaited()


async def test_new_companion_movement_capability_and_fields_reach_obd_status(client, monkeypatch):
    raw = {
        "schema_version": 5,
        "state": "parked",
        "app_version_name": "0.3.9",
        "app_version_code": 22,
        "capabilities": [movement.CAPABILITY],
        "movement_state": "directory_required",
        "movement_clip_count": 0,
        "movement_gps_state": "permission_required",
        "movement_report_only": True,
    }
    monkeypatch.setattr(obd_transfer.adb, "shell", AsyncMock(return_value=json.dumps(raw)))
    clean = await obd_transfer.read_logger_status("unit", "/safe/status.json")
    assert clean["capabilities"] == [movement.CAPABILITY]
    for key in (
        "movement_state",
        "movement_clip_count",
        "movement_gps_state",
        "movement_report_only",
    ):
        assert clean[key] == raw[key]
    transfer = obd_transfer.get_obd_transfer_status()
    previous = transfer.logger
    try:
        transfer.set_logger(clean)
        response = await client.get("/api/obd/status")
        assert response.status_code == 200
        assert response.json()["logger"]["movement_state"] == "directory_required"
        assert movement.CAPABILITY in response.json()["logger"]["capabilities"]
    finally:
        transfer.set_logger(previous)


@pytest.mark.parametrize(
    "key,value",
    [
        ("movement_state", "Authorization secret"),
        ("movement_gps_state", "private location"),
        ("movement_clip_count", True),
        ("movement_clip_count", 513),
        ("movement_report_only", False),
    ],
)
async def test_new_logger_movement_fields_remain_bounded_and_report_only(monkeypatch, key, value):
    monkeypatch.setattr(obd_transfer.adb, "shell", AsyncMock(return_value=json.dumps({key: value})))
    assert key not in await obd_transfer.read_logger_status("unit", "/safe/status.json")
