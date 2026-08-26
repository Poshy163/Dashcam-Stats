"""Telemetry extraction: decode the overlay strip of a recording into GPS samples.

This is the only source of position, speed and authoritative wall-clock time for this
footage, because the files carry no telemetry metadata whatsoever.

Two derivations happen here that the dashcam does not provide:

* **Heading** is computed from consecutive fixes. It is a bearing between two points, not
  a compass reading, and is stored as such.
* **Distance** accumulates only between fixes far enough apart to exceed the overlay's
  own coordinate quantisation. At 4 decimal places a stationary car jitters by roughly
  11 m per sample; integrating that noise over a two-minute parked segment would invent
  several hundred metres of travel and corrupt every journey total.
"""

from __future__ import annotations

import contextlib
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.core.logging import get_logger
from app.hardware.ffmpeg import (
    DecodeError,
    FFmpegError,
    is_empty_window,
    is_infrastructure_failure,
    iter_frames,
    probe,
)
from app.osd.geo import bearing_deg, haversine_m
from app.osd.glyphs import (
    GlyphTemplates,
    TemplateLearner,
    binarise,
    decode_line,
    expected_time_from_filename,
    learn_structural,
    merge_templates,
    missing_characters,
)
from app.osd.locate import FixSample, FixTrack
from app.osd.outliers import (
    looks_like_sign_loss,
    plausible_radius_m,
    robust_centre,
    spatial_outliers,
)
from app.osd.parser import OsdReading, parse_osd_text
from app.osd.reasons import GpsQuality, GpsReason
from app.osd.region import OsdRegion
from app.osd.track_quality import Fix, classify
from app.osd.validate import MAX_PLAUSIBLE_SPEED_KMH, MAX_PLAUSIBLE_SPEED_MS, is_valid_coordinate

log = get_logger(__name__)


@dataclass(slots=True)
class TelemetrySample:
    """One stored telemetry point, ready to become a ``TelemetryPoint`` row."""

    t_offset_s: float
    captured_at: datetime | None
    lat: float | None
    lon: float | None
    has_fix: bool
    speed_kmh: float | None
    heading_deg: float | None
    ocr_confidence: float
    raw_text: str
    time_status: str = "parse_failed"
    gps_status: str = "parse_failed"
    gps_source: str = "none"
    ocr_status: str = "failed"
    problems: list[str] = field(default_factory=list)
    candidate_count: int = 1
    #: Raw parsed overlay clock, retained even when canonical validation rejects it.
    overlay_captured_at: datetime | None = None
    #: Machine-readable account of why this sample carries no trusted position, from
    #: :class:`app.osd.reasons.GpsReason`. ``None`` when the position was accepted.
    gps_reason: str | None = None
    #: True when the route must be broken *before* this sample rather than joined to
    #: whatever came before it. Set by the gap rules, not by any single bad reading.
    breaks_segment: bool = False


@dataclass(slots=True)
class TelemetryResult:
    samples: list[TelemetrySample] = field(default_factory=list)
    frames_read: int = 0
    parse_failures: int = 0
    low_confidence: int = 0

    distance_m: float = 0.0
    max_speed_kmh: float | None = None
    avg_speed_kmh: float | None = None
    first_fix: TelemetrySample | None = None
    last_fix: TelemetrySample | None = None
    #: Overlay clock at the first readable frame. More trustworthy than the filename,
    #: which is only the moment the camera opened the file.
    osd_start_time: datetime | None = None
    #: Fixes discarded for implying impossible movement -- misread coordinates.
    implausible_jumps: int = 0
    warnings: list[str] = field(default_factory=list)
    #: The decoder that actually produced frames, after any hardware fallback.
    decoder: str | None = None
    #: The decode did not run to completion, so ``samples`` describes only as much of the
    #: recording as ffmpeg managed to hand over. Partial telemetry from a genuinely
    #: truncated file is still worth keeping; an empty result from a decoder that fell over
    #: is not, and the caller has to be able to tell those apart before it replaces what is
    #: already stored. See ``stage_telemetry``.
    decode_failed: bool = False
    #: ...and whether the failure was the machine's rather than the file's -- a killed
    #: ffmpeg, a stalled decode, an unhealthy media gate. Retrying one of those is likely
    #: to work; retrying a truncated file is not.
    decode_infrastructure: bool = False

    @property
    def fix_count(self) -> int:
        return sum(1 for s in self.samples if s.has_fix)

    @property
    def has_gps(self) -> bool:
        return self.fix_count > 0


