"""Full-content reclaim is optional; uncertainty keeps a durable server copy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from app.ingest import adb
from app.ingest import duplicate_cleanup as cleanup
from app.ingest.models import RemoteFile, UnitInfo


@pytest.fixture
def case(tmp_path, monkeypatch):
    data = b"header" + bytes(range(256)) * 2048 + b"trailer"
    name = "20261026120000_camera_0.ts"
    path = tmp_path / name
    path.write_bytes(data)
    item = RemoteFile(name=name, size=len(data), mtime=100, directory="/card/DCIM/Video")
    info = UnitInfo(address="192.0.2.1:5555", source=item.directory)
    seen = {"verify": [], "delete": [], "ignition": []}

    async def ignition(address, *, timeout):
        seen["ignition"].append(timeout)
        return "off"

    async def verify(address, source, remote, digest, *, timeout):
        seen["verify"].append(digest)
        assert digest == hashlib.sha256(data).hexdigest()
        return adb.VerifiedRemoteFile(
            address=address,
            source=source,
            name=remote.name,
            size=remote.size,
            mtime=remote.mtime,
            identity="mock-pinned-identity",
            sha256=digest,
        )

    async def delete(address, source, remote, proof, *, timeout):
        seen["delete"].append(remote.name)
        return True

    monkeypatch.setattr(adb, "ignition_state", ignition)
    monkeypatch.setattr(adb, "verify_remote_file", verify)
    monkeypatch.setattr(adb, "delete_verified_remote_file", delete)
    return tmp_path, path, data, item, info, seen


async def run(case, **kwargs):
    root, _path, _data, item, info, _seen = case
    return await cleanup.cleanup_verified_duplicates(
        info, root, [item], native_remaining=kwargs.pop("native_remaining", lambda: 120.0), **kwargs
    )


def copies(case):
    return list((case[0] / cleanup.PRESERVATION_DIRNAME).glob("*.verified"))


async def test_full_hash_proof_and_durable_independent_copy_precede_deletion(case, monkeypatch):
    root, path, data, item, _info, seen = case

    async def delete(*args, **kwargs):
        preserved = copies(case)
        assert len(preserved) == 1
        assert preserved[0].read_bytes() == data
        assert preserved[0].stat().st_ino != path.stat().st_ino
        receipt = json.loads(preserved[0].with_suffix(".json").read_text())
        assert receipt["phase"] == "delete_requested"
        assert receipt["recording_name"] == item.name
        seen["delete"].append(item.name)
        return True

    monkeypatch.setattr(adb, "delete_verified_remote_file", delete)
    result = await run(case)
    assert (result.attempted_files, result.verified_files, result.deleted_files) == (1, 1, 1)
    assert result.deleted_bytes == len(data)
    assert len(seen["ignition"]) == 2
    assert path.read_bytes() == data
    assert not copies(case)
    assert not list((root / cleanup.PRESERVATION_DIRNAME).glob("*.json"))


async def test_same_size_remote_mismatch_is_never_deleted(case, monkeypatch):
    async def mismatch(*args, **kwargs):
        return None

    monkeypatch.setattr(adb, "verify_remote_file", mismatch)
    result = await run(case)
    assert result.stop_reason == "remote_not_verified"
    assert not case[5]["delete"]
    assert not copies(case)


@pytest.mark.parametrize("change", ["unlink", "replace", "truncate", "mutate"])
async def test_original_change_during_remote_hash_aborts_before_delete(case, monkeypatch, change):
    original_verify = adb.verify_remote_file
    path, data = case[1:3]

    async def verify(*args, **kwargs):
        proof = await original_verify(*args, **kwargs)
        if change == "unlink":
            path.unlink()
        elif change == "replace":
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(data)
            replacement.replace(path)
        elif change == "truncate":
            path.write_bytes(data[:-1])
        else:
            before = path.stat()
            with path.open("r+b") as stream:
                stream.seek(len(data) // 2)
                stream.write(b"changed")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return proof

    monkeypatch.setattr(adb, "verify_remote_file", verify)
    result = await run(case)
    assert result.stop_reason is None
    assert result.skipped_files == 1
    assert not case[5]["delete"]
    assert not copies(case)


@pytest.mark.parametrize("change", ["unlink", "mutate", "replace"])
async def test_retention_or_mutation_during_remote_delete_keeps_durable_source(
    case, monkeypatch, change
):
    path, data = case[1:3]

    async def delete(*args, **kwargs):
        if change == "unlink":
            path.unlink()
        elif change == "mutate":
            path.write_bytes(b"x" * len(data))
        else:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(b"y" * len(data))
            replacement.replace(path)
        return True

    monkeypatch.setattr(adb, "delete_verified_remote_file", delete)
    result = await run(case)
    assert result.deleted_files == 1
    assert result.retained_preservations == 1
    assert result.stop_reason == "canonical_changed_after_delete"
    assert copies(case)[0].read_bytes() == data
    receipt = json.loads(copies(case)[0].with_suffix(".json").read_text())
    assert receipt["phase"] == "retained"
    assert "Restore or reconcile" in receipt["recovery"]


@pytest.mark.parametrize("outcome", ["false", "timeout", "disconnect"])
async def test_uncertain_delete_keeps_copy_and_sanitized_receipt(case, monkeypatch, outcome):
    async def delete(*args, **kwargs):
        if outcome == "timeout":
            raise TimeoutError("secret diagnostic")
        if outcome == "disconnect":
            raise adb.AdbError("secret diagnostic")
        return False

    monkeypatch.setattr(adb, "delete_verified_remote_file", delete)
    result = await run(case)
    assert result.retained_preservations == 1
    assert copies(case)[0].read_bytes() == case[2]
    assert "secret diagnostic" not in copies(case)[0].with_suffix(".json").read_text()


async def test_cancellation_during_delete_keeps_prepared_copy(case, monkeypatch):
    entered = asyncio.Event()

    async def delete(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adb, "delete_verified_remote_file", delete)
    task = asyncio.create_task(run(case))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert copies(case)[0].read_bytes() == case[2]
    assert (
        json.loads(copies(case)[0].with_suffix(".json").read_text())["phase"] == "delete_requested"
    )


@pytest.mark.parametrize("remaining", [None, 0, 15, float("nan"), float("inf")])
async def test_unknown_or_short_native_window_does_no_local_or_remote_work(case, remaining):
    result = await run(case, native_remaining=lambda: remaining)
    assert result.attempted_files == 0
    assert result.stop_reason == "native_window_unavailable_or_short"
    assert not copies(case)
    assert not case[5]["verify"]


@pytest.mark.parametrize("state", ["on", "unknown"])
async def test_ignition_is_required_off_even_when_caller_admitted_run(case, monkeypatch, state):
    async def ignition(*args, **kwargs):
        return state

    monkeypatch.setattr(adb, "ignition_state", ignition)
    result = await run(case)
    assert result.stop_reason == "ignition_not_off"
    assert not case[5]["verify"]
    assert not copies(case)


async def test_second_ignition_check_catches_power_returning_before_delete(case, monkeypatch):
    states = iter(["off", "on"])

    async def ignition(*args, **kwargs):
        return next(states)

    monkeypatch.setattr(adb, "ignition_state", ignition)
    result = await run(case)
    assert result.verified_files == 1
    assert result.stop_reason == "ignition_not_off"
    assert not case[5]["delete"]
    assert not copies(case)


@pytest.mark.parametrize("gate", ["cancelled", "short_window"])
async def test_rechecks_cancellation_and_native_deadline_before_deletion(case, monkeypatch, gate):
    verify = adb.verify_remote_file
    changed = False

    async def change_gate(*args, **kwargs):
        nonlocal changed
        proof = await verify(*args, **kwargs)
        changed = True
        return proof

    monkeypatch.setattr(adb, "verify_remote_file", change_gate)
    result = await run(
        case,
        cancelled=lambda: changed and gate == "cancelled",
        native_remaining=lambda: 14.0 if changed and gate == "short_window" else 120.0,
    )
    assert result.stop_reason == (
        "cancelled" if gate == "cancelled" else "native_window_unavailable_or_short"
    )
    assert not case[5]["delete"]
    assert not copies(case)


async def test_duplicate_basename_collision_is_not_a_reclaim_candidate(case):
    root, _path, _data, item, info, seen = case
    collision = RemoteFile(item.name, item.size, item.mtime, "/card/DCIM/LockVideo")
    result = await cleanup.cleanup_verified_duplicates(
        info, root, [item, collision], native_remaining=lambda: 120
    )
    assert result.skipped_files == 2
    assert result.attempted_files == 0
    assert not seen["delete"]


async def test_file_and_byte_budget_bound_optional_work(case, monkeypatch):
    root, _path, data, item, info, _seen = case
    candidates = [item]
    for index in range(1, 6):
        name = f"20261026120{index}00_camera_0.ts"
        (root / name).write_bytes(data)
        candidates.append(RemoteFile(name, len(data), 100, item.directory))
    result = await cleanup.cleanup_verified_duplicates(
        info, root, candidates, native_remaining=lambda: 120
    )
    assert result.deleted_files == 4
    assert result.stop_reason == "file_budget"
    monkeypatch.setattr(cleanup, "MAX_BYTES", len(data) - 1)
    result = await run(case)
    assert result.attempted_files == 0
    assert result.skipped_files == 1
    assert result.stop_reason is None


async def test_blocked_local_worker_times_out_and_owns_slot_until_it_settles(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    prepare = cleanup._prepare_preservation

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return prepare(*args)

    monkeypatch.setattr(cleanup, "_prepare_preservation", blocked)
    try:
        result = await run(case, budget_s=0.05)
        assert entered.is_set()
        assert result.stop_reason == "operation_timeout"
        assert (await run(case)).stop_reason == "local_io_pending"
        assert not case[5]["verify"]
    finally:
        release.set()
        async with asyncio.timeout(3):
            while cleanup._local_io_slot.locked():
                await asyncio.sleep(0.01)
    assert not copies(case)


async def test_cancelled_async_worker_wrapper_does_not_release_a_live_thread_slot(case):
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert release.wait(3)

    owner = asyncio.create_task(cleanup._local_io(blocked, 0.02))
    try:
        with pytest.raises(TimeoutError):
            await owner
        assert entered.is_set()
        # Simulate shutdown cancelling the to_thread asyncio wrapper; its actual worker
        # must remain the sole owner of the process-local optional IO slot.
        wrappers = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and "to_thread" in repr(task.get_coro())
        ]
        assert wrappers
        for wrapper in wrappers:
            wrapper.cancel()
        await asyncio.sleep(0)
        assert cleanup._local_io_slot.locked()
    finally:
        release.set()
        async with asyncio.timeout(3):
            while cleanup._local_io_slot.locked():
                await asyncio.sleep(0.01)


async def test_competing_preservation_publication_is_never_removed(case, monkeypatch):
    competitor = b"previous verified recovery copy"
    target = None

    def race(source, destination):
        nonlocal target
        target = Path(destination)
        target.write_bytes(competitor)
        raise FileExistsError("concurrent preservation")

    monkeypatch.setattr(os, "link", race)
    result = await run(case)
    assert result.deleted_files == 0
    assert target.read_bytes() == competitor
    assert not case[5]["verify"]


async def test_directory_publication_fsync_failure_aborts_before_remote_work(case, monkeypatch):
    synced = []

    def sync(path):
        synced.append(path)
        raise OSError("disk unavailable")

    monkeypatch.setattr(cleanup, "_sync_directory", sync)
    result = await run(case)
    assert synced == [case[0]]
    assert result.deleted_files == 0
    assert not case[5]["verify"]


async def test_retry_after_directory_fsync_failure_repeats_parent_durability_check(
    case, monkeypatch
):
    sync = cleanup._sync_directory
    roots = []

    def fail_once(path):
        if path == case[0]:
            roots.append(path)
            if len(roots) == 1:
                raise OSError("first parent flush failed")
        sync(path)

    monkeypatch.setattr(cleanup, "_sync_directory", fail_once)
    assert (await run(case)).deleted_files == 0
    assert (await run(case)).deleted_files == 1
    assert roots == [case[0], case[0]]


async def test_removing_private_snapshot_during_hash_never_authorizes_delete(case, monkeypatch):
    verify = adb.verify_remote_file

    async def remove(*args, **kwargs):
        proof = await verify(*args, **kwargs)
        copies(case)[0].unlink()
        return proof

    monkeypatch.setattr(adb, "verify_remote_file", remove)
    result = await run(case)
    assert result.stop_reason == "preservation_changed"
    assert not case[5]["delete"]


async def test_existing_unresolved_content_key_is_preserved_without_a_second_delete(
    case, monkeypatch
):
    async def uncertain(*args, **kwargs):
        return False

    monkeypatch.setattr(adb, "delete_verified_remote_file", uncertain)
    assert (await run(case)).retained_preservations == 1
    preserved = copies(case)[0]
    before = preserved.read_bytes()
    result = await run(case)
    assert result.stop_reason is None
    assert result.skipped_files == 1
    assert copies(case) == [preserved]
    assert preserved.read_bytes() == before


async def test_oversized_leading_candidate_does_not_starve_a_later_valid_copy(case):
    root, _path, data, item, info, _seen = case
    oversized = RemoteFile("20261025120000_camera_0.ts", cleanup.MAX_BYTES + 1, 99, item.directory)
    result = await cleanup.cleanup_verified_duplicates(
        info, root, [oversized, item], native_remaining=lambda: 120
    )
    assert result.deleted_files == 1
    assert result.deleted_bytes == len(data)
    assert result.attempted_files == 1
    assert result.skipped_files == 1


async def test_unresolved_leading_preservation_does_not_starve_a_later_valid_copy(
    case, monkeypatch
):
    root, _path, data, item, info, _seen = case

    async def uncertain(*args, **kwargs):
        return False

    monkeypatch.setattr(adb, "delete_verified_remote_file", uncertain)
    assert (await run(case)).retained_preservations == 1
    preserved = copies(case)[0]
    next_name = "20261027120000_camera_0.ts"
    (root / next_name).write_bytes(data)
    next_item = RemoteFile(next_name, len(data), 101, item.directory)

    async def delete(*args, **kwargs):
        assert args[2] == next_item
        return True

    monkeypatch.setattr(adb, "delete_verified_remote_file", delete)
    result = await cleanup.cleanup_verified_duplicates(
        info, root, [item, next_item], native_remaining=lambda: 120
    )
    assert result.deleted_files == 1
    assert result.attempted_files == 2
    assert result.skipped_files == 1
    assert copies(case) == [preserved]
    assert preserved.read_bytes() == data


async def test_missing_leading_local_candidate_does_not_starve_a_later_valid_copy(case):
    root, _path, _data, item, info, _seen = case
    missing = RemoteFile("20261025120000_camera_0.ts", item.size, 99, item.directory)
    result = await cleanup.cleanup_verified_duplicates(
        info, root, [missing, item], native_remaining=lambda: 120
    )
    assert result.deleted_files == 1
    assert result.attempted_files == 2
    assert result.skipped_files == 1


async def test_symlink_original_is_never_read_or_deleted(case, tmp_path):
    path, data = case[1:3]
    outside = tmp_path / "outside"
    outside.write_bytes(data)
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    result = await run(case)
    assert result.deleted_files == 0
    assert outside.read_bytes() == data
    assert not case[5]["verify"]


async def test_replaced_private_parent_does_not_write_receipts_outside_root(case, monkeypatch):
    root = case[0]
    outside = root / "outside"
    outside.mkdir()
    probe = root / "symlink-probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except OSError:
        pytest.skip("symlink creation unavailable")
    verify = adb.verify_remote_file

    async def replace_parent(*args, **kwargs):
        proof = await verify(*args, **kwargs)
        private = root / cleanup.PRESERVATION_DIRNAME
        private.rename(root / ".original-preservation")
        private.symlink_to(outside, target_is_directory=True)
        return proof

    monkeypatch.setattr(adb, "verify_remote_file", replace_parent)
    result = await run(case)
    assert result.deleted_files == 0
    assert not case[5]["delete"]
    assert not list(outside.iterdir())
    assert next((root / ".original-preservation").glob("*.verified")).read_bytes() == case[2]
