"""Locating the overlay strip within a frame.

Regions are stored as *fractions* of the frame, never pixels, so one profile keeps
working when a camera records at a different resolution. Measured on the real corpus, the
overlay occupies y=1040..1072 of a 1080-line frame — fractions 0.963 to 0.993 — and the
default profile pads that generously in both directions.

Calibration exists because a different camera or firmware will put the text somewhere
else. It is deliberately anchored to the bottom of the frame: a naive "brightest rows"
search finds the sky, headlights or a bright building long before it finds the overlay,
which was observed on real daytime frames during development.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.core.logging import get_logger
from app.hardware.ffmpeg import Crop
from app.osd.glyphs import binarise, segment_glyphs

log = get_logger(__name__)

#: Defaults matching the observed overlay, with padding for firmware variation.
DEFAULT_REGION = (0.0, 0.94, 1.0, 0.06)

#: Calibration never looks above this fraction of the frame. The overlay is always
#: bottom-anchored, and searching higher only invites false positives from the scene.
_SEARCH_FROM = 0.80

#: A candidate band must contain at least this many glyph-shaped components to count as
#: text. Scene highlights produce a few large blobs; a line of overlay text produces many
#: small ones of consistent height, which is what actually distinguishes them.
_MIN_GLYPHS = 12


@dataclass(frozen=True, slots=True)
class OsdRegion:
    """Fractional crop rectangle, resolvable against any frame size."""

    x: float = DEFAULT_REGION[0]
    y: float = DEFAULT_REGION[1]
    w: float = DEFAULT_REGION[2]
    h: float = DEFAULT_REGION[3]

    def to_crop(self, width: int, height: int) -> Crop:
        """Resolve to pixels, clamped to the frame and aligned to even coordinates.

        Even alignment matters: ffmpeg's crop filter on chroma-subsampled formats rejects
        or silently shifts odd offsets on some pixel formats.
        """
        cx = max(0, min(width - 2, int(self.x * width) & ~1))
        cy = max(0, min(height - 2, int(self.y * height) & ~1))
        cw = max(2, min(width - cx, int(self.w * width) & ~1))
        ch = max(2, min(height - cy, int(self.h * height) & ~1))
        return Crop(x=cx, y=cy, width=cw, height=ch)

    @classmethod
    def from_pixels(cls, x: int, y: int, w: int, h: int, width: int, height: int) -> OsdRegion:
        return cls(x=x / width, y=y / height, w=w / width, h=h / height)


def _band_score(mask: np.ndarray) -> tuple[int, float]:
    """How text-like a candidate band is: (glyph count, height consistency)."""
    glyphs = segment_glyphs(mask)
    if len(glyphs) < _MIN_GLYPHS:
        return len(glyphs), 0.0
    heights = np.array([g.height for g in glyphs], dtype=np.float32)
    tall = heights[heights >= heights.max() * 0.6]
    if tall.size < _MIN_GLYPHS // 2:
        return len(glyphs), 0.0
    # Real text has many glyphs sharing one cap height; scene clutter does not.
    consistency = float(1.0 - min(1.0, tall.std() / max(1.0, tall.mean())))
    return len(glyphs), consistency


def calibrate(frames: list[np.ndarray], *, pad: int = 8) -> OsdRegion | None:
    """Find the overlay strip from sample frames, or None if no text band is found.

    Works bottom-up over row-ink profiles rather than searching for the brightest rows,
    then verifies the candidate really is text by segmenting it and checking the glyphs
    share a cap height.
    """
    if not frames:
        return None

    accepted: list[tuple[int, int, int, int]] = []
    for frame in frames:
        gray = frame[..., 0] if frame.ndim == 3 else frame
        height, width = gray.shape[:2]
        start = int(height * _SEARCH_FROM)
        lower = gray[start:]
        if lower.size == 0:
            continue

        mask = binarise(lower)
        row_ink = mask.sum(axis=1)
        if row_ink.max() == 0:
            continue

        # Rows carrying a plausible amount of text ink. The floor is relative so it
        # adapts to night frames without letting a single bright blob dominate.
        threshold = max(8.0, row_ink.max() * 0.15)
        rows = np.flatnonzero(row_ink >= threshold)
        if rows.size == 0:
            continue

        # Take the lowest contiguous run — the overlay sits below any scene content that
        # happens to be bright.
        breaks = np.flatnonzero(np.diff(rows) > 3)
        run_start = rows[breaks[-1] + 1] if breaks.size else rows[0]
        run_end = rows[-1]
        if run_end - run_start < 6:
            continue

        band = mask[run_start : run_end + 1]
        count, consistency = _band_score(band)
        if count < _MIN_GLYPHS or consistency < 0.5:
            continue

        cols = np.flatnonzero(band.any(axis=0))
        if cols.size == 0:
            continue

        accepted.append(
            (
                max(0, int(cols[0]) - pad),
                max(0, start + int(run_start) - pad),
                min(width, int(cols[-1]) + 1 + pad),
                min(height, start + int(run_end) + 1 + pad),
            )
        )

    if not accepted:
        return None

    # Union across frames: the text width changes with the values shown (speed is one to
    # three digits), so one frame alone would crop a narrower strip than needed.
    x0 = min(a[0] for a in accepted)
    y0 = min(a[1] for a in accepted)
    x1 = max(a[2] for a in accepted)
    y1 = max(a[3] for a in accepted)

    sample = frames[0]
    gray = sample[..., 0] if sample.ndim == 3 else sample
    height, width = gray.shape[:2]
    region = OsdRegion.from_pixels(x0, y0, x1 - x0, y1 - y0, width, height)
    log.info(
        "calibrated OSD region",
        frames=len(frames),
        accepted=len(accepted),
        pixels=f"{x0},{y0} {x1 - x0}x{y1 - y0}",
        fractions=f"x={region.x:.4f} y={region.y:.4f} w={region.w:.4f} h={region.h:.4f}",
    )
    return region
