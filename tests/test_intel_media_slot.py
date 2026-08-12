"""The Intel media slot, and the two ways it used to be handed over while still in use.

The deployment crash-loops on a Raptor Lake iGPU whenever VAAPI decoding and OpenVINO
inference touch the chip together: ``clFlush -5 CL_OUT_OF_RESOURCES``, then
``clWaitForEvents -14``, then a native abort from ``drm_buffer_object.cpp`` that takes the
container with it. An ``asyncio.Lock`` was supposed to make that impossible. It did not,
for two reasons that no existing test could see, because every existing test either
consumed its decoder to exhaustion or used a process fake that was already reaped:

1. ``async for`` does not close what it iterates. A consumer that stops early --
   ``extract_frame`` taking one frame for a thumbnail, the plates stage breaking out of a
   seek -- *abandons* the inner decoder rather than closing it. Its ``finally``, which is
   the only thing that kills ffmpeg, then runs whenever the event loop's asyncgen hook gets
   round to it: strictly after the ``async with`` that released the slot.
2. Cleanup gave a killed child two seconds to die and then released the slot anyway. A
   process wedged in an uninterruptible NFS call cannot honour SIGKILL in two seconds, and
   it still owns its iHD contexts and DRM buffer objects when the next OpenVINO request
   starts.

Both are asserted here against fakes that reproduce the awkward part rather than assuming
it away: a child that ignores SIGKILL, and a consumer that walks off mid-stream.
"""

from __future__ import annotations

import asyncio
import weakref
from types import SimpleNamespace

import pytest

from app.hardware import ffmpeg as ffmpeg_module

FRAME = (2, 2)
FRAME_BYTES = FRAME[0] * FRAME[1] * 3


class FakeStdout:
    """Hands out *frames* frames and then ends the stream, as ffmpeg does."""

    def __init__(self, owner: FakeProcess, frames: int, *, ever_ends: bool = True) -> None:
        self._owner = owner
        self._left = frames
        self._ever_ends = ever_ends

    async def readexactly(self, size: int) -> bytes:
        if self._left > 0:
            self._left -= 1
            return b"\x00" * size
        if not self._ever_ends:
            await asyncio.Event().wait()  # a decoder that never finishes on its own
        # Real ffmpeg closes stdout and exits; the natural-end path depends on that.
        self._owner.finish(0)
        raise asyncio.IncompleteReadError(b"", size)


class FakeStderr:
    async def read(self, size: int) -> bytes:
        return b""


class FakeProcess:
    """An ffmpeg child whose death can be withheld, which is the whole point."""

    _next_pid = 9000

    def __init__(self, *, frames: int = 2, dies_on_kill: bool = True, ever_ends: bool = True):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.returncode: int | None = None
        self.killed = False
        self.dies_on_kill = dies_on_kill
        self._exited = asyncio.Event()
        self.stdout = FakeStdout(self, frames, ever_ends=ever_ends)
        self.stderr = FakeStderr()

    def kill(self) -> None:
        self.killed = True
        if self.dies_on_kill:
            self.finish(-9)

    def finish(self, code: int = 0) -> None:
        if self.returncode is None:
            self.returncode = code
            self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode or 0


