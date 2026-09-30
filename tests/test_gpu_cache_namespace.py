"""Only GPU disk caches are partitioned by a verified container runtime fingerprint."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai import openvino_session as runtime


@pytest.fixture
def cache_key(monkeypatch, tmp_path, app_config):
    path = tmp_path / "intel-runtime-cache-key"
    monkeypatch.setattr(runtime, "_INTEL_RUNTIME_CACHE_KEY", path)
    monkeypatch.setattr(runtime, "_runtime_version", lambda: "2025.4.1")
    return path


def test_gpu_fingerprint_changes_cache_namespace_and_preserves_older_cache(cache_key, app_config):
    paths = []
    for fingerprint in ("a" * 64, "b" * 64):
        cache_key.write_bytes((fingerprint + "\n").encode())
        path = Path(runtime._model_cache_options("GPU.0")["CACHE_DIR"])
        assert path == app_config.data_dir / f"openvino_cache_2025.4.1_intel_{fingerprint}"
        assert path.is_dir()
        paths.append(path)
    assert paths[0] != paths[1]
    assert paths[0].is_dir()


def test_missing_key_keeps_non_docker_gpu_cache(cache_key, app_config):
    assert runtime._model_cache_options("GPU") == {
        "CACHE_DIR": str(app_config.data_dir / "openvino_cache_2025.4.1")
    }


def test_cpu_does_not_read_a_gpu_fingerprint(monkeypatch, cache_key, app_config):
    def forbidden(*_args):
        pytest.fail("CPU should not read the Intel GPU cache key")

    monkeypatch.setattr(runtime, "_INTEL_RUNTIME_CACHE_KEY", SimpleNamespace(open=forbidden))
    assert runtime._model_cache_options("CPU") == {
        "CACHE_DIR": str(app_config.data_dir / "openvino_cache_2025.4.1")
    }


@pytest.mark.parametrize(
    "content",
    [b"", b"../outside", b"A" * 64, b"a" * 63, b"a" * 64 + b"\nextra", b"a" * 100000],
    ids=["empty", "traversal", "uppercase", "short", "trailing", "oversized"],
)
def test_invalid_present_key_disables_disk_cache(cache_key, app_config, content):
    cache_key.write_bytes(content)
    assert runtime._model_cache_options("GPU") == {}
    assert not list(app_config.data_dir.glob("openvino_cache*"))


def test_unreadable_key_disables_disk_cache(monkeypatch, cache_key, app_config):
    def denied(*_args):
        raise PermissionError("unreadable image fingerprint")

    monkeypatch.setattr(runtime, "_INTEL_RUNTIME_CACHE_KEY", SimpleNamespace(open=denied))
    assert runtime._model_cache_options("GPU") == {}
    assert not list(app_config.data_dir.glob("openvino_cache*"))


def test_gpu_compile_fallback_restores_cpu_cache_namespace(monkeypatch, cache_key, app_config):
    runtime.reset_gpu_backend_for_tests()
    cache_key.write_bytes(b"a" * 64 + b"\n")
    calls = []

    def compile_model(_model, target, config):
        calls.append((target, dict(config)))
        if target == "GPU":
            raise RuntimeError("unsupported model operation")
        return SimpleNamespace(outputs=[], get_property=lambda _: 1)

    monkeypatch.setattr(
        runtime,
        "_get_core",
        lambda: SimpleNamespace(
            read_model=lambda _: SimpleNamespace(inputs=[], outputs=[]), compile_model=compile_model
        ),
    )
    try:
        session = runtime.OpenVINOSession("model.onnx", device="GPU")
        assert session.device == "CPU"
        assert calls[0][1]["CACHE_DIR"].endswith("_intel_" + "a" * 64)
        assert calls[1][1]["CACHE_DIR"] == str(app_config.data_dir / "openvino_cache_2025.4.1")
    finally:
        runtime.reset_gpu_backend_for_tests()


def test_poison_guard_runs_before_any_cache_or_native_access(monkeypatch):
    runtime.reset_gpu_backend_for_tests()

    def forbidden(*_args):
        pytest.fail("a poisoned constructor must stop before cache/native access")

    monkeypatch.setattr(runtime, "_gpu_context_failed", True)
    monkeypatch.setattr(runtime, "_get_core", forbidden)
    monkeypatch.setattr(runtime, "_model_cache_options", forbidden)
    with pytest.raises(RuntimeError, match="disabled"):
        runtime.OpenVINOSession("model.onnx", device="GPU")
