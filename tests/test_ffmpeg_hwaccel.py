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
