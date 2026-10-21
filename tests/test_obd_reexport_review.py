"""Independent regressions for re-export durability and deletion authority."""

from __future__ import annotations

import asyncio
import threading

import pytest
from test_obd_reexports import (
    _assert_original_history,
    _sync,
)
from test_obd_reexports import (
    reexport_case as reexport_case,
)

from app.db.models import OBDBundle
from app.db.session import session_scope
from app.ingest import obd_transfer


@pytest.mark.parametrize(
    ("field", "value"),
    [("schema_version", 2), ("size_bytes", 1), ("state", "failed")],
)
async def test_preserved_variant_requires_current_canonical_database_authority(
    reexport_case, field, value
):
    case = reexport_case
    assert (await _sync(case)).failed == 0
    async with session_scope() as session:
        row = await session.get(OBDBundle, case.row_id)
        setattr(row, field, value)
    assert not await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.new.bundle_sha256
    )


async def test_reexport_symlink_ancestor_never_grants_deletion_authority(reexport_case, tmp_path):
    case = reexport_case
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = case.config.obd_verified_dir / "reexports"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable to this test user")
    result = await _sync(case)
    assert not result.complete and result.failed == 1
    assert not case.receipts and not case.deleted
    assert not list(outside.iterdir())
    assert not await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.new.bundle_sha256
    )
    await _assert_original_history(case)


async def test_cancellation_joins_variant_publish_before_returning_without_remote_mutation(
    reexport_case, monkeypatch
):
    case = reexport_case
    published = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    preserve = obd_transfer._preserve_equivalent_reexport

    def blocked_publish(*args, **kwargs):
        result = preserve(*args, **kwargs)
        loop.call_soon_threadsafe(published.set)
        assert release.wait(10), "test did not release durable publish worker"
        return result

    monkeypatch.setattr(obd_transfer, "_preserve_equivalent_reexport", blocked_publish)
    task = asyncio.create_task(_sync(case))
    try:
        await asyncio.wait_for(published.wait(), timeout=10)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cancellation must retain ownership of the disk worker"
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    assert not case.receipts and not case.deleted
    assert await obd_transfer.verified_bundle_matches(
        case.canonical.name, case.new.bundle_sha256
    ), "the exact durable variant is retained even when cancellation prevents ACK"
    await _assert_original_history(case)
