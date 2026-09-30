"""A failed native GPU context must never receive another OpenVINO call."""

import concurrent.futures
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app.ai import openvino_session as runtime


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch, tmp_path):
    runtime.reset_gpu_backend_for_tests()
    monkeypatch.setattr(runtime, "_gpu_failure_marker_path", lambda: tmp_path / "gpu.json")
    yield
    runtime.reset_gpu_backend_for_tests()


def bare_session(request):
    session = object.__new__(runtime.OpenVINOSession)
    session.device = "GPU"
    session._request = lambda: request
    session._outputs = (runtime.TensorInfo("output", (1,)),)
    session._output_ports = {"output": "output"}
    session._rebuild_lock = threading.Lock()
    session._model_path = "fixture.onnx"
    session._model_name = "fixture.onnx"
    session._model = object()
    session._config = {}
    return session


def test_gpu_compile_context_failure_does_not_compile_cpu_in_poisoned_core(monkeypatch):
    targets = []

    def compile_model(_model, target, _config):
        targets.append(target)
        if target == "GPU":
            raise RuntimeError("CL_OUT_OF_RESOURCES during compile")
        return SimpleNamespace(outputs=[])

    core = SimpleNamespace(
        read_model=lambda _path: SimpleNamespace(inputs=[], outputs=[]),
        compile_model=compile_model,
    )
    monkeypatch.setattr(runtime, "_get_core", lambda: core)
    with pytest.raises(RuntimeError, match="CL_OUT_OF_RESOURCES"):
        runtime.OpenVINOSession("fixture.onnx", device="GPU")
    assert targets == ["GPU"]
    assert runtime.read_gpu_failure_marker() is not None


def test_native_failure_from_compiled_property_is_not_ignored(monkeypatch):
    def failed_property(_key):
        raise RuntimeError("CL_OUT_OF_RESOURCES in GPU property")

    core = SimpleNamespace(
        read_model=lambda _path: SimpleNamespace(inputs=[], outputs=[]),
        compile_model=lambda *_: SimpleNamespace(outputs=[], get_property=failed_property),
    )
    monkeypatch.setattr(runtime, "_get_core", lambda: core)
    with pytest.raises(RuntimeError, match="CL_OUT_OF_RESOURCES"):
        runtime.OpenVINOSession("fixture.onnx", device="GPU")
    assert runtime.gpu_context_failed()


def test_healthy_cpu_inference_is_parallel_and_gpu_waits_for_it():
    both_reading = threading.Barrier(3)
    release = threading.Event()

    def read():
        with runtime._cpu_native_lane() as usable:
            assert usable
            both_reading.wait(timeout=5)
            assert release.wait(5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        readers = [executor.submit(read) for _ in range(2)]
        both_reading.wait(timeout=5)
        writer_entered = threading.Event()

        def write():
            with runtime._exclusive_native_lane():
                writer_entered.set()

        writer = executor.submit(write)
        assert not writer_entered.wait(0.05)
        release.set()
        for reader in readers:
            reader.result(timeout=5)
        writer.result(timeout=5)
        assert writer_entered.is_set()


def test_waiting_gpu_request_rechecks_failure_before_entering_native_code():
    entered = []
    request = SimpleNamespace(infer=lambda _feed: entered.append(True) or {"output": np.ones(1)})
    session = bare_session(request)
    started = threading.Event()

    def run():
        started.set()
        return session.run(None, {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with runtime._gpu_inference_lock:
            future = executor.submit(run)
            assert started.wait(5)
            runtime.disable_gpu_backend("CL_OUT_OF_RESOURCES", durable=True)
        with pytest.raises(RuntimeError, match="disabled"):
            future.result(timeout=5)
    assert entered == []


def test_existing_session_recovers_through_plain_ort_without_touching_openvino(monkeypatch):
    import onnxruntime

    expected = [np.asarray([7])]
    calls = []

    def forbidden():
        pytest.fail("re-entered the poisoned OpenVINO core")

    def ort_session(path, **kwargs):
        calls.append((path, kwargs))
        return SimpleNamespace(run=lambda _names, _feed: expected)

    monkeypatch.setattr(runtime, "_get_core", forbidden)
    monkeypatch.setattr(onnxruntime, "InferenceSession", ort_session)
    session = bare_session(None)
    runtime.disable_gpu_backend("CL_OUT_OF_RESOURCES", durable=True)
    assert session.ensure_cpu("the previous request failed")
    assert session.device == "CPU"
    assert session.run(None, {}) == expected
    assert calls[0][0] == "fixture.onnx"
    assert calls[0][1]["providers"] == ["CPUExecutionProvider"]
    assert calls[0][1]["sess_options"].intra_op_num_threads > 0


async def test_detector_cpu_recovery_does_not_block_the_event_loop(monkeypatch):
    from app.ai import detector as module

    event_loop_thread = threading.get_ident()
    recovery_threads = []
    model = SimpleNamespace(device="GPU")

    def recover(_reason):
        recovery_threads.append(threading.get_ident())
        model.device = "CPU"

    model.ensure_cpu = recover
    detector = module.ObjectDetector()
    detector._detector = SimpleNamespace(model=model, predict=lambda _frame: [])
    monkeypatch.setattr(module, "get_settings_service", object)
    runtime.disable_gpu_backend("failed media child")
    assert await detector.detect(np.zeros((4, 4, 3)), classes=frozenset({"car"})) == []
    assert recovery_threads and recovery_threads != [event_loop_thread]


def test_new_session_does_not_even_read_a_model_after_native_context_failure(monkeypatch):
    def forbidden():
        pytest.fail("OpenVINO core should remain untouched")

    runtime.disable_gpu_backend("CL_OUT_OF_RESOURCES", durable=True)
    monkeypatch.setattr(runtime, "_get_core", forbidden)
    with pytest.raises(RuntimeError, match="disabled"):
        runtime.OpenVINOSession("fixture.onnx", device="CPU")


def test_fallback_published_between_run_snapshots_never_uses_old_gpu_request():
    expected = [np.ones(1)]

    class Interleaved(runtime.OpenVINOSession):
        @property
        def device(self):
            self._cpu_session = SimpleNamespace(run=lambda *_: expected)
            return "CPU"

        def _request(self):
            pytest.fail("entered stale GPU request after the CPU fallback was published")

    session = object.__new__(Interleaved)
    session._cpu_session = None
    assert session.run(None, {}) == expected


def _two_output_model():
    """Minimal ONNX protobuf: twice=X+X, squared=X*X; no optional onnx dependency."""

    def varint(value):
        encoded = bytearray()
        while value > 127:
            encoded.append((value & 127) | 128)
            value >>= 7
        return bytes(encoded) + bytes([value])

    def integer(number, value):
        return varint(number << 3) + varint(value)

    def field(number, value):
        value = value.encode() if isinstance(value, str) else value
        return varint((number << 3) | 2) + varint(len(value)) + value

    def value_info(name):
        tensor_type = integer(1, 1) + field(2, field(1, integer(1, 1)))
        return field(1, name) + field(2, field(1, tensor_type))

    graph = field(2, "fallback") + field(11, value_info("input"))
    for operation, output in (("Add", "twice"), ("Mul", "squared")):
        graph += field(1, field(1, "input") * 2 + field(2, output) + field(4, operation))
        graph += field(12, value_info(output))
    return integer(1, 9) + field(7, graph) + field(8, integer(2, 13))


def test_real_cpu_model_recovers_named_outputs_through_plain_ort(monkeypatch, tmp_path):
    pytest.importorskip("openvino")
    model = tmp_path / "two-output.onnx"
    model.write_bytes(_two_output_model())
    session = runtime.OpenVINOSession(model, device="CPU")
    assert session.get_inputs()[0].name == "input"
    assert {item.name for item in session.get_outputs()} == {"twice", "squared"}
    feed = {"input": np.asarray([3], dtype=np.float32)}
    before = session.run(["squared", "twice"], feed)
    runtime.disable_gpu_backend("another model hit CL_OUT_OF_RESOURCES", durable=True)

    def forbidden():
        pytest.fail("existing CPU model entered the shared failed OpenVINO core")

    monkeypatch.setattr(runtime, "_get_core", forbidden)
    monkeypatch.setattr(session, "_request", forbidden)
    after = session.run(["squared", "twice"], feed)
    for actual, expected in zip(after, before, strict=True):
        np.testing.assert_array_equal(actual, expected)
    assert [value.item() for value in after] == [9, 6]
    assert session.get_providers() == ["CPUExecutionProvider"]
    assert [item.name for item in session.get_inputs()] == ["input"]
    assert {item.name for item in session.get_outputs()} == {"twice", "squared"}


@pytest.mark.parametrize("device", ["GPU", "CPU", "NPU"])
async def test_failed_cpu_recovery_is_not_reported_as_an_empty_detection(monkeypatch, device):
    from app.ai import detector as module

    def failed(_reason):
        raise RuntimeError("CPU model creation failed")

    detector = module.ObjectDetector()
    detector._detector = SimpleNamespace(model=SimpleNamespace(device=device, ensure_cpu=failed))
    monkeypatch.setattr(module, "get_settings_service", object)
    runtime.disable_gpu_backend("CL_OUT_OF_RESOURCES", durable=True)
    with pytest.raises(RuntimeError, match="CPU model creation failed"):
        await detector.detect(np.zeros((4, 4, 3)), classes=frozenset({"car"}))
