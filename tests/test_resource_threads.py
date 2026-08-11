"""Native runtimes must share CPU capacity instead of each claiming the host."""

from __future__ import annotations

from types import SimpleNamespace


class _Settings:
    def __init__(self, *, threads: int = 0, workers: int = 2) -> None:
        self.values = {
            "advanced.ffmpeg_threads": threads,
            "processing.max_workers": workers,
        }

    def get_nowait(self, key: str):
        return self.values[key]


def test_auto_budget_is_shared_and_capped(monkeypatch):
    import app.core.resources as resources

    monkeypatch.setattr(resources, "get_settings_service", lambda: _Settings())
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 20)

    assert resources.native_thread_budget() == 4


def test_explicit_budget_is_honoured(monkeypatch):
    import app.core.resources as resources

    monkeypatch.setattr(resources, "get_settings_service", lambda: _Settings(threads=3, workers=2))
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 20)

    assert resources.native_thread_budget() == 3


def test_standalone_media_tools_still_get_a_safe_budget(monkeypatch):
    import app.core.resources as resources

    def uninitialised():
        raise RuntimeError("settings service is not initialised")

    monkeypatch.setattr(resources, "get_settings_service", uninitialised)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 20)

    assert resources.native_thread_budget() == 4


def test_onnx_fallback_threads_are_bounded_and_do_not_spin(monkeypatch):
    import app.core.resources as resources

    class Options:
        def __init__(self) -> None:
            self.config: dict[str, str] = {}

        def add_session_config_entry(self, key: str, value: str) -> None:
            self.config[key] = value

    fake_ort = SimpleNamespace(
        SessionOptions=Options,
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL="all"),
    )
    monkeypatch.setitem(__import__("sys").modules, "onnxruntime", fake_ort)
    monkeypatch.setattr(resources, "native_thread_budget", lambda: 4)

    options = resources.onnx_session_options()

    assert options.intra_op_num_threads == 4
    assert options.inter_op_num_threads == 1
    assert options.execution_mode == "sequential"
    assert options.config == {
        "session.intra_op.allow_spinning": "0",
        "session.inter_op.allow_spinning": "0",
    }


def test_opencv_uses_the_same_budget(monkeypatch):
    import app.core.resources as resources

    calls: list[int] = []
    monkeypatch.setitem(
        __import__("sys").modules,
        "cv2",
        SimpleNamespace(setNumThreads=calls.append),
    )
    monkeypatch.setattr(resources, "native_thread_budget", lambda: 4)

    assert resources.configure_opencv_threads() == 4
    assert calls == [4]
