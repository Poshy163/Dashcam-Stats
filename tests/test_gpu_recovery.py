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

    def test_a_corrupt_marker_is_not_a_crash(self, tmp_path):
        """A half-written marker must not take down the status endpoint that exists to
        explain why the GPU is off."""
        (tmp_path / "gpu.json").write_text("{not json", "utf-8")
        assert openvino_session.read_gpu_failure_marker() is None


class TestClearingTheVerdict:
    def test_clearing_removes_the_marker_and_the_reason(self, tmp_path):
        openvino_session.disable_gpu_backend("CL_OUT_OF_RESOURCES", durable=True)
        assert openvino_session.gpu_backend_disabled() is not None
        assert openvino_session.clear_gpu_failure_state() is True
        assert openvino_session.gpu_backend_disabled() is None
        assert openvino_session.read_gpu_failure_marker() is None

    def test_clearing_rearms_persistence_for_the_next_abort(self, tmp_path):
        """The reason and the persisted flag are cleared together on purpose. If the flag
        survived, the next abort in this same process would find itself already 'written',
        skip the marker, and leave the restart nothing to read -- re-arming a chip that
        just aborted twice."""
        openvino_session.disable_gpu_backend("first", durable=True)
        openvino_session.clear_gpu_failure_state()
        openvino_session.disable_gpu_backend("second", durable=True)
        marker = openvino_session.read_gpu_failure_marker()
        assert marker is not None, "a post-clear abort must still be recorded durably"
        assert marker["failures"] == 1
