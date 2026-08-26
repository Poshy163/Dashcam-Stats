"""Locating the overlay strip within a frame.

Regions are stored as *fractions* of the frame, never pixels, so one profile keeps
working when a camera records at a different resolution. Measured on the real corpus, the
overlay occupies y=1040..1072 of a 1080-line frame — fractions 0.963 to 0.993 — and the
default profile pads that generously in both directions.

The region is a *setting*, not something the application derives. Automatic calibration
was written -- a bottom-anchored row-ink search that verified a candidate band really was
text by segmenting it and checking the glyphs shared a cap height -- and then never wired
to anything: no caller, no API route, and the ``telemetry.auto_calibrate`` switch that was
meant to gate it was read by nothing while appearing in the UI as a working control. About
ninety-five lines of unreachable code and an inert setting are worse than an honest gap, so
both are gone. A camera whose overlay sits elsewhere is configured through
``osd_profiles``; if calibration is wanted back, it needs a caller and a way for the
operator to accept or reject what it found, and the history holds the implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger
from app.hardware.ffmpeg import Crop

log = get_logger(__name__)

#: Defaults matching the observed overlay, with padding for firmware variation.
#: Measured directly on the corpus: the overlay occupies y=1040..1072 of a 1080-line
#: frame. This crop is 1030..1080 -- tight enough to exclude the scene above the text
#: (headlights and bright sky break glyph segmentation at night) while leaving margin
#: for firmware that positions the overlay a few pixels differently.
DEFAULT_REGION = (0.0, 0.9537, 1.0, 0.0463)


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
