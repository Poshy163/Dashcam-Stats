"""Seeing what the overlay reader sees.

Every telemetry problem in this project has been diagnosed the same way: pull the frame,
crop the strip, binarise it, and look. That loop lived in throwaway scripts pointed at a
network share, which meant it was only available to whoever had the share mounted and the
repo checked out — and it is the single most useful view in the application, because the
overlay is the only source of position and speed there is.

The important property is that this shows *production's* answer, not a re-derivation of it.
It loads the same active region and the same learned templates the telemetry stage uses, so
a discrepancy here is a real discrepancy rather than an artefact of the debugging tool. A
debug view that quietly does something slightly different is worse than none, because it
sends you looking in the wrong place.
"""

from __future__ import annotations

import io
from datetime import timedelta

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.deps import RowId, SessionDep
from app.core.logging import get_logger
from app.core.paths import resolve_footage_path
from app.core.settings_service import get_settings_service
from app.db.models import OsdProfile, Recording, TelemetryPoint
from app.hardware.ffmpeg import extract_frame, probe
from app.media import ensure_playable
from app.osd.engine import TelemetryExtractor
from app.osd.glyphs import binarise, decode_line, segment_glyphs
from app.osd.parser import parse_osd_text
from app.osd.region import OsdRegion

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["diagnostics"])


async def _active_region(session) -> OsdRegion:
    row = (
        await session.execute(select(OsdProfile).where(OsdProfile.active.is_(True)).limit(1))
    ).scalar_one_or_none()
    if row is None:
        return OsdRegion(x=0.0, y=0.9537, w=1.0, h=0.0463)
    return OsdRegion(x=row.region_x, y=row.region_y, w=row.region_w, h=row.region_h)


async def _read(recording_id: RowId, session, offset_s: float):
    """The optional re-read frame, region, and loaded extractor at *offset_s*."""
    recording = await session.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")

    path = resolve_footage_path(recording.rel_path, must_exist=True)
    region = await _active_region(session)

    extractor = TelemetryExtractor()
    from app.pipeline.stages import _get_templates

    await _get_templates(session, extractor, region)

    info = await probe(path)
    width, height = info.width or 1920, info.height or 1080
    crop = region.to_crop(width, height)

    settings = get_settings_service()
    hwaccel = "auto" if await settings.hardware_acceleration() else "cpu"

    # extract_frame, not a hand-rolled loop over iter_frames. Asking for one frame at an
    # offset is what it is for, and doing it again here got it wrong: pairing `fps=1` with
    # a one-second window makes the frame-selection filter emit *nothing* whenever the
    # chosen instant falls outside the window, which depends on where the seek lands. The
    # symptom was a debug view that worked at t=0 and t=10 and returned 422 at t=1 -- on a
    # recording that decodes perfectly.
    # Read the same normalised MP4 timeline as the browser. Seeking the original MPEG-TS
    # directly is unreliable because its rolling PTS can start above zero or wrap: a debug
    # request for 1:34 has returned the frame from 1:31, which made correct stored telemetry
    # look wrong. The cached remux copies every frame and only repairs the container clock.
    playable = await ensure_playable(path, int(recording.id))
    frame = await extract_frame(playable.path, offset_s, hwaccel=hwaccel)
    return recording, frame, crop, extractor, info


def _crop_strip(frame: np.ndarray, crop) -> np.ndarray:
    y0 = max(0, crop.y)
    x0 = max(0, crop.x)
    return frame[y0 : y0 + crop.height, x0 : x0 + crop.width]