class TelemetryExtractor:
    """Reads the burned-in overlay from recordings.

    Templates are learned once from the footage itself and cached on disk; see
    ``app.osd.glyphs`` for why that works without shipping a font.
    """

    def __init__(
        self,
        templates: GlyphTemplates | None = None,
        *,
        template_path: Path | None = None,
    ) -> None:
        self._templates = templates
        self._template_path = template_path

    # -- templates ---------------------------------------------------------------------

    @property
    def templates(self) -> GlyphTemplates | None:
        return self._templates

    @property
    def ready(self) -> bool:
        return self._templates is not None and not missing_characters(self._templates)

    def load_templates(self, path: Path) -> bool:
        loaded = GlyphTemplates.load(path)
        if loaded is None:
            return False
        self._templates = loaded
        self._template_path = path
        missing = missing_characters(loaded)
        if missing:
            # A partial set is worse than none: an unlearned digit does not come back as
            # "unknown", it comes back as the nearest-looking learned glyph with a high
            # score, so bad values would flow through with convincing confidence.
            log.warning(
                "loaded OSD templates are incomplete; telemetry will be re-learned",
                missing=sorted(missing),
            )
            return False
        return True

    async def learn_templates(
        self,
        recordings: list[Path],
        *,
        region: OsdRegion,
        frames_per_recording: int = 10,
        # 1 fps rather than something sparser: a character needs several samples before it
        # is averaged into a template, and a sparse rate over a short recording yields one
        # or two strips, so the rarest digits are collected too few times and dropped —
        # which produces a silently incomplete template set rather than an error.
        sample_fps: float = 1.0,
    ) -> GlyphTemplates:
        """Bootstrap templates from real footage.

        The caller is expected to have chosen *recordings* with
        ``glyphs.select_training_set`` so that all ten digits appear in the timestamp
        fields that are safe to label.
        """
        learner = TemplateLearner()
        used = 0

        for path in recordings:
            expected = expected_time_from_filename(path.name)
            if expected is None:
                continue
            try:
                async with contextlib.aclosing(
                    self._iter_strips(path, region, fps=sample_fps, limit=frames_per_recording)
                ) as strips:
                    async for _, frame in strips:
                        learner.observe_strip(binarise(frame), expected)
                used += 1
            except (FFmpegError, OSError) as exc:
                log.debug("skipped during template learning", file=path.name, error=str(exc))

        templates = learner.build()
        log.info(
            "learned OSD glyph templates",
            recordings=used,
            characters=templates.characters,
            samples=learner.total_samples,
        )

        # 'N' and '.' have no fixed glyph index, so they need a second pass that reads
        # the surrounding characters to locate them.
        structural = TemplateLearner()
        for path in recordings[:5]:
            try:
                async with contextlib.aclosing(
                    self._iter_strips(path, region, fps=sample_fps, limit=6)
                ) as strips:
                    async for _, frame in strips:
                        learn_structural(structural, templates, binarise(frame))
            except (FFmpegError, OSError):
                continue
        templates = merge_templates(templates, structural.build(min_samples=1))

        missing = missing_characters(templates)
        if missing:
            log.warning("OSD templates incomplete", missing=sorted(missing))

        self._templates = templates
        if self._template_path is not None:
            templates.save(self._template_path)
        return templates

    # -- extraction --------------------------------------------------------------------

    async def _iter_strips(
        self,
        path: Path,
        region: OsdRegion,
        *,
        fps: float,
        limit: int | None = None,
        width: int | None = None,
        height: int | None = None,
        hwaccel: str = "auto",
        on_decoder: Callable[[str], None] | None = None,
    ):
        if width is None or height is None:
            info = await probe(path)
            width, height = info.width or 1920, info.height or 1080
        crop = region.to_crop(width, height)
        count = 0
        # `aclosing`, because `limit` makes this return mid-stream and because every
        # consumer of this generator may itself stop early. An abandoned decoder holds its
        # ffmpeg child, and therefore the Intel media slot, until the event loop gets round
        # to finalising it -- which on this hardware means beside an OpenVINO request.
        async with contextlib.aclosing(
            iter_frames(
                path,
                fps=fps,
                crop=crop,
                grayscale=True,
                hwaccel=hwaccel,
                on_decoder=on_decoder,
            )
        ) as frames:
            async for offset, frame in frames:
                yield offset, frame
                count += 1
                if limit is not None and count >= limit:
                    return

    async def extract(
        self,
        path: Path,
        *,
        region: OsdRegion,
        sample_fps: float = 1.0,
        min_confidence: float = 0.6,
        max_speed_kmh: float = 300.0,
        min_move_m: float = 12.0,
        max_recovery_gap_s: float = 3.0,
        hwaccel: str = "auto",
    ) -> TelemetryResult:
        """Decode telemetry for one recording.

        ``sample_fps`` defaults to 1.0 because the overlay updates once per second.
        Sampling faster costs proportionally more decode time and returns duplicates.
        """
        result = TelemetryResult()

        def note_decoder(decoder: str) -> None:
            if result.decoder != "software":
                result.decoder = decoder

        if self._templates is None:
            result.warnings.append("no OSD glyph templates available")
            return result

        try:
            info = await probe(path)
        except FFmpegError as exc:
            result.warnings.append(f"probe failed: {exc}")
            return result

        width, height = info.width or 1920, info.height or 1080
        # The overlay is stable for a whole second. Looking twice during that second is
        # inexpensive (only a 50-pixel strip crosses the pipe) and avoids making the
        # entire sample depend on one frame whose road texture happens to touch a glyph.
        # Two reads per stored sample, capped. The `max()` this replaced could never pick
        # its first argument -- `sample_fps` is positive, so `sample_fps * 2` always wins --
        # and read as though the doubling were conditional.
        candidate_fps = min(4.0, sample_fps * 2.0)
        buckets: dict[int, list[tuple[float, OsdReading]]] = {}

        try:
            # Closed explicitly: this is the telemetry stage of a cancellable job, and an
            # abandoned strip reader keeps its ffmpeg child -- and the Intel media slot --
            # until the event loop finalises it.
            async with contextlib.aclosing(
                self._iter_strips(
                    path,
                    region,
                    fps=candidate_fps,
                    width=width,
                    height=height,
                    hwaccel=hwaccel,
                    on_decoder=note_decoder,
                )
            ) as strips:
                async for offset, frame in strips:
                    result.frames_read += 1
                    text, confidence = decode_line(binarise(frame), self._templates)
                    reading = parse_osd_text(
                        text, confidence=confidence, max_speed_kmh=max_speed_kmh
                    )
                    bucket = math.floor((offset * sample_fps) + 1e-6)
                    buckets.setdefault(bucket, []).append((offset, reading))
        except DecodeError as exc:
            # Partial telemetry from a damaged file is still worth keeping. Whether this
            # *is* a damaged file is the caller's decision, so both facts are recorded
            # rather than settled here.
            result.warnings.append(f"decode ended early: {exc}")
            # A clean end with nothing in it is a fact about the file, not about the
            # decoder, and the caller must be able to tell the two apart -- see
            # `is_empty_window`.
            result.decode_failed = not is_empty_window(exc)
            result.decode_infrastructure = is_infrastructure_failure(exc)
        except FFmpegError as exc:
            result.warnings.append(f"decode failed: {exc}")
            result.decode_failed = True
            result.decode_infrastructure = is_infrastructure_failure(exc)
            return result

        result.samples = [
            self._combine_candidates(
                round(bucket / sample_fps, 3), candidates, min_confidence=min_confidence
            )
            for bucket, candidates in sorted(buckets.items())
        ]
        result.parse_failures = sum(s.ocr_status == "failed" for s in result.samples)
        result.low_confidence = sum(s.ocr_status == "low_confidence" for s in result.samples)

        result.osd_start_time = self._clock_origin(result.samples, path)
        if result.osd_start_time is not None:
            for sample in result.samples:
                if sample.captured_at is None:
                    continue
                expected = result.osd_start_time + timedelta(seconds=sample.t_offset_s)
                delta_s = (sample.captured_at - expected).total_seconds()
                if abs(delta_s) > 2.5:
                    sample.problems.append(
                        f"overlay clock differed from reconciled timeline by {delta_s:.3f}s"
                    )
                    sample.captured_at = None
                    sample.time_status = "rejected"
        # Validation comes first, and interpolation only once, afterwards.
        #
        # The order used to be interpolate, validate, then interpolate *again* over the
        # holes validation had just made — and the second pass never re-checked its own
        # output. So a position rejected for being 2.6 km out of place was immediately
        # replaced by a straight line drawn through where it had been, at up to 111 m per
        # second, and the count of rejections was decremented to hide it. Those synthetic
        # runs are exactly the long straight lines that appear on the heat map: each step
        # is small enough to slip under the route layer's jump threshold, so the line is
        # never broken, and the vehicle appears to have driven through car parks and
        # reserves at 400 km/h.
        #
        # Filling a hole is only defensible when the fixes bracketing it are themselves
        # trusted, which is not known until validation has run.
        self._derive(result, min_move_m=min_move_m)
        self._recover_short_gaps(result.samples, max_gap_s=max_recovery_gap_s)
        # Anything interpolation invented is judged by the same rules as anything read,
        # so a fill that lands somewhere the vehicle could not have been is discarded
        # rather than drawn.
        self._reject_implausible_fills(result)
        self._retotal(result, min_move_m=min_move_m)
        return result

    @staticmethod
    def _combine_candidates(
        offset: float,
        candidates: list[tuple[float, OsdReading]],
        *,
        min_confidence: float,
    ) -> TelemetrySample:
        """Choose each field from the best frame in one overlay-second.

        This explicitly prevents a later failed OCR result replacing an earlier successful
        one. Time, GPS and speed may come from different frames because their parse and
        validation outcomes are independent.
        """
        trusted = [reading for _at, reading in candidates if reading.confidence >= min_confidence]
        all_readings = [reading for _at, reading in candidates]
        representative = max(
            all_readings,
            key=lambda r: (
                int(r.captured_at is not None) + int(r.has_position) + int(r.speed_kmh is not None),
                r.confidence,
            ),
        )
        if not trusted:
            problems = list(dict.fromkeys((representative.problems or []) + ["OCR confidence low"]))
            return TelemetrySample(
                t_offset_s=offset,
                captured_at=None,
                lat=None,
                lon=None,
                has_fix=False,
                speed_kmh=None,
                heading_deg=None,
                ocr_confidence=representative.confidence,
                raw_text=representative.raw_text[:255],
                time_status="low_confidence",
                gps_status="low_confidence",
                ocr_status="low_confidence",
                problems=problems,
                candidate_count=len(candidates),
                overlay_captured_at=representative.captured_at,
            )

        def best(predicate):
            return max(
                (r for r in trusted if predicate(r)), key=lambda r: r.confidence, default=None
            )

        time_reading = best(lambda r: r.captured_at is not None)
        gps_reading = best(lambda r: r.has_position)
        speed_reading = best(lambda r: r.speed_kmh is not None)
        no_fix = best(lambda r: r.gps_status == "no_fix")

        # A sibling frame that read ``00.0000/00.0000`` is not the *absence* of a position,
        # it is the camera stating it has no lock -- and the two frames sampled in one
        # overlay-second are looking at the same printed characters, so they cannot both be
        # right. Preferring the positioned frame unconditionally meant one corrupted read
        # beat a clean no-fix read every time, whatever the classifier thought of either.
        # That is how a garage full of ``N:00.0000`` became 24 fixes in the Gulf of Guinea.
        #
        # Ties go to no-fix on purpose. Overruling the camera's own report is the claim that
        # needs the evidence, so the frame carrying coordinates has to be *more* confident,
        # not merely as confident.
        overruled = (
            gps_reading is not None
            and no_fix is not None
            and no_fix.confidence >= gps_reading.confidence
        )
        if overruled:
            gps_reading = None

        fields = sum(value is not None for value in (time_reading, gps_reading, speed_reading))
        status = "valid" if fields == 3 else "partial" if fields else "failed"
        problems = list(dict.fromkeys(problem for r in trusted for problem in (r.problems or [])))
        if overruled:
            problems.append(
                "a sibling frame read the no-fix marker at least as confidently; position discarded"
            )
        # Deliberately *not* recorded as a problem.
        #
        # Two candidate frames per overlay second is the design -- `candidate_fps` is
        # twice `sample_fps` precisely so each stored sample can be assembled from the
        # better of two independent reads. Noting that as a "problem" meant every cleanly
        # parsed sample carried one, `quality_rollup` counted it, `telemetry_problem_count`
        # came out equal to the point count, and the Telemetry Health page classified every
        # recording in every library as degraded -- so "Healthy" was unreachable and the one
        # screen built to separate real GPS loss from OCR noise was entirely false
        # positives. How many frames were considered is provenance, not a fault, and it is
        # already stored losslessly as ``quality_json["candidate_count"]``.

        time_status = (
            "valid"
            if time_reading is not None
            else "rejected"
            if any(r.time_status == "rejected" for r in trusted)
            else "parse_failed"
        )
        gps_status = (
            "valid"
            if gps_reading is not None
            else "no_fix"
            if no_fix is not None
            else "rejected"
            if any(r.gps_status == "rejected" for r in trusted)
            else "parse_failed"
        )

        return TelemetrySample(
            t_offset_s=offset,
            captured_at=time_reading.captured_at if time_reading else None,
            lat=gps_reading.lat if gps_reading else None,
            lon=gps_reading.lon if gps_reading else None,
            has_fix=gps_reading is not None,
            speed_kmh=speed_reading.speed_kmh if speed_reading else None,
            heading_deg=None,
            ocr_confidence=representative.confidence,
            raw_text=representative.raw_text[:255],
            time_status=time_status,
            gps_status=gps_status,
            gps_source="direct" if gps_reading else "none",
            ocr_status=status,
            problems=problems,
            candidate_count=len(candidates),
            overlay_captured_at=time_reading.captured_at if time_reading else None,
        )

    @staticmethod
    def _recover_short_gaps(samples: list[TelemetrySample], *, max_gap_s: float) -> int:
        """Interpolate only short OCR/parse gaps bracketed by plausible direct fixes."""
        track = FixTrack(
            [
                FixSample(s.t_offset_s, float(s.lat), float(s.lon), s.captured_at, s.speed_kmh)
                for s in samples
                if s.has_fix and s.lat is not None and s.lon is not None
            ],
            max_fix_age_s=0.0,
            max_bracket_s=max(0.0, max_gap_s),
        )
        recovered = 0
        for sample in samples:
            # An explicit zero-coordinate marker means the camera itself reported no lock.
            # That is never an OCR dropout and must never be filled.
            if sample.has_fix or sample.gps_status == "no_fix":
                continue
            located = track.at(sample.t_offset_s)
            if located is None or located.source != "interpolated":
                continue
            sample.lat = located.lat
            sample.lon = located.lon
            sample.has_fix = True
            sample.gps_status = "interpolated"
            sample.gps_source = "interpolated"
            sample.problems.append(f"GPS interpolated across {located.span_s:g}s OCR/parse gap")
            recovered += 1
        return recovered

    @staticmethod
    def _clock_origin(samples: list[TelemetrySample], path: Path) -> datetime | None:
        """Reconcile overlay clocks into the wall-clock time at video offset zero."""
        origins = [
            sample.captured_at - timedelta(seconds=sample.t_offset_s)
            for sample in samples
            if sample.captured_at is not None
        ]
        if not origins:
            return None
        filename_origin = expected_time_from_filename(path.name)
        if filename_origin is not None:
            plausible = [
                origin
                for origin in origins
                if abs((origin - filename_origin).total_seconds()) <= 15 * 60
            ]
            if plausible:
                origins = plausible
        anchor = origins[0]
        delta = statistics.median((origin - anchor).total_seconds() for origin in origins)
        return anchor + timedelta(seconds=delta)

    @staticmethod
    def _seconds_between(a: TelemetrySample, b: TelemetrySample) -> float:
        """Elapsed time between two samples, preferring the overlay clock.

        Falls back to the decode offset, which is what exists when a timestamp failed to
        parse but the coordinates did.
        """
        if a.captured_at is not None and b.captured_at is not None:
            gap = abs((b.captured_at - a.captured_at).total_seconds())
            if gap > 0:
                return gap
        return max(0.0, b.t_offset_s - a.t_offset_s)

    @staticmethod
    def _discard(sample: TelemetrySample, reason: str | None = None) -> None:
        """Strip a position off a sample, keeping the time and speed that were fine."""
        sample.has_fix = False
        sample.lat = None
        sample.lon = None
        sample.heading_deg = None
        sample.gps_status = "rejected"
        sample.gps_source = "none"
        if reason is not None:
            sample.gps_reason = reason

    @staticmethod
    def _reject_implausible_fills(result: TelemetryResult) -> int:
        """Re-judge every surviving position once interpolation has run.

        Two things make this pass worth its cost rather than a duplicate of the checks in
        :meth:`_derive`.

        The first is that it sees the interpolated samples, which by construction did not
        exist when the earlier checks ran. A fill is a straight line between two fixes, and
        a straight line between two fixes that are individually fine can still cross a
        river.

        The second is that :mod:`app.osd.track_quality` asks a question the sequential walk
        cannot: it compares each position against the median of the samples on *both* sides
        of it. A walk only ever looks backwards, so an excursion that leaves and returns is
        reachable from its predecessor in both directions and passes twice. That is the
        shape a single corrupt digit produces, and it is what put a position 2.6 km off the
        route in the middle of an otherwise clean recording.
        """
        indexed = [
            (index, sample)
            for index, sample in enumerate(result.samples)
            if sample.has_fix and sample.lat is not None and sample.lon is not None
        ]
        if not indexed:
            return 0

        verdicts = classify(
            [
                Fix(
                    t_s=sample.t_offset_s,
                    lat=float(sample.lat),  # type: ignore[arg-type]
                    lon=float(sample.lon),  # type: ignore[arg-type]
                    synthetic=sample.gps_source == "interpolated",
                )
                for _index, sample in indexed
            ]
        )

        removed = 0
        for (_index, sample), verdict in zip(indexed, verdicts, strict=True):
            if verdict.quality is GpsQuality.REJECTED:
                TelemetryExtractor._discard(sample, str(verdict.reasons[0]))
                sample.problems.append(verdict.detail)
                removed += 1
                continue
            # A kept sample may still begin a new segment. Carrying that decision on the
            # row is what lets every map draw the same breaks without re-deriving them.
            sample.breaks_segment = verdict.breaks_segment
            if GpsReason.GPS_GAP in verdict.reasons:
                sample.problems.append(verdict.detail)
        if removed:
            result.implausible_jumps += removed
            result.warnings.append(
                f"discarded {removed} position(s) that disagreed with the samples surrounding them"
            )
        return removed

    @staticmethod
    def _retotal(result: TelemetryResult, *, min_move_m: float) -> None:
        """Recompute distance and endpoints over whatever positions survived.

        Distance is accumulated during the walk in :meth:`_derive`, which necessarily runs
        before interpolation and the pass that judges it. Leaving that total alone would
        report kilometres contributed by positions that were subsequently discarded — the
        precise reason a recording could show a distance its own route did not support.

        Interpolated positions are deliberately excluded from the total. They are a line we
        drew ourselves, and measuring it would be counting our own guess as evidence; the
        gap they span is already covered by the trusted fixes at either end.
        """
        result.distance_m = 0.0
        trusted = [
            sample
            for sample in result.samples
            if sample.has_fix
            and sample.lat is not None
            and sample.lon is not None
            and sample.gps_source != "interpolated"
        ]
        anchor: TelemetrySample | None = None
        for sample in trusted:
            if anchor is None:
                anchor = sample
                continue
            if sample.breaks_segment:
                # Nothing was recorded across this break, so nothing is known to have been
                # travelled. Adding the straight line would invent the distance the route
                # layer is careful not to draw.
                anchor = sample
                continue
            step = haversine_m(anchor.lat, anchor.lon, sample.lat, sample.lon)  # type: ignore[arg-type]
            if step < min_move_m:
                continue
            result.distance_m += step
            anchor = sample

        still_fixed = [s for s in result.samples if s.has_fix]
        result.first_fix = still_fixed[0] if still_fixed else None
        result.last_fix = still_fixed[-1] if still_fixed else None

    @staticmethod
    def _reject_by_consensus(fixes: list[TelemetrySample]) -> list[TelemetrySample]:
        """Drop fixes the rest of the recording disagrees with, before anything is derived.

        This has to happen before the sequential walk below, and the reason is the walk's
        one blind spot: it trusts its starting point absolutely. The first fix becomes the
        anchor with nothing to check it against, so when *that* one is the misread -- a
        longitude of ``-8.6845`` on a drive whose every other reading says ``138.68`` --
        the walk keeps it and rejects all 118 good fixes that follow for disagreeing with
        it. The recording then reports a single position, in the wrong ocean, and every
        detection in it is stamped with that.

        A median cannot be dragged by the outlier it is looking for, so asking the whole
        recording where it was is the check the first fix never had. ``spatial_outliers``
        refuses to act when it needs fewer than three points or when a majority look wrong,
        which is what keeps this from throwing away a genuinely long drive.
        """
        points = [(float(s.lat), float(s.lon)) for s in fixes]  # type: ignore[arg-type]
        span_s = fixes[-1].t_offset_s - fixes[0].t_offset_s if fixes else 0.0
        outliers = spatial_outliers(points, span_s=span_s)
        if not outliers:
            return []

        centre = robust_centre([p for i, p in enumerate(points) if i not in outliers])
        radius = plausible_radius_m(span_s)
        repaired: list[TelemetrySample] = []
        rejected: list[TelemetrySample] = []
        for index in outliers:
            sample = fixes[index]
            point = (float(sample.lat), float(sample.lon))  # type: ignore[arg-type]
            if centre is not None and looks_like_sign_loss(point, centre, radius):
                # A southern-hemisphere minus sign is the thinnest glyph in the overlay.
                # Repair it only when the unmodified coordinate is an outlier and negating
                # latitude puts it inside the recording's independently established track.
                sample.lat = -float(sample.lat)  # type: ignore[arg-type]
                sample.gps_status = "recovered"
                sample.gps_source = "context_repaired"
                sample.problems.append("latitude sign recovered from recording consensus")
                repaired.append(sample)
            else:
                rejected.append(sample)
        for sample in rejected:
            TelemetryExtractor._discard(sample)
        if repaired:
            log.info(
                "recovered latitude signs from recording consensus",
                repaired=len(repaired),
                of=len(fixes),
            )
        if rejected:
            log.warning(
                "discarded positions the rest of the recording disagrees with",
                rejected=len(rejected),
                of=len(fixes),
                centre_lat=round(centre[0], 4) if centre else None,
                centre_lon=round(centre[1], 4) if centre else None,
                radius_km=round(radius / 1000.0, 1),
            )
        return rejected

    @staticmethod
    def _derive(result: TelemetryResult, *, min_move_m: float) -> None:
        """Fill in heading and the journey rollups, discarding impossible movement."""
        # Metres a vehicle could plausibly cover in one second. Anything beyond this
        # between consecutive fixes is a misread coordinate, not travel.
        max_step_m = MAX_PLAUSIBLE_SPEED_MS
        rejected: list[TelemetrySample] = []

        # Belt and braces against a coordinate that is not a number at all. The parser
        # already refuses those, but this is the last point before they become rows, and a
        # NaN reaching the database is invisible everywhere downstream.
        for sample in result.samples:
            if sample.has_fix and not is_valid_coordinate(sample.lat, sample.lon):
                TelemetryExtractor._discard(sample)

        fixes = [s for s in result.samples if s.has_fix and s.lat is not None and s.lon is not None]
        rejected.extend(TelemetryExtractor._reject_by_consensus(fixes))
        fixes = [s for s in fixes if s.has_fix]

        if fixes:
            result.first_fix, result.last_fix = fixes[0], fixes[-1]

        # `anchor` stays on the last point that moved a real distance. Advancing it on
        # every sample instead would re-admit the jitter this threshold exists to reject,
        # since each individual hop clears nothing but they sum to hundreds of metres.
        anchor: TelemetrySample | None = None
        stepwise: list[TelemetrySample] = []
        for sample in fixes:
            if anchor is None:
                anchor = sample
                continue

            step = haversine_m(anchor.lat, anchor.lon, sample.lat, sample.lon)  # type: ignore[arg-type]

            # An OCR misread that lands inside the valid coordinate range still passes
            # every per-point check: 138.6769 read as 13.8769 is a perfectly legal
            # longitude about 13,000 km away. Only comparing consecutive fixes catches it,
            # and without this a single bad digit turned a two-minute clip into a
            # 31,000 km journey. Reject the *sample*, not the anchor -- the anchor is the
            # last position we still trust.
            gap_s = TelemetryExtractor._seconds_between(anchor, sample)
            if step > max_step_m * max(1.0, gap_s):
                stepwise.append(sample)
                continue

            if step < min_move_m:
                # Within the overlay's 4-decimal-place quantisation: not movement, and a
                # bearing between two jitter samples would be meaningless too.
                continue
            result.distance_m += step
            sample.heading_deg = round(
                bearing_deg(anchor.lat, anchor.lon, sample.lat, sample.lon),
                1,  # type: ignore[arg-type]
            )
            anchor = sample

        # A walk that condemns most of what it saw has not found outliers, it has found a
        # bad anchor -- the same failure the consensus pass exists to prevent, in the case
        # where there were too few points for it to judge. Discarding the majority on the
        # authority of one unchecked point is the more expensive mistake, so the walk's
        # verdict is dropped instead.
        if stepwise and len(stepwise) * 2 < len(fixes):
            for sample in stepwise:
                TelemetryExtractor._discard(sample)
            rejected.extend(stepwise)
        elif stepwise:
            result.warnings.append(
                f"{len(stepwise)} of {len(fixes)} positions disagreed with the fix before "
                "them; keeping them all rather than trusting one unverified starting point"
            )

        if rejected:
            result.implausible_jumps = len(rejected)
            result.warnings.append(
                f"discarded {len(rejected)} misread position(s): either implying travel "
                f"faster than {MAX_PLAUSIBLE_SPEED_KMH:.0f} km/h, or naming a place the "
                "rest of the recording disagrees with"
            )
            # first/last fix may have just been invalidated.
            still_fixed = [s for s in result.samples if s.has_fix]
            result.first_fix = still_fixed[0] if still_fixed else None
            result.last_fix = still_fixed[-1] if still_fixed else None

        speeds = [s.speed_kmh for s in result.samples if s.speed_kmh is not None]
        if speeds:
            result.max_speed_kmh = max(speeds)
            # Averaged over moving samples only. Including long idle periods would report
            # a "trip average" of a few km/h for any journey with traffic lights.
            moving = [v for v in speeds if v > 1.0]
            result.avg_speed_kmh = round(sum(moving) / len(moving), 1) if moving else 0.0
