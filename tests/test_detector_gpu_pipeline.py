"""RF-DETR's upstream single-image results and Intel media ownership under overlap."""

import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.ai.detector import Detection2D, ObjectDetector
from app.ai.models import REGISTRY
from app.hardware.ffmpeg import intel_media_lock


async def test_split_prediction_matches_upstream_and_only_inference_owns_gpu(monkeypatch):
    from open_image_models.detection.core.rf_detr import postprocess
    from open_image_models.detection.core.rf_detr.inference import RFDETRDetector

    frame = np.arange(48 * 32 * 3, dtype=np.uint8).reshape(32, 48, 3)
    original = frame.copy()
    inputs = []
    gate = intel_media_lock()
    inference_gate_states = []

    def run(_names, feed):
        inputs.append(feed["image"])
        inference_gate_states.append(gate.locked())
        boxes = np.array([[[0.5, 0.5, 0.5, 0.5]]], dtype=np.float32)
        logits = np.array([[[4.0]]], dtype=np.float32)
        return [boxes, logits]

    upstream = object.__new__(RFDETRDetector)
    upstream.model = SimpleNamespace(device="GPU.0", run=run)
    upstream.input_name = "image"
    upstream.img_size = (576, 576)
    upstream.batch_size = 1
    upstream.num_select = 300
    upstream.class_labels = {0: "car"}
    upstream.conf_thresh = 0.4
    [expected] = upstream.predict(frame)

    detector = ObjectDetector()
    detector._spec = REGISTRY["rfdetr-medium"]
    detector._detector = upstream
    original_prepare = detector.prepare_frame
    original_post = postprocess.convert_to_detection_result
    phases = []

    def prepare(image):
        phases.append(("prepare", gate.locked()))
        return original_prepare(image)

    def finish(**kwargs):
        phases.append(("postprocess", gate.locked()))
        return original_post(**kwargs)

    monkeypatch.setattr(detector, "prepare_frame", prepare)
    monkeypatch.setattr(postprocess, "convert_to_detection_result", finish)
    assert await detector.detect(frame, classes=frozenset({"car"})) == [
        Detection2D("car", expected.confidence, 0.25, 0.25, 0.5, 0.5)
    ]
    np.testing.assert_array_equal(inputs[0], inputs[1])
    np.testing.assert_array_equal(original, frame)
    assert inference_gate_states == [False, True]
    assert phases == [("prepare", False), ("postprocess", False)]


async def test_repeated_cancellation_keeps_media_slot_until_native_inference_returns():
    gate = intel_media_lock()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def predict(_frame):
        entered.set()
        try:
            assert release.wait(5)
            return []
        finally:
            finished.set()

    detector = ObjectDetector()
    detector._detector = SimpleNamespace(model=SimpleNamespace(device="GPU"), predict=predict)
    job = asyncio.create_task(
        detector.detect(np.zeros((4, 4, 3), dtype=np.uint8), classes=frozenset({"car"}))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(2):
            job.cancel()
            await asyncio.sleep(0)
            assert gate.locked()
            assert not job.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await job
    assert finished.is_set()
    assert not gate.locked()


async def test_known_media_failure_recovers_without_waiting_for_a_held_slot():
    gate = intel_media_lock()
    model = SimpleNamespace(device="GPU")
    model.ensure_cpu = lambda _reason: setattr(model, "device", "CPU")
    seen = []

    def predict(_frame):
        seen.append(model.device)
        return []

    detector = ObjectDetector()
    detector._detector = SimpleNamespace(model=model, predict=predict)
    async with gate:
        gate._unhealthy = "VAAPI child has not exited"
        try:
            await asyncio.wait_for(
                detector.detect(np.zeros((4, 4, 3), np.uint8), classes=frozenset({"car"})),
                timeout=2,
            )
        finally:
            gate._unhealthy = None
    assert seen == ["CPU"]


@pytest.mark.parametrize("outcome", ["recovered", "cpu_error", "request_error"])
async def test_split_inference_retries_same_frame_or_propagates_failure(monkeypatch, outcome):
    from app.ai import detector as module

    gate = intel_media_lock()
    reason = None
    requests = []
    prepared = np.ones((1, 3, 4, 4), dtype=np.float32)
    frame = np.zeros((32, 48, 3), dtype=np.uint8)
    model = SimpleNamespace(device="GPU.0")
    monkeypatch.setattr(module, "gpu_backend_disabled", lambda: reason)
    monkeypatch.setattr(module, "gpu_context_failed", lambda: reason is not None)

    def run(_outputs, feed):
        nonlocal reason
        assert feed["image"] is prepared
        requests.append((model.device, gate.locked()))
        if model.device == "GPU.0":
            if outcome == "request_error":
                raise ValueError("invalid model input")
            reason = "CL_OUT_OF_RESOURCES"
            raise RuntimeError(reason)
        if outcome == "cpu_error":
            raise RuntimeError("CPU inference failed")
        return [np.array([[[0.5, 0.5, 0.5, 0.5]]]), np.array([[[4.0]]])]

    def ensure_cpu(failure):
        assert failure == reason
        assert not gate.locked()
        model.device = "CPU"

    model.run = run
    model.ensure_cpu = ensure_cpu
    detector = ObjectDetector()
    detector._spec = REGISTRY["rfdetr-medium"]
    detector._detector = SimpleNamespace(
        model=model, input_name="image", class_labels={0: "car"}, conf_thresh=0.4, num_select=300
    )
    prediction = detector.detect(frame, classes=frozenset({"car"}), prepared=prepared)
    if outcome == "request_error":
        with pytest.raises(ValueError, match="invalid model input"):
            await prediction
        assert requests == [("GPU.0", True)]
        return
    if outcome == "cpu_error":
        with pytest.raises(RuntimeError, match="CPU inference failed"):
            await prediction
    else:
        [car] = await prediction
        assert car.class_label == "car"
    assert requests == [("GPU.0", True), ("CPU", False)]
