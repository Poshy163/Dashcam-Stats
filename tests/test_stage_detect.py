"""The detection stage, driven end to end over a fake decoder and a fake detector.

This file exists because of a specific escape. The stage was changed to close its decoder
deterministically -- ``async with contextlib.aclosing(iter_frames(...)) as frames:`` -- and
``frames`` was already the name of the counter that ``analyse_frame`` increments through
``nonlocal``. Binding the generator to that name replaced the counter with the generator
for the whole scope, so the first frame raised

    TypeError: unsupported operand type(s) for +=: 'async_generator' and 'int'

and detection failed for every recording in the library. Six hundred and sixty-seven tests
passed, because not one of them ran ``stage_detect``: the decoder was always mocked at
``_decode_frames`` or below, and the stage itself was only ever referenced by name.

So this drives the real stage body. It needs no ffmpeg, no model and no GPU.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.db.models import Recording, StageState
from app.pipeline import stages


class FakeDetector:
    """Stands in for the shared RF-DETR detector."""

    device = "CPU"
    available = True

    def __init__(self) -> None:
        self.seen = 0

    async def detect(self, frame, **kwargs):
        self.seen += 1
        return []


@pytest.fixture
def decoded_clip(monkeypatch):
    """A recording, a detector and eight decoded frames, with nothing real behind them."""
    detector = FakeDetector()

    async def shared_detector(name):
        return detector

    async def iter_frames(path, **kwargs):
        on_decoder = kwargs.get("on_decoder")
        if on_decoder:
            on_decoder("software")
        for index in range(8):
            yield float(index) / 4.0, np.zeros((16, 16, 3), dtype=np.uint8)

    async def resolve(rel_path):
        return rel_path

    monkeypatch.setattr(stages, "_shared_detector", shared_detector)
    monkeypatch.setattr(stages, "iter_frames", iter_frames)
    monkeypatch.setattr(stages.asyncio, "to_thread", lambda fn, *a, **k: _immediate(fn, *a, **k))
    return detector


async def _immediate(fn, *args, **kwargs):
    return fn(*args, **kwargs)


class TestTheDetectionStageActuallyRuns:
    async def test_every_decoded_frame_reaches_the_detector(self, db_session, decoded_clip):
        """The regression: this raised TypeError on the first frame.

        Asserting the count rather than merely "it did not raise", because the counter is
        exactly what got shadowed -- a stage that silently analysed nothing would still
        report success.
        """
        from app.db.session import session_scope

        async with session_scope() as session:
            recording = Recording(
                rel_path="20260812120000_camera_0.ts",
                filename="20260812120000_camera_0.ts",
                width=16,
                height=16,
                duration_s=2.0,
                video_codec="h264",
            )
            session.add(recording)
            await session.flush()

            result = await stages.stage_detect(session, recording)

        assert result.ok
        assert decoded_clip.seen == 8, "frames were decoded but never analysed"
        assert result.stats["frames_analysed"] == 8
        assert recording.detection_state is StageState.DONE

    async def test_the_decoder_is_closed_when_the_stage_ends(self, db_session, monkeypatch):
        """The reason the `aclosing` is there at all: the ffmpeg child must not outlive it.

        An abandoned decoder keeps its process -- and the Intel media slot behind it --
        alive until the event loop finalises the generator, which on this hardware means
        beside an OpenVINO request.
        """
        from app.db.session import session_scope

        closed = {"value": False}

        async def shared_detector(name):
            return FakeDetector()

        async def iter_frames(path, **kwargs):
            try:
                for index in range(4):
                    yield float(index), np.zeros((16, 16, 3), dtype=np.uint8)
            finally:
                closed["value"] = True

        monkeypatch.setattr(stages, "_shared_detector", shared_detector)
        monkeypatch.setattr(stages, "iter_frames", iter_frames)
        monkeypatch.setattr(
            stages.asyncio, "to_thread", lambda fn, *a, **k: _immediate(fn, *a, **k)
        )

        async with session_scope() as session:
            recording = Recording(
                rel_path="clip.ts", filename="clip.ts", width=16, height=16, duration_s=1.0
            )
            session.add(recording)
            await session.flush()
            await stages.stage_detect(session, recording)

        assert closed["value"], "the decoder was left for the garbage collector to finalise"
