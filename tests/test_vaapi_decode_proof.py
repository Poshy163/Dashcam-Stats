"""VAAPI is reported only when hardware surfaces reach the download filter."""

import asyncio
import subprocess
import weakref
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.hardware import detect, ffmpeg


@pytest.fixture(autouse=True)
def isolated_decoder_state(monkeypatch):
    monkeypatch.setattr(ffmpeg, "_hwaccel_refused", set())
    monkeypatch.setattr(ffmpeg, "_hwaccel_proven", set())
    monkeypatch.setattr(ffmpeg, "_vaapi_decode_locks", weakref.WeakKeyDictionary())


@pytest.mark.parametrize(
    "returncode,byte_count,available",
    [(0, 0, False), (0, 10, False), (1, 115200, False), (0, 115200, True)],
)
def test_capability_probe_requires_one_complete_frame_from_hardware(
    monkeypatch, returncode, byte_count, available
):
    monkeypatch.setattr(detect, "_run", lambda *_: SimpleNamespace(returncode=0, stdout=b"clip"))

    def run(command, **kwargs):
        assert command[command.index("-hwaccel_output_format") + 1] == "vaapi"
        assert command[command.index("-vf") + 1] == "hwdownload,format=nv12"
        assert command[command.index("-frames:v") + 1] == "1"
        assert kwargs["input"] == b"clip"
        assert kwargs["timeout"] == detect.PROBE_TIMEOUT_S
        return SimpleNamespace(returncode=returncode, stdout=b"\x00" * byte_count)

    monkeypatch.setattr(detect.subprocess, "run", run)
    info = detect.HardwareInfo(
        ffmpeg_path="ffmpeg", render_nodes=["/dev/dri/renderD128"], ffmpeg_hwaccels=["vaapi"]
    )
    detect._probe_vaapi(info)
    assert info.vaapi_available is available
    assert info.vaapi_decode_codecs == (["h264", "hevc"] if available else [])


def test_profile_rejection_is_preserved_but_benign_verbose_setup_is_not_an_error():
    profile = "[h264] [verbose] Codec h264 profile 66 not supported for hardware decode."
    stderr = (
        "[AVHWDeviceContext] [verbose] Opened VA display\n"
        + profile
        + "\n[h264] [error] Failed setup for format vaapi: hwaccel initialisation returned error.\n"
        + "[h264] [error] Additional error\n" * 200
    )
    result = ffmpeg._decode_error_diagnostics(stderr)
    assert len(result) <= 2000
    assert profile in result
    assert not ffmpeg._is_transient_hwaccel_failure(ffmpeg.DecodeError("no frames", stderr=result))
    benign = ffmpeg._decode_error_diagnostics(
        "[AVHWDeviceContext] [verbose] Initialised vaapi device\n"
    )
    assert ffmpeg.is_empty_window(ffmpeg.DecodeError("no frames", stderr=benign, returncode=0))
    assert ffmpeg._is_transient_hwaccel_failure(
        ffmpeg.DecodeError("busy", stderr="hwaccel initialisation returned error")
    )


async def test_profile_refusal_falls_back_once_without_hardware_proof(monkeypatch):
    attempts, reported = [], []
    monkeypatch.setattr(
        ffmpeg,
        "select_hwaccel",
        lambda preference, _: ([], "software" if preference == "cpu" else "vaapi"),
    )

    async def decode(_path, **kwargs):
        attempts.append(kwargs["hwaccel"])
        if kwargs["hwaccel"] != "cpu":
            raise ffmpeg.DecodeError(
                "no frames",
                returncode=1,
                stderr="Codec h264 profile 66 not supported for hardware decode.\n"
                "Failed setup for format vaapi: hwaccel initialisation returned error.",
            )
        yield 0.0, np.zeros((4, 4, 3), dtype=np.uint8)

    monkeypatch.setattr(ffmpeg, "_decode_frames", decode)
    frames = [
        frame
        async for frame in ffmpeg.iter_frames(
            "baseline.ts", frame_size=(4, 4), hwaccel="vaapi", on_decoder=reported.append
        )
    ]
    assert len(frames) == 1
    assert attempts == ["vaapi", "cpu"]
    assert reported == ["vaapi", "software"]
    assert "baseline.ts" in ffmpeg._hwaccel_refused
    assert "baseline.ts" not in ffmpeg._hwaccel_proven


