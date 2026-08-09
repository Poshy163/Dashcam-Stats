"""The hardware-acceleration flags and the filter graph have to agree.

This exists because they once did not, and the result was silent. `select_hwaccel` asked
the decoder to hand frames back in system memory (`-hwaccel_output_format nv12`) while
`build_filter_chain` prepended `hwdownload`, a filter that only accepts *hardware* frames.
The graph failed to configure, every accelerated decode raised, and the telemetry stage
recorded zero points across an entire library while every other stage reported success.

Nothing caught it: the bug only appears when VAAPI genuinely works, so a development
machine with no /dev/dri and a CI runner with no GPU both sail past. These tests assert
the *contract* between the two functions instead, which needs no GPU at all.
"""

from __future__ import annotations

import pytest

from app.hardware.ffmpeg import Crop, build_filter_chain, select_hwaccel


class TestFilterChainContract:
    @pytest.mark.parametrize("label", ["vaapi", "qsv", "software"])
    def test_chain_never_downloads_frames_itself(self, label):
        """`hwdownload` must not appear while the decoder already downloads.

        Whichever half changes, the other has to change with it -- this is the assertion
        that makes that impossible to forget.
        """
        chain = build_filter_chain(fps=1.0, hwaccel_label=label, pix_fmt="gray")
        assert "hwdownload" not in chain, (
            f"{label} chain injects hwdownload while -hwaccel_output_format already "
            "returns software frames; the graph will fail to configure"
        )

    def test_hwaccel_args_request_a_software_output_format(self):
        """If this ever emits vaapi surfaces, the chain must download them again."""
        args, label = select_hwaccel("auto", "h264")
        if label == "software":
            pytest.skip("no VAAPI on this machine; the contract is asserted above")
        assert "-hwaccel_output_format" in args
        fmt = args[args.index("-hwaccel_output_format") + 1]
        assert fmt != "vaapi", (
            "decoder is emitting GPU surfaces, so build_filter_chain must hwdownload them"
        )

    def test_cpu_preference_never_requests_hardware(self):
        args, label = select_hwaccel("cpu", "h264")
        assert args == []
        assert label == "software"

    def test_chain_orders_filters_so_only_wanted_pixels_are_copied(self):
        # fps before crop: resampling first means the crop runs on far fewer frames, and
        # cropping in-graph is what keeps a 1920x50 telemetry strip off the pipe as a
        # full 1080p frame.
        chain = build_filter_chain(
            fps=1.0, crop=Crop(0, 1030, 1920, 50), hwaccel_label="vaapi", pix_fmt="gray"
        )
        assert chain.index("fps=") < chain.index("crop=")
        assert chain.endswith("format=gray")

    def test_pixel_format_is_last(self):
        """The output format must be the final conversion or the raw byte layout differs
        from what the reader unpacks into numpy."""
        chain = build_filter_chain(scale=(480, -2), hwaccel_label="software")
        assert chain.split(",")[-1] == "format=bgr24"


class TestDecodeFallback:
    def test_iter_frames_wraps_a_single_attempt(self):
        """`iter_frames` is the public entry point and adds the software retry.

        Stages must not call `_decode_frames` directly, or a hardware failure becomes a
        stage failure instead of a slower success.
        """
        from app.hardware import ffmpeg

        assert hasattr(ffmpeg, "_decode_frames")
        assert ffmpeg.iter_frames is not ffmpeg._decode_frames

    def test_no_stage_bypasses_the_fallback(self):
        """Grep the pipeline for direct use of the un-wrapped decoder."""
        from pathlib import Path

        backend = Path(__file__).resolve().parent.parent / "backend" / "app"
        offenders = [
            path.relative_to(backend).as_posix()
            for path in backend.rglob("*.py")
            if path.name != "ffmpeg.py" and "_decode_frames" in path.read_text(encoding="utf-8")
        ]
        assert not offenders, f"these bypass the software fallback: {offenders}"


class TestPtsWrapClamp:
    """The two files this clamp was written for, and why it never caught them.

    MPEG-TS timestamps are 33 bits at 90 kHz, so they wrap every ~95,443 s. When they do,
    the last timestamp is *smaller* than the first and the container reports
    ``wrap - elapsed`` — a one-minute clip comes back as 95,376 s, just under the wrap
    period rather than over it. The original guard asked for a duration at or beyond the
    wrap point, which nothing ever reports, so it never fired.

    Real values, measured from the deployment:

        20260804085744_camera_0.ts  duration 95376.184  size 9,568,256  bit_rate 802
        20260804085744_camera_1.ts  duration 95377.157  size 10,878,976 bit_rate 912

    Between them they contributed 190,753 of the 260,817 s the dashboard called the
    library's footage — 72 hours claimed against about 19 real ones — and produced a
    26-hour "journey" that overlapped the one after it.
    """

    def test_the_threshold_sits_below_the_wrap_period(self):
        from app.hardware.ffmpeg import PTS_WRAP_SECONDS, PTS_WRAP_THRESHOLD_S

        assert PTS_WRAP_THRESHOLD_S < PTS_WRAP_SECONDS, (
            "a wrapped duration lands under the wrap period, so a threshold at or above "
            "it can never fire"
        )

    @pytest.mark.parametrize("duration", [95376.184456, 95377.156767])
    def test_the_real_wrapped_durations_are_caught(self, duration):
        from app.hardware.ffmpeg import PTS_WRAP_THRESHOLD_S

        assert duration >= PTS_WRAP_THRESHOLD_S

    @pytest.mark.parametrize("duration", [59.9, 67.5, 120.0, 3600.0])
    def test_ordinary_segments_are_untouched(self, duration):
        from app.hardware.ffmpeg import PTS_WRAP_THRESHOLD_S

        assert duration < PTS_WRAP_THRESHOLD_S

    @pytest.mark.parametrize(
        ("size_bytes", "bitrate"),
        [(9568256, 802), (10878976, 912)],
    )
    def test_a_bitrate_derived_from_the_bad_duration_is_refused(self, size_bytes, bitrate):
        """The recovery path's own trap.

        ffprobe computes the bitrate from the duration, so on these files both are wrong
        together. Reconstructing the duration as ``size * 8 / bitrate`` returns ~95,441 s —
        the wrap period again — which is inside any naive "is it less than the wrap point"
        check and would have been stored as the answer.
        """
        from app.hardware.ffmpeg import MIN_PLAUSIBLE_BITRATE, PTS_WRAP_THRESHOLD_S

        reconstructed = size_bytes * 8 / bitrate
        assert reconstructed > PTS_WRAP_THRESHOLD_S, "this bitrate reproduces the bad duration"
        assert bitrate < MIN_PLAUSIBLE_BITRATE, "and must therefore be refused as evidence"

    def test_a_genuine_bitrate_is_still_usable(self):
        from app.hardware.ffmpeg import MIN_PLAUSIBLE_BITRATE

        # The healthy files in this corpus run about 13 Mbps.
        assert MIN_PLAUSIBLE_BITRATE <= 13_000_000


class TestInferenceProviders:
    """Naming the OpenVINO provider is not the same as reaching the iGPU.

    ``OpenVINOExecutionProvider`` takes a ``device_type`` and, left unset, compiles for the
    CPU. The provider then shows up in diagnostics and everything looks accelerated while
    the GPU sits idle — which is exactly what a real deployment showed: intel_gpu_top with
    a quiet render engine and a pinned Python process, pacing a 674-recording queue at
    around three minutes each.
    """

    def test_the_device_is_requested_explicitly(self, monkeypatch):
        import app.ai.runtime as runtime

        runtime.onnx_providers.cache_clear()
        monkeypatch.setattr(runtime, "_openvino_device", lambda: "GPU")

        class _Ort:
            @staticmethod
            def get_available_providers():
                return ["OpenVINOExecutionProvider", "CPUExecutionProvider"]

        monkeypatch.setitem(__import__("sys").modules, "onnxruntime", _Ort)
        try:
            providers = runtime.onnx_providers()
        finally:
            runtime.onnx_providers.cache_clear()

        first = providers[0]
        assert isinstance(first, tuple), "the provider must carry options, not be a bare name"
        assert first[0] == "OpenVINOExecutionProvider"
        assert first[1]["device_type"] == "GPU", (
            "without device_type the provider silently compiles for the CPU"
        )

    def test_a_missing_device_falls_back_rather_than_failing(self, monkeypatch):
        """Naming a device OpenVINO cannot see fails session creation outright.

        That would take the whole feature down instead of merely making it slower, so an
        absent device means falling through to the next provider.
        """
        import app.ai.runtime as runtime

        runtime.onnx_providers.cache_clear()
        monkeypatch.setattr(runtime, "_openvino_device", lambda: None)

        class _Ort:
            @staticmethod
            def get_available_providers():
                return ["OpenVINOExecutionProvider", "CPUExecutionProvider"]

        monkeypatch.setitem(__import__("sys").modules, "onnxruntime", _Ort)
        try:
            providers = runtime.onnx_providers()
        finally:
            runtime.onnx_providers.cache_clear()

        assert providers == ("CPUExecutionProvider",)

    def test_diagnostics_report_the_device_not_just_the_provider(self, monkeypatch):
        # "OpenVINOExecutionProvider" alone does not say whether the work is on the iGPU or
        # back on the CPU, which is the only thing anyone wants to know from this.
        import app.ai.runtime as runtime

        runtime.onnx_providers.cache_clear()
        monkeypatch.setattr(runtime, "_openvino_device", lambda: "GPU")

        class _Ort:
            @staticmethod
            def get_available_providers():
                return ["OpenVINOExecutionProvider", "CPUExecutionProvider"]

        monkeypatch.setitem(__import__("sys").modules, "onnxruntime", _Ort)
        try:
            described = runtime.describe_runtime()
        finally:
            runtime.onnx_providers.cache_clear()

        assert described["device"] == "GPU"
        assert described["accelerated"] is True


class TestReportedDevice:
    """The Queue page's device column has to be readable.

    A provider entry is a ``(name, options)`` pair once it carries a device, so returning
    it whole wrote the entire tuple repr into the column -- accurate and unreadable.
    """

    def test_the_device_is_a_plain_name(self, monkeypatch):
        import app.ai.runtime as runtime
        from app.ai.detector import ObjectDetector

        runtime.onnx_providers.cache_clear()
        monkeypatch.setattr(
            runtime,
            "onnx_providers",
            lambda: (("OpenVINOExecutionProvider", {"device_type": "GPU"}), "CPUExecutionProvider"),
        )
        import app.ai.detector as detector_module

        monkeypatch.setattr(detector_module, "onnx_providers", runtime.onnx_providers)

        detector = ObjectDetector()
        detector._detector = object()  # stand in for a loaded session
        assert detector.device == "GPU"

    def test_a_bare_provider_name_still_works(self, monkeypatch):
        import app.ai.detector as detector_module
        from app.ai.detector import ObjectDetector

        monkeypatch.setattr(detector_module, "onnx_providers", lambda: ("CPUExecutionProvider",))
        detector = ObjectDetector()
        detector._detector = object()
        assert detector.device == "CPUExecutionProvider"
