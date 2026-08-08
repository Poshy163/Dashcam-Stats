"""Glyph segmentation, classification and self-supervised template learning.

The overlay font is fixed and the text is high-contrast, so template matching beats a
general OCR model here on both accuracy and speed — there is no need to run a neural
network over a 1920x50 strip to read digits drawn from a single known typeface.

The interesting problem is getting templates without shipping a font. This module learns
them from the footage itself, exploiting the one thing that is known a priori: the
timestamp field has a rigid ``YYYY-MM-DD HH:MM:SS`` layout.

* Separators are learned by **position** — glyph 4 and 7 are always ``-``, glyph 12 and
  15 are always ``:``, whatever the time happens to be.
* Digits are learned from the **slow-moving fields** (year, month, day, hour). Those are
  predictable from the recording's start time, and unlike minutes and seconds they do not
  change if the camera clock is a few seconds off the filename. Across a corpus spanning
  several dates and times of day, all ten digits appear in those positions.

Minutes and seconds are deliberately never used as labels: a two-second clock skew would
silently teach the classifier that a ``7`` is a ``9``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from app.core.logging import get_logger

log = get_logger(__name__)

#: Grid every glyph is resampled onto before correlation. Tall and narrow to match the
#: font's aspect; big enough to separate 8 from B, small enough to stay cheap.
TEMPLATE_SHAPE = (24, 16)

#: Glyph indices within the timestamp field. ``2026-08-04 17:44:38`` segments into 18
#: glyphs (spaces produce no ink), and these positions never move.
_SEP_DASH_INDICES = (4, 7)
_SEP_COLON_INDICES = (12, 15)
#: (glyph index, attribute of datetime, digit position) for the fields that do not drift.
_STABLE_DIGIT_SLOTS: tuple[tuple[int, str, int], ...] = (
    (0, "year", 0),
    (1, "year", 1),
    (2, "year", 2),
    (3, "year", 3),
    (5, "month", 0),
    (6, "month", 1),
    (8, "day", 0),
    (9, "day", 1),
    (10, "hour", 0),
    (11, "hour", 1),
)
TIMESTAMP_GLYPH_COUNT = 18

#: Trailing "km/h" is the other fixed anchor — always the last four glyphs on the line.
_TRAILING_LITERALS = ("k", "m", "/", "h")

_MIN_SAMPLES_PER_CHAR = 3
_BRIGHT_FLOOR = 170


@dataclass(slots=True)
class Glyph:
    x0: int
    x1: int
    y0: int
    y1: int
    bitmap: np.ndarray

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0


def binarise(strip: np.ndarray) -> np.ndarray:
    """Threshold the overlay strip to a boolean text mask.

    A fixed bright floor rather than pure Otsu: Otsu assumes a bimodal histogram, which
    holds at night but not at midday when the bonnet below the text is itself bright.
    Otsu is used only to *raise* the floor when the whole strip is washed out.
    """
    if strip.ndim == 3:
        strip = strip[..., 0]
    floor = _BRIGHT_FLOOR
    hi = int(strip.max())
    if hi <= floor:
        # Dim strip (heavy underexposure) — fall back to a relative threshold so the text
        # is still separated rather than losing the whole frame.
        floor = max(60, int(hi * 0.75))
    else:
        otsu = _otsu_threshold(strip)
        floor = max(floor, min(otsu, hi - 10))
    return strip >= floor


def _otsu_threshold(image: np.ndarray) -> int:
    hist = np.bincount(image.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return _BRIGHT_FLOOR
    omega = np.cumsum(hist) / total
    mu = np.cumsum(hist * np.arange(256)) / total
    mu_t = mu[-1]
    denom = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        sigma_b = np.where(denom > 0, (mu_t * omega - mu) ** 2 / denom, 0.0)
    return int(np.argmax(sigma_b))


def segment_glyphs(
    mask: np.ndarray,
    *,
    min_height: int = 2,
    max_height: int = 60,
    min_width: int = 2,
    max_width: int = 60,
    min_ink: int = 8,
    gap: int = 0,
) -> list[Glyph]:
    """Split the mask into glyphs using vertical column projection.

    The font never lets adjacent glyphs touch, so a column with no ink is a reliable
    boundary. This is both simpler and markedly faster than connected components, and it
    keeps ``:`` and ``.`` in one piece — their two blobs share columns, so a
    component-based split would tear them apart and produce phantom characters.

    The size bounds are deliberately permissive in height. ``-`` is drawn only two pixels
    tall and ``.`` barely more; a height floor set for digits silently deletes both, and
    losing them does far more damage than admitting a speck of noise — every glyph after
    a dropped separator shifts position, which corrupts the positional labelling that
    template learning depends on. Noise is rejected on total ink instead, which
    distinguishes a separator from a speck without assuming glyphs are tall.
    """
    if mask.ndim != 2:
        raise ValueError("expected a 2-D mask")

    columns = mask.any(axis=0)
    glyphs: list[Glyph] = []
    start: int | None = None
    blank = 0

    for x in range(len(columns) + 1):
        inked = bool(columns[x]) if x < len(columns) else False
        if inked:
            if start is None:
                start = x
            blank = 0
            continue
        if start is None:
            continue
        blank += 1
        # ``gap`` defaults to 0: any blank column ends the glyph. This font sets adjacent
        # characters exactly one pixel apart, so tolerating even a single blank column
        # silently welds neighbours together — which reads "2026" as three glyphs and
        # poisons both the learned templates and every decode that uses them.
        if blank <= gap and x < len(columns):
            continue

        end = x - blank + 1
        width = end - start
        if min_width <= width <= max_width:
            sub = mask[:, start:end]
            rows = np.flatnonzero(sub.any(axis=1))
            if rows.size:
                y0, y1 = int(rows[0]), int(rows[-1]) + 1
                if min_height <= (y1 - y0) <= max_height and int(sub.sum()) >= min_ink:
                    glyphs.append(Glyph(start, end, y0, y1, sub[y0:y1]))
        start = None
        blank = 0

    return glyphs


def normalise(bitmap: np.ndarray, shape: tuple[int, int] = TEMPLATE_SHAPE) -> np.ndarray:
    """Resample a glyph bitmap onto the fixed comparison grid.

    Nearest-neighbour indexing rather than an interpolating resize: the source is a
    binary mask a few pixels across, where interpolation invents grey values that differ
    between a glyph and the averaged template of the same character.
    """
    h, w = bitmap.shape
    th, tw = shape
    if h == 0 or w == 0:
        return np.zeros(shape, dtype=np.float32)
    ys = np.minimum((np.arange(th) * h) // th, h - 1)
    xs = np.minimum((np.arange(tw) * w) // tw, w - 1)
    return bitmap[ys][:, xs].astype(np.float32)


def _zscore(a: np.ndarray) -> np.ndarray:
    std = a.std()
    if std < 1e-6:
        return np.zeros_like(a)
    return (a - a.mean()) / std


@dataclass
class GlyphTemplates:
    """Learned per-character templates plus their pre-normalised correlation forms."""

    templates: dict[str, np.ndarray] = field(default_factory=dict)
    sample_counts: dict[str, int] = field(default_factory=dict)
    _z: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        self._z = {ch: _zscore(t) for ch, t in self.templates.items()}

    def __len__(self) -> int:
        return len(self.templates)

    @property
    def characters(self) -> list[str]:
        return sorted(self.templates)

    def classify(self, bitmap: np.ndarray) -> tuple[str, float]:
        """Best-matching character and its normalised cross-correlation score."""
        if not self._z:
            return "?", 0.0
        probe = _zscore(normalise(bitmap))
        best_char, best_score = "?", -1.0
        for ch, tz in self._z.items():
            score = float(np.mean(probe * tz))
            if score > best_score:
                best_char, best_score = ch, score
        return best_char, best_score

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            characters=np.array(list(self.templates), dtype=object),
            stack=np.stack([self.templates[c] for c in self.templates])
            if self.templates
            else np.zeros((0, *TEMPLATE_SHAPE), dtype=np.float32),
            counts=np.array([self.sample_counts.get(c, 0) for c in self.templates]),
        )

    @classmethod
    def load(cls, path: Path) -> GlyphTemplates | None:
        try:
            with np.load(path, allow_pickle=True) as data:
                chars = [str(c) for c in data["characters"]]
                stack = data["stack"]
                counts = data["counts"]
        except (OSError, KeyError, ValueError) as exc:
            log.warning("could not load glyph templates", path=str(path), error=str(exc))
            return None
        if not chars:
            return None
        return cls(
            templates={c: stack[i].astype(np.float32) for i, c in enumerate(chars)},
            sample_counts={c: int(counts[i]) for i, c in enumerate(chars)},
        )


class TemplateLearner:
    """Accumulates labelled glyph samples and averages them into templates."""

    def __init__(self) -> None:
        self._samples: dict[str, list[np.ndarray]] = {}

    @property
    def total_samples(self) -> int:
        return sum(len(v) for v in self._samples.values())

    def add(self, char: str, bitmap: np.ndarray) -> None:
        self._samples.setdefault(char, []).append(normalise(bitmap))

    def observe_strip(self, mask: np.ndarray, expected: datetime | None) -> int:
        """Harvest labelled glyphs from one overlay strip. Returns the number learned."""
        glyphs = segment_glyphs(mask)
        if len(glyphs) < TIMESTAMP_GLYPH_COUNT:
            return 0

        learned = 0
        stamp = glyphs[:TIMESTAMP_GLYPH_COUNT]

        for idx in _SEP_DASH_INDICES:
            self.add("-", stamp[idx].bitmap)
            learned += 1
        for idx in _SEP_COLON_INDICES:
            self.add(":", stamp[idx].bitmap)
            learned += 1

        if expected is not None:
            fields = {
                "year": f"{expected.year:04d}",
                "month": f"{expected.month:02d}",
                "day": f"{expected.day:02d}",
                "hour": f"{expected.hour:02d}",
            }
            for idx, name, pos in _STABLE_DIGIT_SLOTS:
                self.add(fields[name][pos], stamp[idx].bitmap)
                learned += 1

        # "km/h" closes every line, so the final four glyphs are known outright.
        if len(glyphs) >= TIMESTAMP_GLYPH_COUNT + len(_TRAILING_LITERALS):
            for ch, glyph in zip(_TRAILING_LITERALS, glyphs[-4:]):
                self.add(ch, glyph.bitmap)
                learned += 1

        # 'E' and ':' immediately follow the timestamp, before the longitude.
        if len(glyphs) > TIMESTAMP_GLYPH_COUNT + 1:
            self.add("E", glyphs[TIMESTAMP_GLYPH_COUNT].bitmap)
            self.add(":", glyphs[TIMESTAMP_GLYPH_COUNT + 1].bitmap)
            learned += 2

        return learned

    def build(self, min_samples: int = _MIN_SAMPLES_PER_CHAR) -> GlyphTemplates:
        templates: dict[str, np.ndarray] = {}
        counts: dict[str, int] = {}
        for char, samples in self._samples.items():
            if len(samples) < min_samples:
                continue
            templates[char] = np.mean(np.stack(samples), axis=0).astype(np.float32)
            counts[char] = len(samples)
        return GlyphTemplates(templates=templates, sample_counts=counts)


def learn_structural(learner: TemplateLearner, templates: GlyphTemplates, mask: np.ndarray) -> bool:
    """Teach ``N`` and ``.`` once digits and ``:`` are already known.

    Neither has a fixed glyph index — the longitude field's width varies with the
    coordinate — so both are located by structure instead:

    * ``N`` precedes the fourth ``:`` on the line (two colons are in the timestamp, the
      third follows ``E``).
    * ``.`` is short *and sits on the digit baseline*. Height alone is not enough to
      identify it, because ``-`` is even shorter; the two are told apart by vertical
      position, since the dash floats at mid height while the decimal point rests on the
      baseline. Getting this wrong is expensive: with no ``.`` template, every coordinate
      decodes as an unbroken digit run and no position is ever recovered.
    """
    glyphs = segment_glyphs(mask)
    if len(glyphs) < TIMESTAMP_GLYPH_COUNT + 6:
        return False

    decoded = [templates.classify(g.bitmap)[0] for g in glyphs]
    gained = False

    colon_positions = [i for i, ch in enumerate(decoded) if ch == ":"]
    if len(colon_positions) >= 4:
        n_index = colon_positions[3] - 1
        if n_index > TIMESTAMP_GLYPH_COUNT:
            learner.add("N", glyphs[n_index].bitmap)
            gained = True

    # Digits define the baseline; anything resting on it that is too short to be a digit
    # is the decimal point.
    digit_bottoms = [g.y1 for g, ch in zip(glyphs, decoded) if ch.isdigit()]
    digit_heights = [g.height for g, ch in zip(glyphs, decoded) if ch.isdigit()]
    if digit_bottoms and digit_heights:
        baseline = int(np.median(digit_bottoms))
        digit_h = int(np.median(digit_heights))
        for i in range(TIMESTAMP_GLYPH_COUNT, len(glyphs)):
            glyph = glyphs[i]
            on_baseline = abs(glyph.y1 - baseline) <= max(2, digit_h // 8)
            short = glyph.height <= max(3, digit_h // 3)
            between_digits = (
                i > 0
                and i + 1 < len(decoded)
                and decoded[i - 1].isdigit()
                and decoded[i + 1].isdigit()
            )
            if short and on_baseline and between_digits:
                learner.add(".", glyph.bitmap)
                gained = True
                break

    return gained


def merge_templates(base: GlyphTemplates, extra: GlyphTemplates) -> GlyphTemplates:
    """Add characters from *extra* that *base* does not already know."""
    merged = dict(base.templates)
    counts = dict(base.sample_counts)
    for ch, tpl in extra.templates.items():
        if ch not in merged:
            merged[ch] = tpl
            counts[ch] = extra.sample_counts.get(ch, 1)
    return GlyphTemplates(templates=merged, sample_counts=counts)


#: Everything that has to be readable before telemetry can be trusted. Missing any digit
#: is silently corrupting: an unlearned character does not classify as "unknown", it
#: classifies as whichever known glyph looks nearest — and does so with a high
#: correlation score, so confidence thresholds do not catch it either.
REQUIRED_CHARACTERS = frozenset("0123456789-:.")


def missing_characters(templates: GlyphTemplates) -> set[str]:
    return set(REQUIRED_CHARACTERS) - set(templates.templates)


def stable_field_digits(when: datetime) -> set[str]:
    """Digits this timestamp would contribute, from the fields safe to label by position.

    Only year, month, day and hour are used. Minutes and seconds drift against the
    filename, and a two-second skew would teach the classifier a wrong digit.
    """
    return set(f"{when.year:04d}{when.month:02d}{when.day:02d}{when.hour:02d}")


def select_training_set(
    candidates: list[tuple[str, datetime]], *, limit: int = 12
) -> list[tuple[str, datetime]]:
    """Greedily choose recordings whose timestamps cover all ten digits.

    Sampling the first few files is not good enough: a corpus recorded over one week in
    2026 can easily contain no ``9`` at all in any stable field, and the resulting
    classifier reads every 9 as an 8 or a 0 without ever signalling doubt. A set cover
    over the filename timestamps fixes that with only a handful of files.
    """
    remaining = set("0123456789")
    chosen: list[tuple[str, datetime]] = []
    pool = list(candidates)

    while remaining and pool and len(chosen) < limit:
        best_index, best_gain = None, 0
        for i, (_, when) in enumerate(pool):
            gain = len(stable_field_digits(when) & remaining)
            if gain > best_gain:
                best_index, best_gain = i, gain
        if best_index is None:
            break
        name, when = pool.pop(best_index)
        chosen.append((name, when))
        remaining -= stable_field_digits(when)

    # Pad with additional files so each character is averaged over many samples rather
    # than resting on a single frame.
    for entry in pool:
        if len(chosen) >= limit:
            break
        chosen.append(entry)
    return chosen


#: A gap wider than roughly one glyph cell means a character cell was skipped, i.e. a
#: space. Measured on the real overlay, gaps within a field run 2-16 px while gaps
#: between fields are 26 px and up, so one median glyph width (~20 px) separates them
#: cleanly. Setting this lower splits ``138.6769`` into ``138. 6769`` and loses the
#: coordinate entirely, which is exactly the failure this constant exists to prevent.
_SPACE_GAP_RATIO = 1.0


def decode_line(mask: np.ndarray, templates: GlyphTemplates) -> tuple[str, float]:
    """Decode a whole overlay strip into text plus a mean confidence.

    Wide gaps are re-emitted as spaces. The parser matches on field order and tolerates
    missing separators, but keeping the spaces makes the raw text readable in the UI when
    a user wants to see what the OCR actually saw.
    """
    glyphs = segment_glyphs(mask)
    if not glyphs:
        return "", 0.0

    # Narrow glyphs (':', '.') would drag a plain median down, so measure the cell width
    # from the full-height characters that actually define the grid.
    tall = [g.width for g in glyphs if g.height >= max(g2.height for g2 in glyphs) * 0.6]
    widths = tall or [g.width for g in glyphs]
    median_width = float(np.median(widths))
    space_threshold = max(10.0, median_width * _SPACE_GAP_RATIO)

    chars: list[str] = []
    scores: list[float] = []
    previous_end: int | None = None

    for glyph in glyphs:
        if previous_end is not None and (glyph.x0 - previous_end) >= space_threshold:
            chars.append(" ")
        char, score = templates.classify(glyph.bitmap)
        chars.append(char)
        scores.append(score)
        previous_end = glyph.x1

    text = "".join(chars)
    # Correlation runs -1..1; map to 0..1 so it reads as a confidence in the UI.
    confidence = float(np.clip((np.mean(scores) + 1.0) / 2.0, 0.0, 1.0)) if scores else 0.0
    return text, confidence


_FILENAME_TIME_RE = re.compile(r"(\d{14})")


def expected_time_from_filename(name: str) -> datetime | None:
    """Recording start time encoded in ``YYYYMMDDHHMMSS_camera_N.ts``.

    Used only to label the slow-moving timestamp fields during learning, so a few seconds
    of drift between the filename and the camera clock does not matter.
    """
    match = _FILENAME_TIME_RE.search(name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
