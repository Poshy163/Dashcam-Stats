"""Only fresh, complete, paired quiet proofs may omit footage from wireless transfer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from test_ingest_preparation_lifecycle import isolated_mirror as isolated_mirror
from test_ingest_preparation_lifecycle import prepared_unit as prepared_unit

from app.ingest import clip_exclusions, movement, obd_transfer, parked_filter, puller
from app.ingest.models import RemoteFile, RunState

START = datetime(2026, 10, 21, 2, 30, tzinfo=UTC)
SOURCE = "/storage/Tfcard/DCIM/Video"


def make_policy():
    decisions = []
    for camera in (0, 1):
        clip = {
            "clip_id": str(uuid.uuid4()),
            "relative_path": f"DCIM/Video/20261021130000_camera_{camera}.ts",
            "camera_key": f"camera_{camera}",
            "locked": False,
            "size_bytes": 61700000,
            "mtime_ms": int((START + timedelta(minutes=1)).timestamp() * 1000),
            "fingerprint": {
                "algorithm": "sha256-sampled-v1",
                "sample_bytes": 16384,
                "digest": str(camera + 1) * 64,
            },
            "local_start": "2026-10-21T13:00:00",
            "timezone": "Australia/Adelaide",
            "start_utc": START.isoformat(),
            "end_utc": (START + timedelta(minutes=1)).isoformat(),
            "duration_ms": 60000,
            "time_quality": "bounded",
            "classification": "acc_off_candidate",
            "reasons": ["acc_off_observed", "report_only"],
            "analysis_status": "complete",
            "coverage": {
                "window_ms": 60000,
                "acc_ms": 60000,
                "speed_ms": 0,
                "gps_ms": 0,
                "unknown_ms": 0,
                "max_gap_ms": 5000,
            },
            "evidence": {
                "acc_on_ms": 0,
                "acc_off_ms": 60000,
                "valid_speed_samples": 0,
                "moving_samples": 0,
                "max_speed_kmh": None,
                "gps_speed_samples": 0,
                "gps_moving_samples": 0,
                "max_gps_speed_m_s": None,
            },
        }
        decisions.append(
            {
                "clip": clip,
                "decision": "skip",
                "reason": "parked_visual_quiet",
                "defer_until_utc": None,
                "guard": {
                    "start_utc": (START - timedelta(seconds=122)).isoformat(),
                    "end_utc": (START + timedelta(seconds=182)).isoformat(),
                    "window_ms": 304000,
                    "acc_off_ms": 304000,
                    "unknown_ms": 0,
                    "max_gap_ms": 5000,
                    "observation_count": 62,
                    "on_samples": 0,
                    "unknown_samples": 0,
                    "segment_count": 1,
                    "positive_speed_samples": 0,
                },
                "visual": {
                    "algorithm_version": 2,
                    "motion_metric": "adjacent_rolling6_coherent3x3_v1",
                    "rolling_frames": 6,
                    "max_coherent_pixels": 1,
                    "max_rolling_coherent_pixels": 30,
                    "max_consecutive_coherent_frames": 0,
                    "roi_profile": "dashcam_osd_bottom4_v1",
                    "roi_rows": 86,
                    "min_mean_luma": 80.0,
                    "max_mean_luma": 100.0,
                    "max_dark_fraction": 0.1,
                    "max_bright_fraction": 0.1,
                    "min_contrast_span": 60,
                    "status": "quiet",
                    "input_frames": 1801,
                    "decoded_frames": 1801,
                    "sampled_frames": 1801,
                    "duration_ms": 60000,
                    "covered_ms": 60000,
                    "max_gap_ms": 34,
                    "motion_frames": 0,
                    "analysis_completed_at_utc": (START + timedelta(minutes=4)).isoformat(),
                    "first_pts_us": 1000000,
                    "last_pts_us": 61000000,
                    "input_eos": True,
                    "decoder_eos": True,
                    "grid_width": 160,
                    "grid_height": 90,
                    "max_changed_pixels": 0,
                },
            }
        )
    return {
        "schema_version": 1,
        "source_id": str(uuid.uuid4()),
        "card_id": "a" * 64,
        "session_id": str(uuid.uuid4()),
        "boot_id": str(uuid.uuid4()),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "producer_version": "0.3.10",
        "policy_version": "parked_visual_quiet_v1",
        "enabled": True,
        "mode": "acc_off_visual_quiet",
        "status": "ready",
        "truncated": False,
        "decisions": decisions,
    }


def make_deferred_policy(*, now=None):
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    doc = make_policy()
    start, end = now - timedelta(seconds=90), now - timedelta(seconds=30)
    local = start.astimezone(ZoneInfo("Australia/Adelaide")).replace(tzinfo=None)
    doc["generated_at_utc"] = now.isoformat()
    for entry in doc["decisions"]:
        clip, guard, visual = entry["clip"], entry["guard"], entry["visual"]
        clip.update(
            relative_path=f"DCIM/Video/{local:%Y%m%d%H%M%S}_{clip['camera_key']}.ts",
            local_start=local.isoformat(),
            start_utc=start.isoformat(),
            end_utc=end.isoformat(),
            duration_ms=None,
            mtime_ms=int(end.timestamp() * 1000),
        )
        first, last = start - timedelta(seconds=122), now - timedelta(seconds=1)
        window = round((last - first).total_seconds() * 1000)
        guard.update(
            start_utc=first.isoformat(),
            end_utc=last.isoformat(),
            window_ms=window,
            acc_off_ms=window,
            observation_count=window // 5000 + 2,
        )
        entry.update(
            decision="defer",
            reason="analysis_pending",
            defer_until_utc=(end + timedelta(seconds=360)).isoformat(),
        )
        for key in visual:
            if key in {"status", "analysis_completed_at_utc", "first_pts_us", "last_pts_us"}:
                continue
            if isinstance(visual[key], bool):
                visual[key] = False
            elif key not in {
                "algorithm_version",
                "motion_metric",
                "rolling_frames",
                "roi_profile",
                "roi_rows",
                "grid_width",
                "grid_height",
            }:
                visual[key] = 0
        visual.update(
            status="not_analyzed",
            analysis_completed_at_utc=None,
            first_pts_us=None,
            last_pts_us=None,
        )
    return doc


def policy_inventory(doc):
    return [
        RemoteFile(
            entry["clip"]["relative_path"].rsplit("/", 1)[-1],
            entry["clip"]["size_bytes"],
            entry["clip"]["mtime_ms"] // 1000,
            "/storage/Tfcard/" + entry["clip"]["relative_path"].rsplit("/", 1)[0],
        )
        for entry in doc["decisions"]
    ]


@pytest.fixture
def policy(monkeypatch):
    doc = make_policy()
    state = SimpleNamespace(
        doc=doc,
        remote=policy_inventory(doc),
        logger={
            "capabilities": [parked_filter.CAPABILITY],
            "transfer_policy_enabled": True,
            "transfer_policy_state": "ready",
            "movement_state": "ready",
            "updated_at_utc": datetime.now(UTC).isoformat(),
        },
    )
    monkeypatch.setattr(parked_filter, "_read_policy", AsyncMock(return_value=doc))
    monkeypatch.setattr(movement, "read_remote_card_id", AsyncMock(return_value=doc["card_id"]))
    state.fingerprints = AsyncMock(return_value={e["clip"]["clip_id"] for e in doc["decisions"]})
    monkeypatch.setattr(parked_filter, "_verify_fingerprints", state.fingerprints)
    state.record = AsyncMock(return_value={"generation": "committed"})
    monkeypatch.setattr(clip_exclusions, "record_evaluation", state.record)
    return state


async def filtered(policy, *, enabled=True, native_remaining_s=None):
    policy.result = await parked_filter.filter_inventory(
        "unit:5555",
        "/obd/status.json",
        policy.remote,
        SOURCE,
        policy.logger,
        enabled=enabled,
        native_remaining_s=native_remaining_s,
    )
    return policy.result.files


def test_valid_policy_is_strict_and_does_not_promote_stationary_traffic():
    doc = make_policy()
    checked = parked_filter.validate_policy_manifest(json.dumps(doc))
    assert all(parked_filter.eligible_skip(entry) for entry in checked["decisions"])
    clip = checked["decisions"][0]["clip"]
    clip["classification"] = "sampled_stationary"
    clip["evidence"].update(acc_on_ms=60000, acc_off_ms=0, valid_speed_samples=12, max_speed_kmh=0)
    assert not parked_filter.eligible_skip(checked["decisions"][0])


def test_actual_android_publisher_fixture_passes_exact_contract():
    raw = (Path(__file__).parent / "fixtures/transfer-policy-v1-android.json").read_text()
    doc = parked_filter.validate_policy_manifest(raw)
    assert len(doc["decisions"]) == 2
    assert all(parked_filter.eligible_skip(entry) for entry in doc["decisions"])
    assert {entry["clip"]["camera_key"] for entry in doc["decisions"]} == {"camera_0", "camera_1"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("mode", "acc_off_only"),
        ("status", []),
        ("policy_version", "unapproved_v2"),
        ("coordinates", [1.0, 2.0]),
    ],
)
def test_unapproved_or_privacy_expanding_policy_is_rejected(field, value):
    doc = make_policy()
    doc[field] = value
    with pytest.raises(movement.ManifestError):
        parked_filter.validate_policy_manifest(json.dumps(doc))


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("guard", "acc_off_ms", 303999),
        ("guard", "unknown_ms", 1),
        ("guard", "on_samples", 1),
        ("guard", "unknown_samples", 1),
        ("guard", "positive_speed_samples", 1),
        ("guard", "segment_count", 2),
        ("guard", "max_gap_ms", 7501),
        ("guard", "observation_count", 1),
        ("guard", "observation_count", 2),
        ("guard", "start_utc", START.isoformat()),
        ("visual", "status", "activity"),
        ("visual", "motion_frames", 1),
        ("visual", "input_eos", False),
        ("visual", "decoder_eos", False),
        ("visual", "decoded_frames", 1800),
        ("visual", "sampled_frames", 1800),
        ("visual", "covered_ms", 59000),
        ("visual", "max_gap_ms", 151),
        ("visual", "max_gap_ms", 0),
        ("visual", "max_coherent_pixels", 16),
        ("visual", "max_rolling_coherent_pixels", 128),
        ("visual", "max_consecutive_coherent_frames", 2),
        ("visual", "min_mean_luma", 39.9),
        ("visual", "max_mean_luma", 215.1),
        ("visual", "max_dark_fraction", 0.551),
        ("visual", "max_bright_fraction", 0.401),
        ("visual", "min_contrast_span", 23),
        ("visual", "last_pts_us", 1000000),
        ("visual", "grid_width", 80),
        ("clip", "fingerprint", None),
        ("clip", "time_quality", "unknown"),
        ("clip", "timezone", "UTC"),
        ("clip", "duration_ms", None),
        ("clip", "analysis_status", "unknown"),
        ("clip", "reasons", ["file_unreadable"]),
        ("clip", "reasons", ["validated_speed_positive"]),
    ],
)
async def test_missing_uncertain_or_active_proof_keeps_both_lenses(policy, section, field, value):
    policy.doc["decisions"][0][section][field] = value
    assert await filtered(policy) == policy.remote
    assert policy.record.await_args.kwargs["excluded"] == []


async def test_verified_pair_is_audited_before_omission_and_never_becomes_backlog(policy, tmp_path):
    assert await filtered(policy) == []
    assert policy.record.await_args.kwargs["excluded"] == policy.doc["decisions"]
    assert policy.record.await_args.kwargs["status"] == "ready"
    result = puller.delta([], tmp_path, skip_active_s=15, camera="both")
    assert result.files == [] and result.backlog_files == result.backlog_bytes == 0
    assert policy_inventory(policy.doc) == policy.remote, "camera files were never removed"


@pytest.mark.parametrize("server,camera", [(False, True), (True, False), (False, False)])
async def test_both_explicit_opt_ins_are_required(policy, server, camera):
    policy.doc["enabled"] = camera
    assert await filtered(policy, enabled=server) == policy.remote
    policy.fingerprints.assert_not_awaited()
    assert policy.record.await_args.kwargs["excluded"] == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("transfer_policy_enabled", False),
        ("transfer_policy_enabled", None),
        ("transfer_policy_state", "scanning"),
        ("transfer_policy_state", "unavailable"),
        ("movement_state", "storage_unavailable"),
        pytest.param("updated_at_utc", "2000-01-01T00:00:00Z", id="stale-public-status"),
    ],
)
async def test_fresh_public_logger_opt_out_or_uncertainty_prevents_stale_policy_skip(
    policy, field, value
):
    policy.logger[field] = value
    assert await filtered(policy) == policy.remote
    policy.fingerprints.assert_not_awaited()
    assert policy.record.await_args.kwargs["excluded"] == []


async def test_new_companion_policy_fields_survive_public_status_validation(monkeypatch):
    body = {
        "transfer_policy_enabled": True,
        "transfer_policy_state": "ready",
        "transfer_policy_skip_count": 2,
        "transfer_policy_upload_count": 4,
        "transfer_policy_defer_count": 2,
        "capabilities": [parked_filter.CAPABILITY],
        "updated_at_utc": datetime.now(UTC).isoformat(),
    }
    monkeypatch.setattr(
        obd_transfer.adb,
        "shell",
        AsyncMock(return_value=obd_transfer._STATUS_FILE_PREFIX + json.dumps(body)),
    )
    clean = await obd_transfer.read_logger_status("unit", "/safe/status.json")
    for key, value in body.items():
        assert clean[key] == value


@pytest.mark.parametrize(
    "change", ["size", "mtime", "directory", "card", "fingerprint", "missing_peer"]
)
async def test_current_live_identity_failure_keeps_the_pair(policy, change):
    item = policy.remote[0]
    if change in {"size", "mtime", "directory"}:
        policy.remote[0] = RemoteFile(
            item.name,
            item.size + (change == "size"),
            item.mtime + (change == "mtime"),
            "/another/DCIM/Video" if change == "directory" else item.directory,
        )
    elif change == "card":
        movement.read_remote_card_id.return_value = "b" * 64
    elif change == "fingerprint":
        policy.fingerprints.return_value = {policy.doc["decisions"][0]["clip"]["clip_id"]}
    else:
        policy.remote = policy.remote[:1]
    assert await filtered(policy) == policy.remote
    assert policy.record.await_args.kwargs["excluded"] == []


@pytest.mark.parametrize("change", ["stale", "future", "truncated", "unsupported", "missing"])
async def test_stale_or_unavailable_manifest_never_uses_historical_authority(policy, change):
    if change in {"stale", "future"}:
        policy.doc["generated_at_utc"] = (
            datetime.now(UTC) + timedelta(seconds=-181 if change == "stale" else 31)
        ).isoformat()
    elif change == "truncated":
        policy.doc["truncated"] = True
    elif change == "unsupported":
        policy.logger = {"capabilities": [movement.CAPABILITY]}
    else:
        parked_filter._read_policy.return_value = None
    assert await filtered(policy) == policy.remote
    assert policy.record.await_args.kwargs["excluded"] == []


async def test_protected_peer_keeps_both_perspectives(policy):
    peer = policy.doc["decisions"][1]
    peer["clip"]["relative_path"] = peer["clip"]["relative_path"].replace("/Video/", "/LockVideo/")
    peer["clip"]["locked"] = True
    peer.update(decision="upload", reason="locked_clip")
    policy.remote = policy_inventory(policy.doc)
    assert await filtered(policy) == policy.remote


async def test_conflicting_gps_speed_keeps_both_lenses_even_without_paired_movement(policy):
    policy.doc["decisions"][0]["clip"]["evidence"]["max_gps_speed_m_s"] = 2.0
    assert await filtered(policy) == policy.remote


async def test_unknown_nearby_peer_cannot_be_hidden_by_a_valid_quiet_pair(policy):
    unknown = json.loads(json.dumps(policy.doc["decisions"][1]))
    unknown.update(decision="upload", reason="timing_unknown")
    unknown["clip"].update(
        clip_id=str(uuid.uuid4()),
        start_utc=None,
        end_utc=None,
        relative_path="DCIM/Video/20261021130030_camera_1.ts",
        local_start="2026-10-21T13:00:30",
        time_quality="unknown",
        analysis_status="unknown",
    )
    policy.doc["decisions"].append(unknown)
    policy.remote = policy_inventory(policy.doc)
    assert await filtered(policy) == policy.remote


async def test_audit_failure_copies_normally(policy):
    policy.record.side_effect = OSError("database unavailable")
    assert await filtered(policy) == policy.remote


async def test_bounded_read_failure_clears_current_exclusions_and_copies(policy, monkeypatch):
    async def blocked(*_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(parked_filter, "POLICY_READ_TIMEOUT_S", 0.01)
    monkeypatch.setattr(parked_filter, "_read_policy", blocked)
    assert await asyncio.wait_for(filtered(policy), 0.2) == policy.remote
    assert policy.record.await_args.kwargs["status"] == "invalid"
    assert policy.record.await_args.kwargs["excluded"] == []


async def test_on_camera_identity_check_returns_only_bounded_fingerprint_hashes(monkeypatch):
    doc = make_policy()
    replies = "\n".join(
        f"{i} {e['clip']['fingerprint']['digest']}" for i, e in enumerate(doc["decisions"])
    )
    call = AsyncMock(return_value=replies)
    monkeypatch.setattr(parked_filter.adb, "shell", call)
    checked = await parked_filter._verify_fingerprints("unit", SOURCE, doc["decisions"])
    assert checked == {entry["clip"]["clip_id"] for entry in doc["decisions"]}
    command = call.await_args.args[1]
    assert "sha256-sampled-v1\\000%d\\000" in command and "sha256sum" in command
    assert "head -c 16384" in command and "stat -c '%s %Y'" in command
    assert call.await_args.kwargs["timeout"] == 5.0


async def test_fingerprint_shell_matches_native_sample_algorithm_on_real_bytes(
    tmp_path, monkeypatch
):
    shell = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("sh")
    if shell is None or not Path(shell).is_file():
        pytest.skip("a local POSIX shell is unavailable")
    directory = tmp_path.as_posix()
    if os.name == "nt":
        directory = "/" + directory[0].lower() + directory[2:]
    doc = make_policy()
    for index, entry in enumerate(doc["decisions"]):
        clip = entry["clip"]
        body = bytes(range(256)) * (200 + index)
        path = tmp_path / clip["relative_path"].rsplit("/", 1)[-1]
        path.write_bytes(body)
        mtime = clip["mtime_ms"] // 1000
        os.utime(path, (mtime, mtime))
        clip["size_bytes"] = len(body)
        digest = hashlib.sha256(f"sha256-sampled-v1\0{len(body)}\0".encode())
        for offset in dict.fromkeys((0, (len(body) - 16384) // 2, len(body) - 16384)):
            digest.update(body[offset : offset + 16384])
        clip["fingerprint"]["digest"] = digest.hexdigest()

    async def local_shell(_address, command, **_kwargs):
        process = await asyncio.create_subprocess_exec(
            str(shell),
            "-c",
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await asyncio.wait_for(process.communicate(), 3)
        assert process.returncode == 0, error.decode()
        return output.decode()

    monkeypatch.setattr(parked_filter.adb, "shell", local_shell)
    assert await parked_filter._verify_fingerprints(
        "synthetic-local", directory, doc["decisions"]
    ) == {entry["clip"]["clip_id"] for entry in doc["decisions"]}


async def test_no_filtered_footage_is_transferred_scanned_or_counted_as_backlog(
    prepared_unit, monkeypatch
):
    state = prepared_unit
    state.options["skip_parked_footage"] = True
    filter_call = AsyncMock(return_value=parked_filter.FilterResult([]))
    monkeypatch.setattr(parked_filter, "filter_inventory", filter_call)
    result = await puller.run_pull(info=state.info)
    assert result.state is RunState.IDLE
    assert not state.path.exists() and not state.transferred.is_set()
    assert puller.get_status().snapshot()["backlog_files"] == 0
    filter_call.assert_awaited_once()


async def test_each_sweep_uses_a_fresh_camera_filter_evaluation(prepared_unit, monkeypatch):
    state = prepared_unit
    state.options.update(skip_parked_footage=True, sweep_passes=2)

    async def keep_first(_address, _status, remote, *_args, **_kwargs):
        return parked_filter.FilterResult(remote if not state.transferred.is_set() else [])

    filter_call = AsyncMock(side_effect=keep_first)
    monkeypatch.setattr(parked_filter, "filter_inventory", filter_call)
    assert (await puller.run_pull(info=state.info)).state is RunState.OK
    assert filter_call.await_count == 2


def test_actual_android_defer_fixture_has_fixed_deadline_and_observed_past_guard():
    raw = (Path(__file__).parent / "fixtures/transfer-policy-defer-v1-android.json").read_text()
    doc = parked_filter.validate_policy_manifest(raw)
    now = movement._timestamp(doc["generated_at_utc"])
    assert all(
        parked_filter.eligible_defer(e, now=now, native_remaining_s=1000) for e in doc["decisions"]
    )
    assert not any(parked_filter.eligible_skip(e) for e in doc["decisions"])


def test_visual_v1_proof_is_never_reused():
    doc = make_policy()
    doc["decisions"][0]["visual"]["algorithm_version"] = 1
    with pytest.raises(movement.ManifestError, match="unsupported_visual_algorithm"):
        parked_filter.validate_policy_manifest(json.dumps(doc))


def test_raw_changed_pixel_diagnostic_does_not_override_calibrated_motion_metrics():
    doc = make_policy()
    doc["decisions"][0]["visual"]["max_changed_pixels"] = 4000
    assert parked_filter.eligible_skip(doc["decisions"][0])


async def test_pending_pair_is_deferred_but_never_audited_as_excluded(policy):
    policy.doc = make_deferred_policy()
    policy.remote = policy_inventory(policy.doc)
    parked_filter._read_policy.return_value = policy.doc
    movement.read_remote_card_id.return_value = policy.doc["card_id"]
    policy.logger["transfer_policy_state"] = "scanning"
    assert await filtered(policy, native_remaining_s=1000) == []
    assert len(policy.result.deferred) == 2 and policy.result.skipped == []
    assert policy.record.await_args.kwargs["excluded"] == []
    policy.fingerprints.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "extended",
        "unknown_budget",
        "near_sleep",
        "unknown",
        "activity",
        "dark",
        "gap",
        "future_guard",
    ],
)
async def test_deferral_uncertainty_or_deadline_falls_back_to_upload(policy, change):
    policy.doc = make_deferred_policy()
    entry = policy.doc["decisions"][0]
    budget = 1000
    if change == "expired":
        entry["defer_until_utc"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "extended":
        entry["defer_until_utc"] = (
            movement._timestamp(entry["defer_until_utc"]) + timedelta(seconds=1)
        ).isoformat()
    elif change == "unknown_budget":
        budget = None
    elif change == "near_sleep":
        budget = 400
    elif change == "unknown":
        entry["clip"]["classification"] = "unknown"
    elif change == "activity":
        entry["visual"]["status"] = "activity"
    elif change == "dark":
        entry["visual"].update(status="incomplete", min_mean_luma=10)
    elif change == "gap":
        entry["guard"]["unknown_ms"] = 1
    else:
        entry["guard"]["end_utc"] = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    policy.remote = policy_inventory(policy.doc)
    parked_filter._read_policy.return_value = policy.doc
    movement.read_remote_card_id.return_value = policy.doc["card_id"]
    assert await filtered(policy, native_remaining_s=budget) == policy.remote
    assert policy.result.deferred == [] and policy.result.skipped == []


def test_fixed_defer_deadline_survives_repeated_evaluations_and_cannot_be_renewed():
    now = datetime.now(UTC).replace(microsecond=0)
    entry = make_deferred_policy(now=now)["decisions"][0]
    assert parked_filter.eligible_defer(entry, now=now, native_remaining_s=1000)
    assert parked_filter.eligible_defer(
        entry, now=now + timedelta(seconds=320), native_remaining_s=680
    )
    assert not parked_filter.eligible_defer(
        entry, now=now + timedelta(seconds=330), native_remaining_s=670
    )
    entry["defer_until_utc"] = (now + timedelta(seconds=360)).isoformat()
    assert not parked_filter.eligible_defer(
        entry, now=now + timedelta(seconds=320), native_remaining_s=680
    )


async def test_deferred_only_run_releases_without_radio_claim_and_keeps_pending_status(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, quiet_radios=True)
    remote = unit.inventory.return_value
    until = datetime.now(UTC) + timedelta(seconds=300)
    filter_result = parked_filter.FilterResult([], [(remote[0], until)])
    monkeypatch.setattr(parked_filter, "filter_inventory", AsyncMock(return_value=filter_result))
    begin = AsyncMock(side_effect=AssertionError("no radio claim for deferred-only footage"))
    monkeypatch.setattr(puller.radio_coordinator, "begin", begin)
    notify = Mock()
    from app.workers import scheduler

    monkeypatch.setattr(scheduler, "request_ingest_scan", notify)
    result = await puller.run_pull(info=unit.info)
    status = puller.get_status().snapshot()
    assert result.state is RunState.IDLE and not unit.transferred.is_set()
    assert not puller.get_status().running
    assert status["backlog_files"] == status["policy_deferred_files"] == 1
    assert status["backlog_bytes"] == status["policy_deferred_bytes"] == len(unit.payload)
    assert status["policy_deferred_until_utc"] == until.isoformat()
    assert not puller.close_sleep_window.await_args.kwargs["drained"]
    begin.assert_not_awaited()
    notify.assert_not_called()


def test_already_local_deferred_item_is_not_reported_pending(tmp_path):
    doc = make_deferred_policy()
    remote = policy_inventory(doc)
    (tmp_path / remote[0].name).write_bytes(b"x" * remote[0].size)
    filtered_result = parked_filter.FilterResult(
        [],
        [
            (r, movement._timestamp(e["defer_until_utc"]))
            for r, e in zip(remote, doc["decisions"], strict=True)
        ],
    )
    plan = puller.delta([], tmp_path, skip_active_s=0, camera="both")
    plan = puller._with_deferred_backlog(
        plan, filtered_result, tmp_path, set(), datetime.now(UTC).timestamp()
    )
    assert plan.backlog_files == plan.policy_deferred_files == 1
    assert plan.policy_deferred_bytes == remote[1].size


@pytest.mark.parametrize(
    "state", [RunState.IDLE, RunState.OK, RunState.PARTIAL, RunState.CANCELLED]
)
def test_bounded_defers_revisit_after_release_despite_visit_limit_but_respect_stop(
    state, monkeypatch
):
    from app.ingest.poller import IngestPoller

    poller = IngestPoller()
    poller._backups_this_visit = 100
    monkeypatch.setattr(poller, "_max_backups_per_visit", lambda: 1)
    status = SimpleNamespace(policy_deferred_files=2, state=state, since_finished=lambda: 29)
    assert not poller._should_drain_again(status)
    status.since_finished = lambda: 30
    assert poller._should_drain_again(status) is (state is not RunState.CANCELLED)


async def test_deferred_to_skip_sweep_clears_backlog_without_reclaiming_skip(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, sweep_passes=1, delete_after_verify=True)
    uploaded = unit.inventory.return_value[0]
    quiet = RemoteFile("20260812120100_camera_1.ts", len(unit.payload), 0)
    unit.inventory.return_value = [uploaded, quiet]
    deadline = datetime.now(UTC) + timedelta(seconds=300)
    monkeypatch.setattr(
        parked_filter,
        "filter_inventory",
        AsyncMock(
            side_effect=[
                parked_filter.FilterResult([uploaded], [(quiet, deadline)]),
                parked_filter.FilterResult([], skipped=[quiet]),
            ]
        ),
    )
    reclaim = AsyncMock()
    monkeypatch.setattr(puller, "_reclaim", reclaim)
    result = await puller.run_pull(info=unit.info)
    status = puller.get_status().snapshot()
    assert result.state is RunState.OK and result.files == 1
    assert status["backlog_files"] == status["policy_deferred_files"] == 0
    assert status["backlog_bytes"] == status["policy_deferred_bytes"] == 0
    assert status["policy_deferred_until_utc"] is None
    assert [r.name for c in reclaim.await_args_list for r in c.args[1]] == [uploaded.name]
    assert not (unit.path.parent / quiet.name).exists()


async def test_expired_defer_next_run_uploads_and_scans_once(prepared_unit, monkeypatch):
    unit = prepared_unit
    unit.options["skip_parked_footage"] = True
    remote = unit.inventory.return_value
    deadline = datetime.now(UTC) + timedelta(seconds=300)
    choices = AsyncMock(
        side_effect=[
            parked_filter.FilterResult([], [(remote[0], deadline)]),
            parked_filter.FilterResult(remote),
        ]
    )
    monkeypatch.setattr(parked_filter, "filter_inventory", choices)
    from app.workers import scheduler

    notify = Mock()
    monkeypatch.setattr(scheduler, "request_ingest_scan", notify)
    assert (await puller.run_pull(info=unit.info)).state is RunState.IDLE
    assert puller.get_status().snapshot()["backlog_files"] == 1
    result = await puller.run_pull(info=unit.info, continuation=True)
    assert result.state is RunState.OK and unit.path.read_bytes() == unit.payload
    assert puller.get_status().snapshot()["backlog_files"] == 0
    assert puller.get_status().snapshot()["policy_deferred_files"] == 0
    notify.assert_called_once_with()


async def test_persistent_puller_failure_never_retains_sql_parameter_dump(
    prepared_unit, monkeypatch
):
    unit = prepared_unit
    unit.settings.footage_dir.side_effect = RuntimeError(
        "database is locked [SQL: secret SQL] [parameters: sensitive]"
    )
    result = await puller.run_pull(info=unit.info)
    assert result.error == "RuntimeError: database is locked"
    assert "sensitive" not in str(puller.get_status().snapshot())


async def test_completed_quiet_lens_can_wait_for_pending_peer_only_with_full_quality_proof(policy):
    policy.doc = make_deferred_policy()
    first = policy.doc["decisions"][0]
    first["visual"] = make_policy()["decisions"][0]["visual"]
    first["clip"]["duration_ms"] = 60000
    first["visual"]["analysis_completed_at_utc"] = (
        datetime.now(UTC) - timedelta(seconds=5)
    ).isoformat()
    policy.remote = policy_inventory(policy.doc)
    parked_filter._read_policy.return_value = policy.doc
    movement.read_remote_card_id.return_value = policy.doc["card_id"]
    assert await filtered(policy, native_remaining_s=1000) == []
    assert len(policy.result.deferred) == 2
    first["visual"]["min_mean_luma"] = 10
    assert await filtered(policy, native_remaining_s=1000) == policy.remote
    assert not policy.result.deferred


async def test_hash_budget_overflow_never_omits_an_unverified_half_pair(policy):
    actual = parked_filter._verify_fingerprints

    # The real verifier honors the selected list; returning one exact hash proves
    # only one lens. A second pairing pass must restore both to the upload plan.
    async def verified_only_requested(_address, _source, selected):
        return {entry["clip"]["clip_id"] for entry in selected}

    parked_filter._verify_fingerprints = verified_only_requested
    try:
        result = await parked_filter.filter_inventory(
            "unit:5555",
            "/obd/status.json",
            policy.remote,
            SOURCE,
            policy.logger,
            enabled=True,
            fingerprint_budget=1,
        )
    finally:
        parked_filter._verify_fingerprints = actual
    assert result.files == policy.remote and result.skipped == []
    assert result.fingerprint_checks == 1
    assert policy.record.await_args.kwargs["excluded"] == []


async def test_sweeps_share_a_single_bounded_fingerprint_budget(prepared_unit, monkeypatch):
    unit = prepared_unit
    unit.options.update(skip_parked_footage=True, sweep_passes=1)

    async def keep(_address, _path, remote, *_args, **kwargs):
        return parked_filter.FilterResult(
            remote, fingerprint_checks=min(64, kwargs["fingerprint_budget"])
        )

    calls = AsyncMock(side_effect=keep)
    monkeypatch.setattr(parked_filter, "filter_inventory", calls)
    assert (await puller.run_pull(info=unit.info)).state is RunState.OK
    assert [c.kwargs["fingerprint_budget"] for c in calls.await_args_list] == [64, 0]


def test_selected_lens_defer_status_keeps_full_backlog_without_mixed_deadlines(tmp_path):
    doc = make_deferred_policy()
    remote = policy_inventory(doc)
    held = parked_filter.FilterResult(
        [],
        [
            (r, movement._timestamp(e["defer_until_utc"]))
            for r, e in zip(remote, doc["decisions"], strict=True)
        ],
    )
    plan = puller._with_deferred_backlog(
        puller.delta([], tmp_path, skip_active_s=0, camera="camera_0"),
        held,
        tmp_path,
        set(),
        datetime.now(UTC).timestamp(),
        camera="camera_0",
    )
    assert plan.backlog_files == len(held.deferred) == 2
    assert plan.policy_deferred_files == 1
    assert plan.policy_deferred_bytes == remote[0].size
    assert plan.policy_deferred_until_utc == doc["decisions"][0]["defer_until_utc"]
