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


class TestEmptyWindowIsNotAHardwareFailure:
    """An empty seek window says nothing about the GPU.

    The plates stage takes one short ``-ss t -t 0.5`` seek per tracked vehicle — up to 410
    per recording — and a window that lands past the last frame or inside a damaged GOP
    legitimately yields nothing. That arrived as the same ``DecodeError`` as a driver
    failure, so ``iter_frames`` concluded hardware decode was broken *for that file* and
    forced software decoding for the rest of the process. One unlucky seek cost the iGPU
    the remainder of a recording it had already been decoding on VAAPI for minutes, and the
    Queue page went on reporting "Decoder: vaapi" throughout.
    """

    def test_a_clean_exit_with_no_frames_is_a_window_not_a_device(self):
        from app.hardware.ffmpeg import DecodeError, _is_empty_window

        exc = DecodeError("no frames decoded from clip.ts", stderr="", returncode=0)
        assert _is_empty_window(exc)

    def test_a_failed_decoder_is_still_a_device_failure(self):
        from app.hardware.ffmpeg import DecodeError, _is_empty_window

        assert not _is_empty_window(DecodeError("no frames decoded", stderr="", returncode=1))
        assert not _is_empty_window(
            DecodeError(
                "no frames decoded",
                stderr="Failed to initialise VAAPI connection: -1 (unknown libva error)",
                returncode=0,
            )
        )

    async def test_an_empty_window_does_not_condemn_the_file_to_software(self, monkeypatch):
        """The behaviour that actually cost throughput, asserted end to end."""
        from app.hardware import ffmpeg as ffmpeg_module

        monkeypatch.setattr(ffmpeg_module, "_hwaccel_refused", set())
        monkeypatch.setattr(
            ffmpeg_module, "select_hwaccel", lambda *a, **k: (["-hwaccel", "vaapi"], "vaapi")
        )

        attempts = []

        async def empty_decode(path, **kwargs):
            attempts.append(kwargs.get("hwaccel"))
            raise ffmpeg_module.DecodeError(
                "no frames decoded from clip.ts", stderr="", returncode=0
            )
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(ffmpeg_module, "_decode_frames", empty_decode)

        with pytest.raises(ffmpeg_module.DecodeError):
            async for _ in ffmpeg_module.iter_frames("clip.ts", start=119.9, duration=0.5):
                pass

        assert "clip.ts" not in ffmpeg_module._hwaccel_refused, (
            "an empty seek window was remembered as a hardware failure, so the rest of "
            "this file will be decoded on the CPU"
        )
        assert len(attempts) == 1, (
            "software decode was retried for a window that is empty either way"
        )


class TestProbeIsNotRepeated:
    """Probing is a subprocess against an SMB share; the answer cannot change mid-run.

    The plates stage asked for one frame per tracked vehicle, and every one of those seeks
    fell through to ``probe()`` to re-learn the frame size — up to 410 ffprobe launches per
    recording, each reading both ends of an unindexed MPEG-TS over the network, before the
    ffmpeg process that actually decodes the frame.
    """

    async def test_a_second_probe_of_an_unchanged_file_costs_nothing(self, tmp_path, monkeypatch):

        from app.hardware import ffmpeg as ffmpeg_module

        clip = tmp_path / "probe_me.ts"
        clip.write_bytes(b"\x47" * 4096)

        calls = {"count": 0}

        async def fake_raw(path, **kwargs):
            calls["count"] += 1
            return {
                "format": {"format_name": "mpegts", "duration": "60.0", "bit_rate": "8000000"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "r_frame_rate": "30/1",
                    }
                ],
            }

        ffmpeg_module.clear_probe_cache()
        monkeypatch.setattr(ffmpeg_module, "ffprobe_raw", fake_raw)

        first = await ffmpeg_module.probe(clip)
        second = await ffmpeg_module.probe(clip)

        assert calls["count"] == 1, f"ffprobe ran {calls['count']} times for one unchanged file"
        assert second is first
        assert second.width == 1920

    async def test_a_rewritten_file_is_probed_again(self, tmp_path, monkeypatch):
        """Caching must not outlive the file it describes."""
        import os

        from app.hardware import ffmpeg as ffmpeg_module

        clip = tmp_path / "rewritten.ts"
        clip.write_bytes(b"\x47" * 4096)

        calls = {"count": 0}

        async def fake_raw(path, **kwargs):
            calls["count"] += 1
            return {
                "format": {"format_name": "mpegts", "duration": "60.0"},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "r_frame_rate": "30/1",
                    }
                ],
            }

        ffmpeg_module.clear_probe_cache()
        monkeypatch.setattr(ffmpeg_module, "ffprobe_raw", fake_raw)

        await ffmpeg_module.probe(clip)
        clip.write_bytes(b"\x47" * 8192)
        # Same-second writes can leave mtime unchanged on a coarse clock; make the change
        # unambiguous so this asserts the invalidation rather than the filesystem.
        os.utime(clip, (0, 0))
        await ffmpeg_module.probe(clip)

        assert calls["count"] == 2, "a rewritten file was served from the probe cache"