@pytest.fixture
def media(monkeypatch):
    """A clean per-test gate, a VAAPI decoder, and a launcher that records children."""
    monkeypatch.setattr(ffmpeg_module, "_vaapi_decode_locks", weakref.WeakKeyDictionary())
    monkeypatch.setattr(ffmpeg_module, "_ffprobe_process_locks", weakref.WeakKeyDictionary())
    monkeypatch.setattr(ffmpeg_module, "_hwaccel_refused", set())
    monkeypatch.setattr(ffmpeg_module, "_hwaccel_proven", set())
    monkeypatch.setattr(ffmpeg_module, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(
        ffmpeg_module, "select_hwaccel", lambda *a, **k: (["-hwaccel", "vaapi"], "vaapi")
    )

    async def fake_probe(path, **kwargs):
        return SimpleNamespace(width=FRAME[0], height=FRAME[1])

    monkeypatch.setattr(ffmpeg_module, "probe", fake_probe)

    launched: list[FakeProcess] = []
    template: dict[str, object] = {"frames": 2, "dies_on_kill": True, "ever_ends": True}

    async def create(*args, **kwargs):
        proc = FakeProcess(**template)  # type: ignore[arg-type]
        launched.append(proc)
        return proc

    monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", create)
    return SimpleNamespace(launched=launched, template=template)


class TestTheSlotIsNotFreeUntilTheChildIsGone:
    async def test_an_early_break_kills_the_decoder_before_the_slot_is_released(self, media):
        """The thumbnail path, which runs on every recording.

        ``extract_frame`` returns from inside the loop. Before this was fixed the child was
        still running -- and had not even been signalled -- at the moment the slot became
        available to OpenVINO.
        """
        frame = await ffmpeg_module.extract_frame("clip.ts", 1.0, codec="h264")

        assert frame is not None
        assert media.launched, "no decoder was started"
        child = media.launched[0]
        assert child.returncode is not None, (
            "the VAAPI child outlived the call that started it; OpenVINO can now be handed "
            "an iGPU that ffmpeg still owns"
        )

        gate = ffmpeg_module.intel_media_lock()
        assert not gate.locked()
        assert gate.live_children() == []
        assert gate.gpu_safe()

    async def test_a_child_that_ignores_sigkill_keeps_the_slot_shut(self, media, monkeypatch):
        """The production line 'subprocess did not exit after SIGKILL', asserted properly.

        The slot must stay closed against the next acquirer. Previously the warning was
        logged and the lock released in the same breath.
        """
        monkeypatch.setattr(ffmpeg_module, "PROCESS_EXIT_GRACE_S", 0.01)
        media.template.update(dies_on_kill=False, ever_ends=False, frames=1)

        assert await ffmpeg_module.extract_frame("stuck.ts", 1.0) is not None
        stuck = media.launched[0]
        assert stuck.killed and stuck.returncode is None, "the fake child should still be alive"

        gate = ffmpeg_module.intel_media_lock()
        assert [item["pid"] for item in gate.live_children()] == [stuck.pid]

        entered = asyncio.Event()

        async def take_the_slot():
            async with gate:
                entered.set()

        waiter = asyncio.create_task(take_the_slot())
        await asyncio.sleep(0.05)
        assert not entered.is_set(), (
            "the Intel media slot was handed out while a killed-but-alive ffmpeg child "
            "still held the render node"
        )

        # The late exit the child watcher would eventually observe.
        stuck.finish(-9)
        await asyncio.wait_for(waiter, timeout=2.0)
        assert entered.is_set()
        assert gate.live_children() == []

    async def test_a_child_that_never_dies_marks_the_slot_unhealthy(self, media, monkeypatch):
        monkeypatch.setattr(ffmpeg_module, "PROCESS_EXIT_GRACE_S", 0.01)
        monkeypatch.setattr(ffmpeg_module, "MEDIA_SETTLE_TIMEOUT_S", 0.05)
        media.template.update(dies_on_kill=False, ever_ends=False, frames=1)

        await ffmpeg_module.extract_frame("wedged.ts", 1.0)
        gate = ffmpeg_module.intel_media_lock()
        assert gate.gpu_safe(), "nothing has tried to use the slot yet"

        async with gate:
            pass

        assert not gate.gpu_safe()
        assert gate.unhealthy is not None
        health = ffmpeg_module.media_health()
        assert health["status"] == "unhealthy"
        assert health["reason"]

        # And an unhealthy slot must take hardware decode out of service everywhere.
        assert ffmpeg_module.software_decode_reason() is not None

    async def test_a_late_exit_returns_the_slot_to_health(self, media, monkeypatch):
        monkeypatch.setattr(ffmpeg_module, "PROCESS_EXIT_GRACE_S", 0.01)
        monkeypatch.setattr(ffmpeg_module, "MEDIA_SETTLE_TIMEOUT_S", 0.05)
        media.template.update(dies_on_kill=False, ever_ends=False, frames=1)

        await ffmpeg_module.extract_frame("wedged.ts", 1.0)
        gate = ffmpeg_module.intel_media_lock()
        async with gate:
            pass
        assert not gate.gpu_safe()

        media.launched[0].finish(-9)
        await asyncio.sleep(0.05)  # let the background reaper observe it

        assert gate.gpu_safe(), "the slot stayed condemned after the straggler exited"
        assert gate.live_children() == []

    async def test_a_clean_end_reaps_rather_than_kills(self, media):
        """ffmpeg that finishes by itself must not be shot on the way out.

        The old cleanup killed unconditionally, which turned every ordinary end-of-file
        into a SIGKILL and lost whatever the process was still writing to stderr.
        """
        frames = [
            item
            async for item in ffmpeg_module.iter_frames("clip.ts", frame_size=FRAME, codec="h264")
        ]

        assert len(frames) == 2
        child = media.launched[0]
        assert child.returncode == 0
        assert not child.killed, "a decoder that ended on its own was killed anyway"

    async def test_cancellation_mid_decode_reaps_the_child(self, media):
        """The Cancel button and the heartbeat both stop a job by cancelling its task.

        An ``async for`` does not close its iterator when an exception unwinds through it
        any more than when the body breaks, so every pipeline consumer wraps the decoder in
        ``aclosing``. This asserts that shape works, because the stages depend on it.
        """
        import contextlib as _contextlib

        media.template.update(frames=1, ever_ends=False)
        started = asyncio.Event()

        async def consume():
            async with _contextlib.aclosing(
                ffmpeg_module.iter_frames("clip.ts", frame_size=FRAME, codec="h264")
            ) as frames:
                async for _ in frames:
                    started.set()
                    await asyncio.Event().wait()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        child = media.launched[0]
        assert child.killed, "a cancelled decode left its ffmpeg child running"
        assert child.returncode is not None
        assert ffmpeg_module.intel_media_lock().live_children() == []

    async def test_an_abandoned_decoder_still_keeps_the_slot_shut(self, media):
        """The safety net, for any consumer that forgets `aclosing`.

        Deliberately written the wrong way round: the generator is dropped without being
        closed, exactly as the code did everywhere before this change. The child then
        outlives the call -- nothing can prevent that -- but the slot must not be offered
        to OpenVINO while it does.
        """
        media.template.update(frames=5, ever_ends=False)

        async def leak():
            async for _ in ffmpeg_module.iter_frames("clip.ts", frame_size=FRAME, codec="h264"):
                return  # abandons the generator, as `extract_frame` used to

        await leak()

        gate = ffmpeg_module.intel_media_lock()
        child = media.launched[0]
        if child.returncode is None:
            assert [item["pid"] for item in gate.live_children()] == [child.pid], (
                "a live ffmpeg child was forgotten, so the Intel media slot would be "
                "handed to OpenVINO while VAAPI still held the render node"
            )


class TestOnlyOneMediaProcessStartsAtATime:
    """Two FFmpeg processes must not initialise the Intel media stack together.

    This build initialises it on every invocation -- probing and plain software decoding
    included -- and two of those starting together abort with SIGABRT and no stderr, which
    surfaces as "ffprobe produced no output" against a perfectly good recording. The live
    library produced a steady stream of return-code -6 failures from exactly that: ffprobe
    was serialised against other ffprobes, but a second worker's *software decode* started
    up beside it quite freely.
    """

    async def test_startups_do_not_overlap(self, monkeypatch):
        import weakref as _weakref

        monkeypatch.setattr(ffmpeg_module, "_media_launch_locks", _weakref.WeakKeyDictionary())
        monkeypatch.setattr(ffmpeg_module, "MEDIA_LAUNCH_SETTLE_S", 0.05)

        starting = 0
        most = 0

        async def create(*args, **kwargs):
            nonlocal starting, most
            starting += 1
            most = max(most, starting)
            await asyncio.sleep(0)
            starting -= 1
            return FakeProcess()

        monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", create)

        await asyncio.gather(*(ffmpeg_module._spawn_media(["ffmpeg"]) for _ in range(4)))

        assert most == 1, "two media processes initialised at once; this is the -6 abort"

    async def test_the_slot_is_released_after_the_settle(self, monkeypatch):
        """Startup only. Holding it for the whole decode would serialise both workers."""
        import weakref as _weakref

        monkeypatch.setattr(ffmpeg_module, "_media_launch_locks", _weakref.WeakKeyDictionary())
        monkeypatch.setattr(ffmpeg_module, "MEDIA_LAUNCH_SETTLE_S", 0.01)

        async def create(*args, **kwargs):
            return FakeProcess()

        monkeypatch.setattr(ffmpeg_module.asyncio, "create_subprocess_exec", create)

        await ffmpeg_module._spawn_media(["ffmpeg"])

        assert not ffmpeg_module._media_launch_lock().locked()


class TestOneResourcePolicy:
    """Whenever inference owns the iGPU, every decode in the process is software.

    Enforced in ``select_hwaccel`` rather than in the detection stage, because the stage
    was never the only thing that decodes: metadata thumbnails, telemetry strips, the
    plates stage's compatibility seek and an HTTP debug route all open decoders too, and
    with two workers any of them could hold VAAPI while the other worker inferred.
    """

    def test_gpu_inference_forces_software_decode(self, monkeypatch):
        import app.ai.openvino_session as openvino

        monkeypatch.setattr(openvino, "gpu_inference_engaged", lambda: True)
        reason = ffmpeg_module.software_decode_reason()
        assert reason and "iGPU" in reason
        assert ffmpeg_module.select_hwaccel("auto", "h264") == ([], "software")
        assert ffmpeg_module.select_hwaccel("vaapi", "h264") == ([], "software")

    def test_cpu_inference_leaves_the_decoder_alone(self, monkeypatch):
        import app.ai.openvino_session as openvino

        monkeypatch.setattr(openvino, "gpu_inference_engaged", lambda: False)
        assert ffmpeg_module.software_decode_reason() is None

    def test_resolving_the_device_does_not_re_enumerate_every_time(self, monkeypatch):
        """Device enumeration is a native, synchronous OpenVINO call.

        `select_hwaccel` consults the device on every decode and the health endpoint
        consults it on every poll. Making that a native call per request meant an iGPU that
        stalled -- the exact condition this module exists to survive -- took Uvicorn's only
        event loop with it: the container stayed up, accepted connections, and answered
        nothing at all, /health included.
        """
        import app.ai.openvino_session as openvino
        from app.ai.openvino_session import reset_gpu_backend_for_tests, selected_device

        reset_gpu_backend_for_tests()
        calls = {"n": 0}

        def enumerate_devices():
            calls["n"] += 1
            return ["GPU", "CPU"]

        monkeypatch.setattr(openvino, "available_devices", enumerate_devices)
        try:
            for _ in range(25):
                assert selected_device() == "GPU"
            assert calls["n"] == 1, (
                f"enumerated the devices {calls['n']} times; this runs per decode and per "
                "health check, and it blocks the event loop when the driver stalls"
            )
        finally:
            reset_gpu_backend_for_tests()

    def test_disabling_the_gpu_invalidates_the_cached_device(self, monkeypatch):
        """The cache must not keep naming a chip that has been taken out of service."""
        import app.ai.openvino_session as openvino
        from app.ai.openvino_session import (
            disable_gpu_backend,
            reset_gpu_backend_for_tests,
            selected_device,
        )

        reset_gpu_backend_for_tests()
        monkeypatch.setattr(openvino, "available_devices", lambda: ["GPU", "CPU"])
        try:
            assert selected_device() == "GPU"
            disable_gpu_backend("clFlush -5")
            assert selected_device() == "CPU"
        finally:
            reset_gpu_backend_for_tests()

    def test_the_effective_policy_is_reported(self, monkeypatch):
        """ "Decoder: software" must be explicable, not look like a hardware fault."""
        import app.ai.openvino_session as openvino
        from app.ai.runtime import describe_media_policy

        monkeypatch.setattr(openvino, "gpu_inference_engaged", lambda: True)
        policy = describe_media_policy()

        assert policy["decode"] == "software"
        assert policy["decode_reason"]
        assert policy["media_slot"]["status"] == "healthy"


class TestAnInfrastructureFailureIsNotADamagedFile:
    """A crashing media stack must never be recorded as a verdict about the footage.

    Recording 801 on the live library is sixty seconds of perfectly good video. Every
    thumbnail offset was decoded by an FFmpeg that SIGABRTed inside the Intel media stack,
    ``stage_inspect`` concluded "no usable frame could be decoded", the damaged-footage
    policy hid it, and `claim_next` skips ignored recordings -- so nothing would ever have
    looked at it again.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            ffmpeg_module.DecodeError("no frames decoded", stderr="", returncode=-6),
            ffmpeg_module.ProbeError("ffprobe produced no output", returncode=-6),
            ffmpeg_module.DecodeError(
                "no frames decoded", stderr="Failed to sync surface: HW busy now", returncode=1
            ),
            ffmpeg_module.FFmpegError("timed out after 60s: ffmpeg"),
        ],
    )
    def test_the_machines_own_failures_are_recognised(self, exc):
        assert ffmpeg_module.is_infrastructure_failure(exc)

    @pytest.mark.parametrize(
        "exc",
        [
            ffmpeg_module.ProbeError(
                "ffprobe produced no output",
                returncode=1,
                stderr="Invalid data found when processing input",
            ),
            ffmpeg_module.DecodeError("no frames decoded", stderr="", returncode=0),
        ],
    )
    def test_a_verdict_about_the_file_is_left_alone(self, exc):
        assert not ffmpeg_module.is_infrastructure_failure(exc)

    async def test_an_aborted_decode_is_not_reported_as_conclusive(self, monkeypatch, tmp_path):
        from app.pipeline import stages

        async def aborted(path, t, *, hwaccel="auto", codec=None, on_error=None):
            if on_error is not None:
                on_error(ffmpeg_module.DecodeError("no frames", stderr="", returncode=-6))
            return None

        monkeypatch.setattr(stages, "extract_frame", aborted)
        written, conclusive = await stages._choose_thumbnail(
            tmp_path / "clip.ts",
            tmp_path / "thumb.jpg",
            duration_s=60.0,
            codec="h264",
            width=480,
            quality=80,
        )

        assert not written
        assert not conclusive, (
            "an aborting media stack was accepted as proof the recording is damaged; "
            "stage_inspect would set source_unusable and the policy would hide it"
        )

    async def test_a_genuinely_unreadable_file_is_still_conclusive(self, monkeypatch, tmp_path):
        from app.pipeline import stages

        async def nothing(path, t, *, hwaccel="auto", codec=None, on_error=None):
            return None

        monkeypatch.setattr(stages, "extract_frame", nothing)
        written, conclusive = await stages._choose_thumbnail(
            tmp_path / "clip.ts",
            tmp_path / "thumb.jpg",
            duration_s=60.0,
            codec="h264",
            width=480,
            quality=80,
        )

        assert not written
        assert conclusive, "a file that simply has no decodable picture must still be judged"


class TestAPoisonedGpuContextIsFatalToTheGpu:
    """One OpenCL failure poisons the context; every later request fails the same way.

    On the live library that produced one ``CL_OUT_OF_RESOURCES`` followed by thirty-seven
    suppressed repeats for a single recording -- and the recording was then marked
    COMPLETED with zero detections, because the upstream model helper catches its own
    inference errors and returns nothing. Our own "inference failed" handler never fired
    once in the whole crash loop.
    """

    @pytest.fixture(autouse=True)
    def _clean(self):
        from app.ai.openvino_session import reset_gpu_backend_for_tests

        reset_gpu_backend_for_tests()
        yield
        reset_gpu_backend_for_tests()

    @pytest.mark.parametrize(
        "message",
        [
            "[GPU] clFlush, error code: -5 CL_OUT_OF_RESOURCES",
            "[GPU] clWaitForEvents, error code: -14 CL_EXEC_STATUS_ERROR_FOR_EVENTS_IN_WAIT_LIST",
            "Exception from src/plugins/intel_gpu/src/runtime/ocl/ocl_stream.cpp:388",
            "Abort was called at 91 line in file: ./shared/source/os_interface/linux/"
            "drm_buffer_object.cpp",
        ],
    )
    def test_the_deployments_own_errors_are_recognised(self, message):
        from app.ai.openvino_session import is_gpu_context_failure

        assert is_gpu_context_failure(RuntimeError(message))

    def test_an_ordinary_error_is_not_a_poisoned_context(self):
        from app.ai.openvino_session import is_gpu_context_failure

        assert not is_gpu_context_failure(ValueError("input shape mismatch"))
        assert not is_gpu_context_failure(KeyError("output"))

    def test_a_driver_failure_moves_the_session_to_the_cpu_and_still_answers(self, monkeypatch):
        """The frame must still be inferred. Returning nothing is what lost the detections."""
        import threading

        import numpy as np

        import app.ai.openvino_session as openvino
        from app.ai.openvino_session import OpenVINOSession, TensorInfo, gpu_backend_disabled

        gpu_port, cpu_port = object(), object()

        class GpuRequest:
            def infer(self, feed):
                raise RuntimeError("[GPU] clFlush, error code: -5 CL_OUT_OF_RESOURCES")

        class CpuRequest:
            def infer(self, feed):
                return {cpu_port: np.asarray([7])}

        compiled_gpu = SimpleNamespace(
            outputs=[gpu_port], create_infer_request=lambda: GpuRequest()
        )
        compiled_cpu = SimpleNamespace(
            outputs=[cpu_port], create_infer_request=lambda: CpuRequest()
        )
        monkeypatch.setattr(
            openvino,
            "_get_core",
            lambda: SimpleNamespace(compile_model=lambda model, device, config: compiled_cpu),
        )

        session = object.__new__(OpenVINOSession)
        session.device = "GPU"
        session._outputs = (TensorInfo("output", (1,)),)
        session._output_ports = {"output": gpu_port}
        session._compiled = compiled_gpu
        session._local = threading.local()
        session._model = object()
        session._model_name = "rf-detr-small-512-coco.onnx"
        session._config = {"PERFORMANCE_HINT": "LATENCY"}
        session._rebuild_lock = threading.Lock()

        result = session.run(["output"], {"input": np.asarray([1])})

        assert [int(item[0]) for item in result] == [7], "the frame was silently lost"
        assert session.device == "CPU"
        assert gpu_backend_disabled(), "the poisoned GPU was left armed for the next job"

    def test_a_disabled_gpu_is_removed_from_device_selection(self, monkeypatch):
        import app.ai.openvino_session as openvino
        from app.ai.openvino_session import disable_gpu_backend, selected_device

        monkeypatch.setattr(openvino, "available_devices", lambda: ["GPU", "CPU"])
        assert selected_device() == "GPU"

        disable_gpu_backend("clFlush -5")

        assert selected_device() == "CPU"
        assert openvino.gpu_inference_engaged() is False

    async def test_detection_refuses_the_gpu_while_the_media_slot_is_unhealthy(self, monkeypatch):
        """An unreaped media child is the precondition for the abort. Do not add to it."""
        import numpy as np

        from app.ai import detector as detector_module
        from app.ai.detector import ObjectDetector

        monkeypatch.setattr(ffmpeg_module, "_vaapi_decode_locks", weakref.WeakKeyDictionary())
        monkeypatch.setattr(detector_module, "get_settings_service", object)

        moved: list[str] = []

        class Model:
            device = "GPU"

            def ensure_cpu(self, reason):
                moved.append(reason)
                Model.device = "CPU"
                return True

        class Detector:
            model = Model()

            def predict(self, frame):
                return []

        gate = ffmpeg_module.intel_media_lock()
        gate._unhealthy = "pid 537 did not exit"

        detector = ObjectDetector()
        detector._detector = Detector()
        try:
            frame = np.zeros((4, 4, 3), dtype=np.uint8)
            assert await detector.detect(frame, classes=frozenset({"car"})) == []
        finally:
            Model.device = "GPU"

        assert moved, "GPU inference was issued onto an unhealthy Intel media slot"