async def test_decoder_switch_during_gate_wait_cannot_prove_hardware(monkeypatch):
    selections = iter([(["-hwaccel", "vaapi"], "vaapi"), ([], "software")])
    monkeypatch.setattr(ffmpeg, "select_hwaccel", lambda *_: next(selections))
    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: "ffmpeg")
    commands, reported = [], []

    async def spawn(command, **_kwargs):
        commands.append(command)
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(b"\x00" * 48)
        stdout.feed_eof()
        stderr.feed_eof()

        async def wait():
            return 0

        return SimpleNamespace(pid=123456, returncode=0, stdout=stdout, stderr=stderr, wait=wait)

    monkeypatch.setattr(ffmpeg, "_spawn_media", spawn)
    frames = [
        frame
        async for frame in ffmpeg.iter_frames(
            "changed.ts", frame_size=(4, 4), hwaccel="vaapi", on_decoder=reported.append
        )
    ]
    assert len(frames) == 1
    assert "-hwaccel" not in commands[0]
    assert "hwdownload" not in commands[0][commands[0].index("-vf") + 1]
    assert reported == ["vaapi", "software"]
    assert "changed.ts" not in ffmpeg._hwaccel_proven


async def test_real_ffmpeg_software_frames_fail_hardware_graph_then_fall_back(
    ffmpeg_path, tmp_path, monkeypatch
):
    video = tmp_path / "software.mkv"
    subprocess.run(
        [
            ffmpeg_path,
            "-hide_banner",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=0.5",
            "-c:v",
            "ffv1",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    # Model FFmpeg's implicit CPU fallback: the VAAPI branch receives software frames.
    # No GPU is needed to prove that its strict download graph rejects those frames.
    monkeypatch.setattr(
        ffmpeg,
        "select_hwaccel",
        lambda preference, _: ([], "software" if preference == "cpu" else "vaapi"),
    )
    reported = []
    frames = [
        frame
        async for frame in ffmpeg.iter_frames(
            video, frame_size=(64, 48), hwaccel="vaapi", on_decoder=reported.append
        )
    ]
    assert len(frames) == 5
    assert all(frame.shape == (48, 64, 3) for _, frame in frames)
    assert reported == ["vaapi", "software"]
    assert str(video) in ffmpeg._hwaccel_refused
    assert str(video) not in ffmpeg._hwaccel_proven


@pytest.mark.parametrize("raise_error", [False, True])
async def test_thumbnail_retries_software_and_publishes_only_a_new_image(
    tmp_path, monkeypatch, raise_error
):
    out = tmp_path / "thumb.jpg"
    out.write_bytes(b"previous")
    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpeg, "select_hwaccel", lambda *_: (["-hwaccel", "vaapi"], "vaapi"))
    commands = []

    async def run(command, _timeout):
        commands.append(command)
        target = Path(command[-1])
        assert target != out
        assert out.read_bytes() == b"previous"
        chain = command[command.index("-vf") + 1]
        if len(commands) == 1:
            assert chain.startswith("hwdownload,format=nv12,")
            target.write_bytes(b"partial")
            if raise_error:
                raise ffmpeg.DecodeError("hardware timed out")
            return 1, b"", b"unsupported profile"
        assert "-hwaccel" not in command and "hwdownload" not in chain
        target.write_bytes(b"new thumbnail")
        return 0, b"", b""

    monkeypatch.setattr(ffmpeg, "_run", run)
    assert await ffmpeg.write_thumbnail("clip.ts", out)
    assert len(commands) == 2
    assert out.read_bytes() == b"new thumbnail"
    assert list(tmp_path.iterdir()) == [out]


async def test_empty_success_does_not_accept_or_delete_an_old_thumbnail(tmp_path, monkeypatch):
    out = tmp_path / "thumb.jpg"
    out.write_bytes(b"previous")
    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpeg, "select_hwaccel", lambda *_: ([], "software"))

    async def run(*_args):
        return 0, b"", b""

    monkeypatch.setattr(ffmpeg, "_run", run)
    assert not await ffmpeg.write_thumbnail("clip.ts", out)
    assert out.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [out]


@pytest.mark.parametrize(
    "reason", ["the Intel media slot is unhealthy", "inference is running on the Intel iGPU"]
)
async def test_thumbnail_policy_change_while_waiting_never_launches_hardware(
    tmp_path, monkeypatch, reason
):
    state = {"reason": None}

    class Gate:
        async def __aenter__(self):
            state["reason"] = reason

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(ffmpeg, "select_hwaccel", lambda *_: (["-hwaccel", "vaapi"], "vaapi"))
    monkeypatch.setattr(ffmpeg, "_vaapi_decode_lock", Gate)
    monkeypatch.setattr(ffmpeg, "software_decode_reason", lambda: state["reason"])
    commands = []

    async def run(command, *_args, **_kwargs):
        commands.append(command)
        assert "-hwaccel" not in command
        assert "hwdownload" not in command[command.index("-vf") + 1]
        Path(command[-1]).write_bytes(b"thumbnail")
        return 0, b"", b""

    monkeypatch.setattr(ffmpeg, "_run_process", run)
    out = tmp_path / "thumb.jpg"
    assert await ffmpeg.write_thumbnail("clip.ts", out)
    assert len(commands) == 1
    assert out.read_bytes() == b"thumbnail"
