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

import numpy as np
from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.deps import SessionDep
from app.core.logging import get_logger
from app.core.paths import resolve_footage_path
from app.core.settings_service import get_settings_service
from app.db.models import OsdProfile, Recording
from app.hardware.ffmpeg import FFmpegError, iter_frames, probe
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


async def _read(recording_id: int, session, offset_s: float):
    """The frame at *offset_s*, the region, and the loaded extractor."""
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

    frame = None
    try:
        async for _offset, decoded in iter_frames(
            path,
            fps=1.0,
            crop=None,
            grayscale=False,
            hwaccel=hwaccel,
            start=offset_s,
            duration=1.0,
        ):
            frame = decoded
            break
    except FFmpegError as exc:
        # A diagnostic that answers a decode failure with a stack trace is worse than
        # useless: the thing being diagnosed is often precisely that the file will not
        # decode, and that is an answer, not a crash.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"could not decode this frame: {exc}"
        ) from exc
    if frame is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no frame could be decoded at that offset; it may be past the end of the file",
        )

    return recording, frame, crop, extractor


def _crop_strip(frame: np.ndarray, crop) -> np.ndarray:
    y0 = max(0, crop.y)
    x0 = max(0, crop.x)
    return frame[y0 : y0 + crop.height, x0 : x0 + crop.width]


@router.get("/recordings/{recording_id}/osd-debug")
async def osd_debug(
    recording_id: int,
    session: SessionDep,
    t: float = Query(0.0, ge=0.0, description="Offset into the recording, in seconds."),
):
    """What the overlay reader extracted at this moment, and what it made of it."""
    recording, frame, crop, extractor = await _read(recording_id, session, t)

    gray = frame[..., 0] if frame.ndim == 3 else frame
    strip = _crop_strip(gray, crop)
    mask = binarise(strip)
    glyphs = segment_glyphs(mask)

    text, confidence = ("", 0.0)
    if extractor.templates is not None:
        text, confidence = decode_line(mask, extractor.templates)

    settings = get_settings_service()
    reading = parse_osd_text(
        text,
        confidence=confidence,
        max_speed_kmh=float(settings.get_nowait("telemetry.max_speed_kmh")),
    )

    return {
        "recording_id": recording.id,
        "filename": recording.filename,
        "offset_s": t,
        "region": {"x": crop.x, "y": crop.y, "width": crop.width, "height": crop.height},
        "templates_loaded": extractor.templates is not None,
        "decoded_text": text,
        "confidence": round(confidence, 3),
        "glyphs": len(glyphs),
        # The parsed result, so the two halves of a failure are distinguishable: text that
        # decoded wrongly looks nothing like text that decoded fine and failed validation.
        "parsed": {
            "captured_at": reading.captured_at.isoformat() if reading.captured_at else None,
            "lat": reading.lat,
            "lon": reading.lon,
            "has_fix": reading.has_fix,
            "speed_kmh": reading.speed_kmh,
            "problems": reading.problems or [],
        },
        "image_url": f"/api/recordings/{recording.id}/osd-debug.png?t={t}",
    }


@router.get("/recordings/{recording_id}/osd-debug.png")
async def osd_debug_image(
    recording_id: int,
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

    _recording, frame, crop, extractor = await _read(recording_id, session, t)

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