class TestAProvenFileIsNotDemoted:
    """A GPU that decoded this file once can decode it again.

    This is the case the returncode check does not cover, and it is the one that was
    actually costing throughput. A 62 MB clip decodes on VAAPI for its whole detection
    stage - 134 tracked vehicles, thousands of frames - and then the plates stage takes one
    0.5 s seek per vehicle. One of those exits non-zero having produced nothing, which is a
    fact about that window, and the file was demoted to software decoding for the rest of
    the run while the Queue page went on reporting "Decoder: vaapi".

    Live evidence: 20260731154309_camera_0.ts, 62.4 MB, 65 s, 134 vehicles tracked, then
    "hardware decode failed; using software for this file from now on".
    """

    async def test_a_later_failure_does_not_condemn_a_file_that_already_decoded(self, monkeypatch):
        import numpy as np

        from app.hardware import ffmpeg as ffmpeg_module

        monkeypatch.setattr(ffmpeg_module, "_hwaccel_refused", set())
        monkeypatch.setattr(ffmpeg_module, "_hwaccel_proven", set())
        monkeypatch.setattr(
            ffmpeg_module, "select_hwaccel", lambda *a, **k: (["-hwaccel", "vaapi"], "vaapi")
        )

        state = {"fail": False}

        async def decode(path, **kwargs):
            if state["fail"] and kwargs.get("hwaccel") != "cpu":
                # Non-zero exit, so _is_empty_window deliberately does not apply.
                raise ffmpeg_module.DecodeError(
                    "no frames decoded from clip.ts", stderr="", returncode=1
                )
            yield 0.0, np.zeros((4, 4, 3), dtype=np.uint8)

        monkeypatch.setattr(ffmpeg_module, "_decode_frames", decode)

        # The detection stage: a long successful hardware pass over the whole file.
        async for _ in ffmpeg_module.iter_frames("clip.ts", fps=5.0):
            pass
        assert "clip.ts" in ffmpeg_module._hwaccel_proven

        # The plates stage: one short seek that comes back with nothing.
        state["fail"] = True
        frames = [f async for f in ffmpeg_module.iter_frames("clip.ts", start=64.9, duration=0.5)]

        assert frames, "the software fallback should still answer this particular call"
        assert "clip.ts" not in ffmpeg_module._hwaccel_refused, (
            "one bad 0.5s window demoted a file that had already decoded thousands of "
            "frames on the GPU; the rest of the recording will now run on the CPU"
        )

    async def test_a_file_that_never_decoded_on_hardware_is_still_demoted(self, monkeypatch):
        """The original behaviour has to survive: a genuinely broken GPU path is sticky."""
        from app.hardware import ffmpeg as ffmpeg_module

        monkeypatch.setattr(ffmpeg_module, "_hwaccel_refused", set())
        monkeypatch.setattr(ffmpeg_module, "_hwaccel_proven", set())
        monkeypatch.setattr(
            ffmpeg_module, "select_hwaccel", lambda *a, **k: (["-hwaccel", "vaapi"], "vaapi")
        )

        import numpy as np

        async def decode(path, **kwargs):
            if kwargs.get("hwaccel") != "cpu":
                raise ffmpeg_module.DecodeError(
                    "no frames decoded",
                    stderr="Failed to initialise VAAPI connection",
                    returncode=1,
                )
            yield 0.0, np.zeros((4, 4, 3), dtype=np.uint8)

        monkeypatch.setattr(ffmpeg_module, "_decode_frames", decode)

        frames = [f async for f in ffmpeg_module.iter_frames("broken.ts", fps=1.0)]
        assert frames, "software decode should have answered"
        assert "broken.ts" in ffmpeg_module._hwaccel_refused
