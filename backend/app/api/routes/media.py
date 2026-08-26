"""Serving thumbnails, crops and video.

Every path here originates from a client and is resolved through the path guards, so a
crafted request cannot reach outside ``/data/media`` or the footage root. Clients never
supply absolute paths — video is addressed by recording id, and the server looks up where
that actually lives.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.api.deps import RowId, SessionDep
from app.core.logging import get_logger
from app.core.paths import PathTraversalError, resolve_footage_path, resolve_media_path
from app.db.models import Recording
from app.media import ensure_playable
from app.media.playback import ensure_export_clip

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
        # place, so it can be cached hard -- but only by the browser that asked for it.
        #
        # `public` was actively dangerous behind a CDN. These paths are `.jpg` at
        # sequential zero-padded ids (`thumbnails/00000123.jpg`,
        # `plates/00000012/...`), which is on Cloudflare's default cacheable list, and a
        # CDN cache key does not include the session cookie. One signed-in look at the
        # recordings grid would have populated the edge with seven days of thumbnails and
        # licence-plate crops that anyone could then enumerate without the request ever
        # reaching this process -- the gate cannot refuse a request it never sees.
        #
        # `private` unconditionally, not only while sign-in is on: objects cached during
        # an unauthenticated period outlive the moment it is switched on.
        headers={"Cache-Control": "private, max-age=604800, immutable"},
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
    recording_id: RowId,
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

    # Both the resolution and the stat below can enter a hard network mount, so neither runs
    # on the event loop. Everything else in the process -- /health, the SPA, the queue polls,
    # the workers' heartbeats -- is behind whichever of them the storage server is slowest
    # to answer.
    try:
        path = await asyncio.to_thread(resolve_footage_path, recording.rel_path, must_exist=True)
    except PathTraversalError:
        log.error("stored recording path failed validation", recording_id=recording_id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid recording path") from None
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file not found") from None

    # The camera records MPEG-TS, which no browser can demux. The H.264 inside is fine,
    # so this hands back a remuxed MP4 (copied, not re-encoded) and serves that instead.
    playable = await ensure_playable(path, recording_id)
    path = playable.path

    size = (await asyncio.to_thread(path.stat)).st_size
    media_type = playable.media_type
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
        # Read in a worker thread, one chunk at a time.
        #
        # ``FileResponse`` (the no-range branch above) already does this through anyio; this
        # branch is hand-rolled and did not, so every seek in the player -- and a browser
        # asking for ``bytes=0-``, which is most of them -- pulled the whole clip through
        # the event loop a megabyte at a time. On the NFS mount this design assumes, one
        # slow read stalls the entire process.
        fh = await asyncio.to_thread(path.open, "rb")
        try:
            await asyncio.to_thread(fh.seek, start)
            remaining = length
            while remaining > 0:
                chunk = await asyncio.to_thread(fh.read, min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            await asyncio.to_thread(fh.close)

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


@router.get("/api/recordings/{recording_id}/export.mp4", summary="Download an MP4 clip")
async def export_recording(
    recording_id: RowId,
    session: SessionDep,
    start: float = Query(0.0, ge=0.0),
    end: float | None = Query(None, gt=0.0),
) -> FileResponse:
    recording = await session.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording not found")
    if recording.file_missing:
        raise HTTPException(status.HTTP_410_GONE, "This recording is no longer on disk")
    if end is not None and end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "End must be after start")
    if recording.duration_s is not None and start >= recording.duration_s:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Start is outside the recording")
    try:
        # Off the loop, like its sibling in `stream_recording`: this is the call that
        # enters the network mount.
        source = await asyncio.to_thread(resolve_footage_path, recording.rel_path, must_exist=True)
        clip = await ensure_export_clip(source, recording.id, start_s=start, end_s=end)
    except (PathTraversalError, FileNotFoundError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recording file not found") from None
    name = f"{recording.filename.rsplit('.', 1)[0]}-{start:.0f}s.mp4"
    # An exported clip carries no cache headers of its own, and `.mp4` is another
    # extension a CDN caches by default. Said explicitly for the same reason as above.
    return FileResponse(
        clip,
        media_type="video/mp4",
        filename=name,
        headers={"Cache-Control": "private, no-store"},
    )
