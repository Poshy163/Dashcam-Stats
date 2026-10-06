"""Committing and reclaiming behind the transfer rather than inside it.

The bulk socket and the ADB control channel are independent, but the commit and the card
delete used to run between chunks with the stream stopped -- so the one thing guaranteed
idle during them was the link. On average that is cheap (a rename, and one ADB round trip
measured at ~185 ms against a chunk that takes twelve seconds to move); occasionally it is
not, and this application has recorded twenty-second ADB timeouts.

What must not change is the ordering that makes it safe: a card delete still happens only
after the commit for those exact files returned them, chunks are still handled one at a
time in arrival order, and the run still may not decide what is outstanding until the
worker has finished saying so.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from app.ingest.puller import COMMIT_QUEUE_DEPTH, _clean, _CommitPipeline, commit


def _chunk(name: str) -> list[str]:
    """Stands in for a chunk of RemoteFile; the pipeline never looks inside one."""
    return [name]


async def test_the_transfer_does_not_wait_for_the_commit():
    """The whole point: submit returns while the handler is still working."""
    released = asyncio.Event()
    started = asyncio.Event()

    async def handler(chunk):
        started.set()
        await released.wait()

    pipeline = _CommitPipeline(handler, depth=2)
    pipeline.start()

    await asyncio.wait_for(pipeline.submit(_chunk("a")), timeout=2)
    await asyncio.wait_for(started.wait(), timeout=2)
    # Handler is still in flight, and the transfer has already moved on.
    assert not released.is_set()

    released.set()
    await pipeline.close()


async def test_chunks_are_handled_one_at_a_time_in_order():
    """Delete-after-commit only stays safe while the order is the arrival order."""
    seen: list[str] = []
    concurrent = 0
    peak = 0

    async def handler(chunk):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.01)
        seen.append(chunk[0])
        concurrent -= 1

    pipeline = _CommitPipeline(handler, depth=4)
    pipeline.start()
    for name in "abcde":
        await pipeline.submit(_chunk(name))
    await pipeline.close()

    assert seen == list("abcde")
    assert peak == 1


async def test_the_queue_is_backpressure_not_a_buffer():
    """Each queued chunk is recordings sitting in staging, so the depth has to bind."""
    release = asyncio.Event()

    async def handler(chunk):
        await release.wait()

    pipeline = _CommitPipeline(handler, depth=1)
    pipeline.start()

    # One in the worker, one in the queue, and the next submit has nowhere to go.
    await asyncio.wait_for(pipeline.submit(_chunk("a")), timeout=2)
    await asyncio.wait_for(pipeline.submit(_chunk("b")), timeout=2)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(pipeline.submit(_chunk("c")), timeout=0.2)

    release.set()
    await pipeline.close()


async def test_close_drains_everything_before_it_returns():
    """The run reads its committed list the moment this returns."""
    done: list[str] = []

    async def handler(chunk):
        await asyncio.sleep(0.01)
        done.append(chunk[0])

    pipeline = _CommitPipeline(handler, depth=COMMIT_QUEUE_DEPTH)
    pipeline.start()
    for name in "abcd":
        await pipeline.submit(_chunk(name))
    await pipeline.close()

    assert done == list("abcd")


async def test_a_failure_surfaces_and_stops_further_work():
    """Nothing should still be erasing a card after a commit failed."""
    handled: list[str] = []

    async def handler(chunk):
        handled.append(chunk[0])
        if chunk[0] == "b":
            raise RuntimeError("commit failed")

    pipeline = _CommitPipeline(handler, depth=4)
    pipeline.start()
    for name in "abc":
        await pipeline.submit(_chunk(name))

    with pytest.raises(RuntimeError, match="commit failed"):
        await pipeline.close()

    # "c" was queued behind the failure and deliberately never ran.
    assert handled == ["a", "b"]


async def test_a_failure_does_not_deadlock_the_drain():
    """The queue keeps being consumed after an error, or close() would wait forever.

    Submitting past a failure raises -- that is how the transfer finds out -- so the
    submits here tolerate it. What is being proved is that `close` still returns.
    """
    attempts = 0

    async def handler(chunk):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("always fails")

    pipeline = _CommitPipeline(handler, depth=1)
    pipeline.start()
    for name in "abcdef":
        with contextlib.suppress(RuntimeError):
            await asyncio.wait_for(pipeline.submit(_chunk(name)), timeout=2)

    await asyncio.wait_for(pipeline.close(discard=True), timeout=5)
    # Latched after the first: telling the transfer about the failure must not restart the
    # work that failed. Anything else would keep erasing a card after a commit broke.
    assert attempts == 1


async def test_a_failure_reaches_the_transfer_at_the_next_chunk():
    """So a run stops moving bytes rather than piling onto a broken commit."""

    async def handler(chunk):
        raise RuntimeError("commit failed")

    pipeline = _CommitPipeline(handler, depth=2)
    pipeline.start()
    await pipeline.submit(_chunk("a"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        try:
            await pipeline.submit(_chunk("b"))
        except RuntimeError as exc:
            assert "commit failed" in str(exc)
            break
    else:
        pytest.fail("the failure never reached the transfer")

    await pipeline.close(discard=True)


async def test_discard_abandons_the_queue_without_raising():
    """Drop queued work while preserving ownership of the active handler."""
    release = asyncio.Event()
    started = asyncio.Event()
    handled: list[str] = []

    async def handler(chunk):
        started.set()
        await release.wait()
        handled.append(chunk[0])

    pipeline = _CommitPipeline(handler, depth=3)
    pipeline.start()
    await pipeline.submit(_chunk("a"))
    await asyncio.wait_for(started.wait(), timeout=1)
    await pipeline.submit(_chunk("b"))

    closing = asyncio.create_task(pipeline.close(discard=True))
    try:
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
        await asyncio.wait_for(closing, timeout=2)
    assert handled == ["a"]


@pytest.mark.parametrize("cancellations", [1, 2])
async def test_cancelled_discard_waits_for_active_filesystem_thread(tmp_path, cancellations):
    """Caller cancellation cannot release shared staging while a commit still runs."""
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    handled = []
    target = tmp_path / "committed.ts"

    def slow_commit():
        started.set()
        assert release.wait(3), "test did not release the filesystem worker"
        target.write_bytes(b"complete")
        finished.set()

    async def handler(chunk):
        await asyncio.to_thread(slow_commit)
        handled.append(chunk[0])

    pipeline = _CommitPipeline(handler, depth=2)
    pipeline.start()
    await pipeline.submit(_chunk("active"))
    assert await asyncio.to_thread(started.wait, 1)
    await pipeline.submit(_chunk("queued"))
    closing = asyncio.create_task(pipeline.close(discard=True))
    try:
        await asyncio.sleep(0)
        for _ in range(cancellations):
            closing.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not closing.done(), "cleanup escaped before the filesystem worker settled"
            assert not finished.is_set()
            assert not target.exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert await asyncio.to_thread(finished.wait, 1)
    assert handled == ["active"], "queued work must remain discarded"
    assert target.read_bytes() == b"complete"


async def test_discard_swallows_a_failure_that_was_already_recorded():
    """Closing down a failed window must not raise over the error that shut it."""

    async def handler(chunk):
        raise RuntimeError("commit failed")

    pipeline = _CommitPipeline(handler, depth=2)
    pipeline.start()
    await pipeline.submit(_chunk("a"))
    await asyncio.sleep(0.05)
    await asyncio.wait_for(pipeline.close(discard=True), timeout=2)


async def test_without_a_worker_it_runs_inline():
    """Exactly the old behaviour, for any caller that never starts the pipeline."""
    handled: list[str] = []

    async def handler(chunk):
        handled.append(chunk[0])

    pipeline = _CommitPipeline(handler)
    await pipeline.submit(_chunk("a"))

    assert handled == ["a"]
    # Closing one that never started is a no-op rather than an error.
    await pipeline.close()


async def test_closing_twice_is_harmless():
    async def handler(chunk):
        return None

    pipeline = _CommitPipeline(handler, depth=2)
    pipeline.start()
    await pipeline.submit(_chunk("a"))
    await pipeline.close()
    await pipeline.close()


@pytest.mark.parametrize("next_chunk_bytes", [40, 100])
async def test_commit_preserves_the_next_chunk_already_in_shared_staging(
    tmp_path, next_chunk_bytes
):
    """Exercise real files while a completed chunk overlaps the next transfer."""
    footage = tmp_path / "footage"
    staging = footage / ".ingest_staging"
    staging.mkdir(parents=True)
    (staging / "a.ts").write_bytes(b"a" * 100)
    # Run-start cleanup owns unrelated files. Chunk commits cannot tell these apart
    # from the next receive's files, so neither may be deleted during the transfer.
    (staging / "old.ts").write_bytes(b"old")
    first_started = asyncio.Event()
    first_released = asyncio.Event()
    first_finished = asyncio.Event()
    committed: list[str] = []

    async def handler(chunk):
        name = chunk[0]
        if name == "a.ts":
            first_started.set()
            await first_released.wait()
        committed.extend(await asyncio.to_thread(commit, staging, footage, {name: 100}))
        if name == "a.ts":
            first_finished.set()

    pipeline = _CommitPipeline(handler, depth=2)
    pipeline.start()
    try:
        await pipeline.submit(_chunk("a.ts"))
        await asyncio.wait_for(first_started.wait(), timeout=2)
        next_file = staging / "b.ts"
        next_file.write_bytes(b"b" * next_chunk_bytes)
        first_released.set()
        await asyncio.wait_for(first_finished.wait(), timeout=2)
        assert next_file.read_bytes() == b"b" * next_chunk_bytes
        assert (staging / "old.ts").read_bytes() == b"old"
        with next_file.open("ab") as handle:
            handle.write(b"b" * (100 - next_chunk_bytes))
        await pipeline.submit(_chunk("b.ts"))
        await pipeline.close()
    finally:
        await pipeline.close(discard=True)

    assert committed == ["a.ts", "b.ts"]
    assert (footage / "a.ts").read_bytes() == b"a" * 100
    assert (footage / "b.ts").read_bytes() == b"b" * 100
    assert not (footage / "old.ts").exists()
    _clean(staging)
    assert list(staging.iterdir()) == []