@router.get("/recordings/{recording_id}/osd-debug")
async def osd_debug(
    recording_id: RowId,
    session: SessionDep,
    t: float = Query(0.0, ge=0.0, description="Offset into the recording, in seconds."),
):
    """What the overlay reader extracted at this moment, and what it made of it."""
    recording, frame, crop, extractor, info = await _read(recording_id, session, t)
    duration = recording.duration_s if recording.duration_s is not None else info.duration_s
    if duration is not None and t > duration + 0.05:
        raise HTTPException(
            422, f"frame cannot be decoded: offset {t:g}s is past the end of this recording"
        )

    glyphs = []
    mask = None
    if frame is not None:
        gray = frame[..., 0] if frame.ndim == 3 else frame
        strip = _crop_strip(gray, crop)
        mask = binarise(strip)
        glyphs = segment_glyphs(mask)

    text, confidence = ("", 0.0)
    if mask is not None and extractor.templates is not None:
        text, confidence = decode_line(mask, extractor.templates)

    settings = get_settings_service()
    reading = parse_osd_text(
        text,
        confidence=confidence,
        max_speed_kmh=float(settings.get_nowait("telemetry.max_speed_kmh")),
    )

    stored = (
        await session.execute(
            select(TelemetryPoint)
            .where(
                TelemetryPoint.recording_id == recording.id,
                TelemetryPoint.t_offset_s <= t,
            )
            .order_by(TelemetryPoint.t_offset_s.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    expected = recording.started_at + timedelta(seconds=t) if recording.started_at else None
    stored_out = None
    if stored is not None:
        quality = stored.quality_json or {
            "ocr_status": "legacy",
            "time_status": "valid" if stored.captured_at else "unknown",
            "gps_status": "valid" if stored.has_fix else "unknown",
            "gps_source": "direct" if stored.has_fix else "none",
            "interpolated": False,
            "problems": ["quality unavailable until telemetry is reprocessed"],
        }
        stored_out = {
            "t_offset_s": stored.t_offset_s,
            "dt_s": round(abs(stored.t_offset_s - t), 3),
            "captured_at": stored.captured_at.isoformat() if stored.captured_at else None,
            "lat": stored.lat,
            "lon": stored.lon,
            "has_fix": stored.has_fix,
            "speed_kmh": stored.speed_kmh,
            "heading_deg": stored.heading_deg,
            "ocr_confidence": stored.ocr_confidence,
            "raw_text": stored.raw_text,
            "quality": quality,
        }

    return {
        "recording_id": recording.id,
        "filename": recording.filename,
        "offset_s": t,
        "region": {"x": crop.x, "y": crop.y, "width": crop.width, "height": crop.height},
        "templates_loaded": extractor.templates is not None,
        "decoded_text": text,
        "confidence": round(confidence, 3),
        "glyphs": len(glyphs),
        "reread_available": frame is not None,
        "reread_error": None
        if frame is not None
        else "no frame could be decoded by the seeked re-reader at this offset",
        # The parsed result, so the two halves of a failure are distinguishable: text that
        # decoded wrongly looks nothing like text that decoded fine and failed validation.
        "parsed": {
            "captured_at": reading.captured_at.isoformat() if reading.captured_at else None,
            "lat": reading.lat,
            "lon": reading.lon,
            "has_fix": reading.has_fix,
            "speed_kmh": reading.speed_kmh,
            "problems": reading.problems or [],
            "time_status": reading.time_status,
            "gps_status": reading.gps_status,
        },
        # This is the canonical timeline used everywhere downstream. ``video_pts_s`` is
        # the requested browser-media time; the stored sample gives its exact processing
        # offset and delta rather than pretending a seek necessarily returned that frame.
        "timeline": {
            "frame_number": round(t * recording.fps) if recording.fps else None,
            "video_pts_s": t,
            "relative_time_s": t,
            "expected_at": expected.isoformat() if expected else None,
        },
        "stored_sample": stored_out,
        "image_url": f"/api/recordings/{recording.id}/osd-debug.png?t={t}",
    }


@router.get("/recordings/{recording_id}/osd-debug.png")
async def osd_debug_image(
    recording_id: RowId,
    session: SessionDep,
    t: float = Query(0.0, ge=0.0, description="Offset into the recording, in seconds."),
):
    """A composite of the frame, the strip, and what binarisation made of it.

    Three panels stacked, because the failures live between them. A strip that looks fine
    and a mask that is a solid block says the threshold is wrong; a clean mask that decodes
    to nonsense says the templates are; and a strip showing the road rather than the
    overlay says the region is. Reading only the final text cannot tell those apart, which
    is why every one of these bugs took a frame dump to find.
    """
    from PIL import Image, ImageDraw

    _recording, frame, crop, extractor, _info = await _read(recording_id, session, t)
    if frame is None:
        raise HTTPException(
            422,
            f"no frame could be decoded at {t:g}s; use the stored processing sample in "
            "the JSON diagnostic for the authoritative OCR result",
        )

    gray = frame[..., 0] if frame.ndim == 3 else frame
    strip = _crop_strip(gray, crop)
    mask = binarise(strip)

    strip_img = Image.fromarray(strip.astype(np.uint8)).convert("RGB")
    mask_img = Image.fromarray((mask * 255).astype(np.uint8)).convert("RGB")

    rgb = frame if frame.ndim == 3 else np.stack([frame] * 3, axis=-1)
    # Frames arrive BGR from the decoder; PIL expects RGB.
    preview = Image.fromarray(rgb[:, :, ::-1].astype(np.uint8))

    # Every panel shares one width, and that width is the strip's. The strip and the mask
    # are the panels being *read* — a glyph that is 20 px across in the source has to stay
    # legible, so they are never scaled down. Fitting the frame to them instead costs
    # nothing, since the strip was cut from a frame of exactly that width.
    width = max(strip_img.width, mask_img.width, 1)
    if preview.width != width:
        scale = width / preview.width
        preview = preview.resize((width, max(1, int(preview.height * scale))))
    else:
        scale = 1.0

    # Outline where the strip was taken from, so a misplaced region is obvious at a glance.
    ImageDraw.Draw(preview).rectangle(
        [
            crop.x * scale,
            crop.y * scale,
            (crop.x + crop.width) * scale - 1,
            (crop.y + crop.height) * scale - 1,
        ],
        outline=(255, 64, 64),
        width=3,
    )

    text, confidence = ("", 0.0)
    if extractor.templates is not None:
        text, confidence = decode_line(mask, extractor.templates)

    # Labels are ASCII on purpose: PIL's built-in bitmap font has no glyph for an em
    # dash and draws a replacement box, which looks like a rendering fault in a view whose
    # whole job is showing you what a rendering fault looks like.
    gap = 10
    caption_h = 34
    panels = [
        ("frame: the red box is the region being read", preview),
        ("cropped strip, as decoded", strip_img),
        ("after thresholding: what the classifier actually sees", mask_img),
    ]
    height = sum(p.height + caption_h + gap for _label, p in panels) + caption_h + gap
    canvas = Image.new("RGB", (width, height), (16, 16, 20))
    draw = ImageDraw.Draw(canvas)

    y = 0
    for label, panel in panels:
        draw.text((6, y + 8), label, fill=(150, 160, 180))
        y += caption_h
        canvas.paste(panel, (0, y))
        y += panel.height + gap

    decoded = text or "(nothing decoded)"
    draw.text(
        (6, y + 8),
        f"decoded: {decoded}    confidence {confidence:.2f}",
        fill=(150, 255, 170) if text else (255, 140, 140),
    )

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return Response(
        content=buffer.getvalue(),
        media_type="image/png",
        # Frames never change for a given recording and offset, but the region and the
        # templates can, so this is short-lived rather than immutable.
        headers={"Cache-Control": "private, max-age=60"},
    )
