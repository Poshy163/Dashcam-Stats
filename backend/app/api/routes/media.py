"""Serving thumbnails, crops and video.

Every path here originates from a client and is resolved through the path guards, so a
crafted request cannot reach outside ``/data/media`` or the footage root. Clients never
supply absolute paths — video is addressed by recording id, and the server looks up where
that actually lives.
"""

from __future__ import annotations

import mimetypes
import re

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.api.deps import SessionDep
from app.core.logging import get_logger
from app.core.paths import PathTraversalError, resolve_footage_path, resolve_media_path
from app.db.models import Recording

log = get_logger(__name__)

router = APIRouter(tags=["media"])

#: Chunk size for range responses. Large enough that seeking is not chatty, small enough
#: that a seek does not stall behind a huge read.
_CHUNK = 1 << 20

_RANGE_RE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")


@router.get("/media/{path:path}", summary="Thumbnail or crop image")
async def get_media(path: str) -> FileResponse:
    try:
        resolved = resolve_media_path(path, must_exist=True)
    except PathTraversalError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid media path") from None
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found") from None

    return FileResponse(
        resolved,
        media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
        # Media is content-addressed by recording and track id and never rewritten in
        # place, so it can be cached hard.
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = _RANGE_RE.fullmatch(header.strip())
    if not match:
        return None
    raw_start, raw_end = match.group("start"), match.group("end")

    if not raw_start and not raw_end:
        return None
    if not raw_start:
        # Suffix form: the last N bytes.
        length = int(raw_end)
        if length <= 0:
            return None
        start = max(0, size - length)
        return start, size - 1

    start = int(raw_start)
    end = int(raw_end) if raw_end else size - 1
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


@router.get("/stream/{recording_id}", summary="Stream a recording")
async def stream_recording(
    recording_id: int,
    session: SessionDep,
    request: Request,
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    """Serve video with HTTP range support.

    Range handling is not optional: without it the browser cannot seek, so clicking a
    detection on the timeline would restart playback from the beginning instead of
    jumping to the moment in question.
    """
    recording = await session.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    if recording.file_missing:
        raise HTTPException(status.HTTP_410_GONE, "This recording is no longer on disk")

    try:
        path = resolve_footage_path(recording.rel_path, must_exist=True)
    except PathTraversalError:
        log.error("stored recording path failed validation", recording_id=recording_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid recording path") from None
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file not found") from None

    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp2t"
    base_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}

    if not range_header:
        return FileResponse(path, media_type=media_type, headers=base_headers)

    span = _parse_range(range_header, size)
    if span is None:
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
        )

    start, end = span
    length = end - start + 1

    async def body():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        body(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers={
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )
