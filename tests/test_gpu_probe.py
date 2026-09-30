"""The diagnostic parent survives a failed child and respects its resource bounds."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backend/scripts/probe_gpu.py"
spec = importlib.util.spec_from_file_location("probe_gpu", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_timeout_is_a_failed_report_not_a_hung_supervisor():
    result = probe.supervise([sys.executable, "-c", "import time; time.sleep(30)"], 0.1)
    assert result["status"] == "timeout"
    assert result["wall_seconds"] < 10
    assert result["returncode"] is not None


def test_crashed_child_is_reported_without_a_native_runtime_in_the_parent():
    result = probe.supervise([sys.executable, "-c", "import os; os._exit(134)"], 5)
    assert result["status"] == "failed"
    assert result["returncode"] == 134


def test_native_stderr_is_bounded():
    result = probe.supervise(
        [sys.executable, "-c", "import sys; sys.stderr.write('x' * 2000000)"], 5
    )
    assert result["status"] == "failed"
    assert len(result["stderr_tail"]) == 12000


@pytest.fixture
def dynamic_model(tmp_path):
    ov = pytest.importorskip("openvino")
    parameter = ov.opset13.parameter([-1, 3, 2, 2], name="images")
    parameter.output(0).set_names({"images"})
    model = ov.Model(
        [ov.opset13.add(parameter, ov.opset13.constant(1.0, dtype="float32"))], [parameter]
    )
    path = tmp_path / "probe.xml"
    ov.save_model(model, path)
    return path


def test_real_cpu_inference_in_child_with_dynamic_batch(dynamic_model):
    result = probe.supervise(
        [
            sys.executable,
            str(SCRIPT),
            "--child",
            "--model",
            str(dynamic_model),
            "--device",
            "CPU",
            "--iterations",
            "3",
            "--warmup",
            "1",
        ],
        30,
    )
    assert result["status"] == "passed", result
    assert result["inputs"][0]["shape"] == [1, 3, 2, 2]
    assert result["execution_devices"] == ["CPU"]
    assert result["outputs"][0]["finite"]
    assert result["iterations"] == 3


def test_shape_override_and_static_compile(dynamic_model):
    result = probe.supervise(
        [
            sys.executable,
            str(SCRIPT),
            "--child",
            "--model",
            str(dynamic_model),
            "--device",
            "CPU",
            "--iterations",
            "1",
            "--warmup",
            "0",
            "--shape",
            "images=2,3,2,2",
            "--static-shapes",
        ],
        30,
    )
    assert result["status"] == "passed", result
    assert result["inputs"][0]["shape"] == [2, 3, 2, 2]
    assert result["outputs"][0]["shape"] == [2, 3, 2, 2]


def test_huge_dynamic_shape_is_refused_before_allocating(dynamic_model):
    result = probe.supervise(
        [
            sys.executable,
            str(SCRIPT),
            "--child",
            "--model",
            str(dynamic_model),
            "--device",
            "CPU",
            "--shape",
            "images=1000000000,3,2,2",
        ],
        30,
    )
    assert result["status"] == "failed"
    assert "max-input-mib" in result["error"]


def test_missing_model_and_invalid_arguments_are_controlled(tmp_path):
    assert probe.main(["--model", str(tmp_path / "missing.onnx")]) == 1
    with pytest.raises(SystemExit):
        probe.main(["--model", "model.onnx", "--warmup", "-1"])
    with pytest.raises(SystemExit):
        probe.main(["--model", "model.onnx", "--device", "AUTO:GPU,CPU"])


@pytest.mark.parametrize(
    "result",
    [
        {"status": "timeout", "returncode": -9, "unreaped_child_pid": 123},
        {"status": "failed", "returncode": 1, "error": "RuntimeError: CL_OUT_OF_RESOURCES"},
    ],
)
def test_directory_scan_stops_after_native_failure(tmp_path, monkeypatch, capsys, result):
    for name in ("first.onnx", "second.onnx"):
        (tmp_path / name).write_bytes(b"model")
    calls = []

    def failed(command, _timeout):
        calls.append(command)
        return result

    monkeypatch.setattr(probe, "supervise", failed)
    assert probe.main(["--model-dir", str(tmp_path)]) == 1
    assert len(calls) == 1
    assert '"status": "skipped"' in capsys.readouterr().out
