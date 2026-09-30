"""Recovering an iGPU that condemned itself, without a shell inside the container.

The durable marker is deliberately one-way: a poisoned OpenCL context fails every later
request on the same compiled model, so re-arming mid-process only reproduces the abort.
That makes the marker correct and the *operator* stuck -- until this, the only way to ask
"has the chip failed once or thirty times?", or to let it try again, was a filesystem the
deployment does not expose.
"""

from __future__ import annotations

import json

import pytest

from app.ai import openvino_session


@pytest.fixture(autouse=True)
def _isolated_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(openvino_session, "_gpu_failure_marker_path", lambda: tmp_path / "gpu.json")
    openvino_session.reset_gpu_backend_for_tests()
    yield
    openvino_session.reset_gpu_backend_for_tests()


class TestReadingTheVerdict:
    def test_no_marker_means_nothing_to_report(self):
        assert openvino_session.read_gpu_failure_marker() is None

    def test_it_reports_the_failure_count(self, tmp_path):
        """The count is the whole point: one abort weeks ago and thirty today want
        opposite responses, and the in-process reason cannot tell them apart."""
        (tmp_path / "gpu.json").write_text(
            json.dumps(
                {
                    "reason": "RuntimeError: [GPU] clFlush, error code: -5 CL_OUT_OF_RESOURCES",
                    "failures": 7,
                    "last_failed_at": "2026-09-01T13:00:00+00:00",
                }
            ),
            "utf-8",
        )
        marker = openvino_session.read_gpu_failure_marker()
        assert marker is not None
        assert marker["failures"] == 7
        assert "CL_OUT_OF_RESOURCES" in str(marker["reason"])
        assert marker["last_failed_at"] == "2026-09-01T13:00:00+00:00"

    @pytest.mark.parametrize("contents", ["{not json", "[]", "null", '"text"'])
    def test_a_corrupt_marker_is_not_a_crash(self, tmp_path, contents):
        """A half-written marker must not take down the status endpoint that exists to
        explain why the GPU is off."""
        (tmp_path / "gpu.json").write_text(contents, "utf-8")
        marker = openvino_session.read_gpu_failure_marker()
        assert marker is not None
        assert marker["failures"] is None
        assert openvino_session.restore_gpu_failure_state()
        assert openvino_session.gpu_backend_disabled() is not None

    @pytest.mark.parametrize("count", ["invalid", [], {}, float("inf")])
    def test_invalid_failure_count_does_not_break_status_or_startup(self, tmp_path, count):
        (tmp_path / "gpu.json").write_text(
            json.dumps({"reason": "previous failure", "failures": count}), "utf-8"
        )
        assert openvino_session.read_gpu_failure_marker() is not None
        assert openvino_session.restore_gpu_failure_state() == "previous failure"


class TestPersistingTheVerdict:
    def test_failed_publication_preserves_previous_marker_and_can_retry(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "gpu.json"
        previous = json.dumps({"reason": "first", "failures": 3})
        path.write_text(previous, "utf-8")
        replace = openvino_session.os.replace

        def fail_replace(source, target):
            assert path.read_text("utf-8") == previous
            assert json.loads(source.read_text("utf-8"))["failures"] == 4
            raise OSError("storage temporarily unavailable")

        monkeypatch.setattr(openvino_session.os, "replace", fail_replace)
        openvino_session.disable_gpu_backend("second", durable=True)
        assert path.read_text("utf-8") == previous
        assert list(tmp_path.iterdir()) == [path]
        monkeypatch.setattr(openvino_session.os, "replace", replace)
        openvino_session.disable_gpu_backend("second", durable=True)
        assert json.loads(path.read_text("utf-8"))["failures"] == 4

    def test_a_new_failure_can_replace_a_corrupt_marker(self, tmp_path):
        (tmp_path / "gpu.json").write_text("[]", "utf-8")
        openvino_session.disable_gpu_backend("new failure", durable=True)
        assert openvino_session.read_gpu_failure_marker()["reason"] == "new failure"


class TestClearingTheVerdict:
    def test_clearing_removes_only_the_marker_until_restart(self, tmp_path, monkeypatch):
        openvino_session.disable_gpu_backend("CL_OUT_OF_RESOURCES", durable=True)
        assert openvino_session.gpu_backend_disabled() is not None

        def must_not_probe_or_clear():
            pytest.fail("retry must not clear or re-enumerate a poisoned runtime")

        with monkeypatch.context() as patch:
            patch.setattr(openvino_session, "_clear_device_cache", must_not_probe_or_clear)
            assert openvino_session.clear_gpu_failure_state() is True
        assert openvino_session.gpu_backend_disabled() == "CL_OUT_OF_RESOURCES"
        assert openvino_session.read_gpu_failure_marker() is None

    def test_clearing_rearms_persistence_for_the_next_abort(self, tmp_path):
        """An in-flight native request can fail after a retry was requested."""
        openvino_session.disable_gpu_backend("first", durable=True)
        openvino_session.clear_gpu_failure_state()
        openvino_session.disable_gpu_backend("second", durable=True)
        marker = openvino_session.read_gpu_failure_marker()
        assert marker is not None, "a post-clear abort must still be recorded durably"
        assert marker["failures"] == 1

    async def test_retry_endpoint_keeps_gpu_disabled_until_restart(self, client):
        openvino_session.disable_gpu_backend("failed native context", durable=True)
        response = await client.post("/api/system/gpu/retry")
        assert response.status_code == 200
        assert response.json()["restart_required"] is True
        assert openvino_session.gpu_backend_disabled() == "failed native context"
        assert openvino_session.read_gpu_failure_marker() is None
