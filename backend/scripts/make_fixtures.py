"""Generate small synthetic dashcam clips for the test suite.

The tests need footage in the *exact* shape the real dashcam produces — MPEG-TS,
H.264 Baseline, 1920x1080, with a burned-in 1 Hz telemetry overlay — but the real
corpus is the user's own driving: their home location, their movements, and other
people's licence plates. None of that belongs in a repository. So the fixtures are
synthesised to match the format rather than copied from the share.

What is reproduced faithfully, because the code under test depends on it:

* container/codec/profile/pixel format and the ``YYYYMMDDHHMMSS_camera_N.ts`` naming
* front = camera_0 at 30 fps with audio, rear = camera_1 at 25 fps without
* the overlay layout ``YYYY-MM-DD HH:MM:SS   E:<lon> N:<lat>  <speed> km/h``,
  redrawn once per second and never faster
* ``E:00.0000 N:00.0000`` for the no-fix case
* the damaged files that genuinely exist in the corpus: zero-byte, truncated, garbage

Usage::

    python backend/scripts/make_fixtures.py --out tests/fixtures [--ffmpeg /path/to/ffmpeg]

Clips are a few seconds at a low bitrate, so the whole set stays well under a megabyte.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Deliberately not the user's real location. Central Adelaide is a public landmark and
# keeps the coordinate magnitudes/signs identical to the real data (negative latitude,
# positive longitude), which is what the parser actually cares about.
BASE_LAT = -34.9285
BASE_LON = 138.6007


@dataclass(frozen=True)
class ClipSpec:
    name: str
    camera: int
    start: datetime
    seconds: int
    fps: int
    with_audio: bool
    gps: bool
    speed_kmh_start: int = 0
    speed_kmh_end: int = 0
    width: int = 1920
    height: int = 1080


def find_ffmpeg(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if not found:
        sys.exit("ffmpeg not found on PATH; pass --ffmpeg")
    return found


def _escape(text: str) -> str:
    """Escape a literal for ffmpeg drawtext, which treats : and \\ specially."""
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def build_overlay_filters(spec: ClipSpec) -> list[str]:
    """One drawtext per second, gated by `between(t,...)`.

    Redrawing per second rather than per frame is not a shortcut — it is the behaviour
    under test. The real overlay only changes once a second, which is why the telemetry
    extractor samples at 1 fps and why sampling faster would be wasted work.
    """
    # The real overlay is white text over the car's bonnet and road surface — dark, low
    # contrast at the edges. Darkening the strip keeps the OCR fixtures representative;
    # over testsrc2's raw colour bars the text would sit on yellow, which never happens.
    filters: list[str] = ["drawbox=x=0:y=ih-56:w=iw:h=56:color=black@0.72:t=fill"]
    for i in range(spec.seconds):
        stamp = spec.start + timedelta(seconds=i)
        if spec.gps:
            # A gentle north-east drift, large enough to exceed the ~11 m quantisation
            # so distance/heading derivation has something real to work with.
            lat = BASE_LAT + i * 0.00035
            lon = BASE_LON + i * 0.00045
            frac = i / max(1, spec.seconds - 1)
            speed = round(spec.speed_kmh_start + (spec.speed_kmh_end - spec.speed_kmh_start) * frac)
            coords = f"E:{lon:.4f} N:{lat:.4f}"
        else:
            # The genuine no-fix marker. Must never be read as coordinates (0, 0).
            coords = "E:00.0000 N:00.0000"
            speed = 0

        text = f"{stamp:%Y-%m-%d %H:%M:%S}   {coords}  {speed} km/h"
        filters.append(
            "drawtext="
            + ":".join(
                [
                    f"text='{_escape(text)}'",
                    "fontcolor=white",
                    "fontsize=34",
                    "x=12",
                    "y=h-46",
                    "box=0",
                    f"enable='between(t,{i},{i + 1})'",
                ]
            )
        )
    return filters


def render(ffmpeg: str, spec: ClipSpec, out: Path) -> None:
    # A moving synthetic scene rather than a flat colour, so detection code has
    # something with edges and motion to chew on.
    src = f"testsrc2=size={spec.width}x{spec.height}:rate={spec.fps}:duration={spec.seconds}"
    vf = ",".join([*build_overlay_filters(spec), "format=yuvj420p"])

    args = [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", src]
    if spec.with_audio:
        # Matches the real front camera: AAC-LC, 8 kHz, mono, very low bitrate.
        args += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=8000:duration={spec.seconds}",
        ]

    args += [
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",  # the real cameras encode Baseline, no B-frames
        "-level",
        "4.0",
        "-pix_fmt",
        "yuvj420p",
        "-g",
        "25",
        "-b:v",
        "600k",
        "-r",
        str(spec.fps),
    ]
    if spec.with_audio:
        args += ["-c:a", "aac", "-b:a", "16k", "-ac", "1", "-ar", "8000"]
    else:
        args += ["-an"]
    args += ["-f", "mpegts", str(out)]

    proc = subprocess.run(args, capture_output=True)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg failed for {out.name}: {proc.stderr.decode()[:800]}")


def make_damaged(outdir: Path, ffmpeg: str) -> list[str]:
    """Recreate the failure modes that actually occur in the corpus."""
    made: list[str] = []

    # Zero-byte segments: 3 of these exist on the real share.
    zero = outdir / "20260807214113_camera_0.ts"
    zero.write_bytes(b"")
    made.append(zero.name)

    # Not a transport stream at all — probe must fail cleanly, not hang.
    garbage = outdir / "20260807214114_camera_0.ts"
    garbage.write_bytes(os.urandom(64_000))
    made.append(garbage.name)

    # Truncated mid-GOP: header parses, decode dies partway. This is the case that must
    # not stall the queue.
    full = outdir / "_tmp_truncate.ts"
    render(
        ffmpeg,
        ClipSpec("tmp", 0, datetime(2026, 8, 7, 21, 42, 0), 4, 30, False, True, 40, 45),
        full,
    )
    data = full.read_bytes()
    truncated = outdir / "20260807214200_camera_0.ts"
    truncated.write_bytes(data[: len(data) // 3])
    full.unlink()
    made.append(truncated.name)

    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="tests/fixtures", type=Path)
    ap.add_argument("--ffmpeg", default=None)
    args = ap.parse_args()

    ffmpeg = find_ffmpeg(args.ffmpeg)
    outdir: Path = args.out
    outdir.mkdir(parents=True, exist_ok=True)

    base = datetime(2026, 8, 4, 17, 43, 53)

    # The timestamps below are chosen so that between them, the year/month/day/hour
    # fields contain every digit 0-9. That is not cosmetic: the telemetry engine learns
    # its glyph templates by labelling those fields, and a digit that never appears in
    # them is never learned. An unlearned digit does not decode as "unknown" — it decodes
    # as whichever known glyph looks nearest, with a high confidence score — so a fixture
    # set missing a 3 or a 9 silently produces zero usable telemetry.
    specs = [
        # A front/rear pair from one drive. The rear filename is deliberately one second
        # off the front, because in the real corpus they never match exactly.
        ClipSpec("front_gps", 0, base, 6, 30, True, True, 48, 64),
        ClipSpec("rear_gps", 1, base + timedelta(seconds=1), 6, 25, False, True, 48, 64),
        # Contiguous next segment — the journey builder should join this to the pair above.
        ClipSpec("front_gps_next", 0, base + timedelta(seconds=6), 6, 30, True, True, 64, 70),
        # Parked with no satellite fix: the 0.0000/0.0000 case.
        ClipSpec("front_nofix", 0, datetime(2026, 8, 5, 15, 3, 58), 5, 30, True, False),
        # A different day and hour, so it forms its own journey — and supplies the 3 and 9
        # that no other fixture's stable fields contain.
        ClipSpec("front_other_day", 0, datetime(2026, 8, 3, 9, 15, 0), 4, 30, True, True, 30, 35),
    ]

    written: list[str] = []
    for spec in specs:
        fname = f"{spec.start:%Y%m%d%H%M%S}_camera_{spec.camera}.ts"
        target = outdir / fname
        render(ffmpeg, spec, target)
        written.append(f"{fname} ({target.stat().st_size / 1024:.0f} KB)")

    written += [f"{n} (damaged)" for n in make_damaged(outdir, ffmpeg)]

    total = sum(p.stat().st_size for p in outdir.glob("*.ts"))
    print(f"Wrote {len(written)} fixtures to {outdir} ({total / 1024:.0f} KB total)")
    for w in written:
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
