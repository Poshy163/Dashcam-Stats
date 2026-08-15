"""The bulk data path: one TCP socket, one tar stream, off ``adbd`` entirely.

The unit serves ``tar c <files> | nc -l -p 9000`` and this side connects and unpacks. That
is the whole transport, and it measured 34.3 MB/s against the live unit where everything
routed through ``adbd`` capped at 10 MB/s regardless of parallelism -- see the table in
:mod:`app.ingest.adb`.

Two departures from the shell pipeline that proved the idea:

* The tar stream is read by :mod:`tarfile` in ``r|`` (streaming, non-seekable) mode rather
  than piped to a ``tar`` child. That removes ``netcat`` and ``tar`` from the image's
  requirements, gives exact byte accounting off the socket instead of inferring progress
  from ``tar xv`` chatter, and makes cancellation a flag this code checks rather than a
  signal sent to a pipeline.
* It runs on a worker thread with a blocking socket. The receive loop is pure I/O at tens
  of megabytes a second and has no business sharing the event loop that serves /health.

Resumability is per file and comes for free: whatever completed before the car pulled out
of the driveway is committed, and the size-based delta re-fetches the rest next time. That
is why this stays a single stream -- per-file connections would buy nothing and reintroduce
per-file setup cost on a link where setup was the entire original problem.
"""

from __future__ import annotations

import socket
import tarfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

#: How long to keep retrying the initial connect.
#:
#: The listener is launched detached and this side connects immediately afterwards, so a
#: few hundred milliseconds of "connection refused" while the unit gets ``nc`` up is
#: normal rather than an error.
CONNECT_RETRY_S = 10.0
#: Tight, because this interval is pure dead window. The listener comes up in a few hundred
#: milliseconds and the wait is whatever is left of the current gap when it does -- so the
#: interval is the error bar on the start of every transfer. A refused connection on the LAN
#: costs a round trip and an RST, which is nothing next to the footage the gap represents.
CONNECT_RETRY_INTERVAL_S = 0.05

#: Read timeout once connected. Generous: a stall this long means the car has gone.
SOCKET_TIMEOUT_S = 30.0

_READ_SIZE = 1 << 20


class TransferCancelled(RuntimeError):
    """The operator asked for the pull to stop, or the app is shutting down."""


@dataclass(slots=True)
class TransferResult:
    files: list[str] = field(default_factory=list)
    bytes_received: int = 0
    seconds: float = 0.0
    complete: bool = False
    error: str | None = None

    @property
    def throughput_mbs(self) -> float:
        if self.seconds <= 0 or not self.bytes_received:
            return 0.0
        return round(self.bytes_received / self.seconds / 1_000_000, 2)


class _CountingReader:
    """A file object over the socket that counts bytes and honours cancellation.

    ``tarfile`` in streaming mode only ever calls ``read``, so this is the natural place to
    both meter throughput and give the operator a way out: the flag is checked once per
    megabyte rather than per file, so Cancel takes effect mid-file.
    """

    def __init__(
        self,
        source,
        on_bytes: Callable[[int], None] | None,
        cancel: threading.Event | None,
    ) -> None:
        self._source = source
        self._on_bytes = on_bytes
        self._cancel = cancel
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        if self._cancel is not None and self._cancel.is_set():
            raise TransferCancelled("the transfer was cancelled")
        chunk = self._source.read(_READ_SIZE if size is None or size < 0 else size)
        if chunk:
            self.total += len(chunk)
            if self._on_bytes is not None:
                self._on_bytes(len(chunk))
        return chunk

    def close(self) -> None:
        self._source.close()


def _connect(host: str, port: int, deadline: float) -> socket.socket:
    last: OSError | None = None
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection((host, port), timeout=SOCKET_TIMEOUT_S)
            sock.settimeout(SOCKET_TIMEOUT_S)
            return sock
        except OSError as exc:
            last = exc
            time.sleep(CONNECT_RETRY_INTERVAL_S)
    raise OSError(f"could not connect to {host}:{port}: {last}")


def receive(
    host: str,
    port: int,
    staging: Path,
    *,
    expected: set[str] | None = None,
    on_file_started: Callable[[str], None] | None = None,
    on_file_done: Callable[[str], None] | None = None,
    on_bytes: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
) -> TransferResult:
    """Pull the tar stream into *staging*. Blocking; call from a worker thread.

    Never raises for an interrupted transfer. A window that closes mid-file is the normal
    case this whole design expects, so it comes back as ``complete=False`` with whatever
    did arrive listed in ``files`` -- the caller commits those and lets the delta pick up
    the rest.
    """
    started = time.monotonic()
    result = TransferResult()
    if not staging.parent.is_dir():
        # Never ``parents=True``. The staging directory lives inside the footage share, so
        # creating its parent would materialise the share's own mount point and write a
        # whole card underneath it -- invisible once the real share mounts back over the
        # top, and gone for good if delete-after-verify then cleared the camera.
        result.error = f"the footage directory {staging.parent} does not exist"
        result.seconds = time.monotonic() - started
        return result
    staging.mkdir(exist_ok=True)

    try:
        sock = _connect(host, port, started + CONNECT_RETRY_S)
    except OSError as exc:
        result.error = str(exc)
        result.seconds = time.monotonic() - started
        return result

    reader: _CountingReader | None = None
    try:
        with sock, sock.makefile("rb", buffering=_READ_SIZE) as stream:
            reader = _CountingReader(stream, on_bytes, cancel)
            # ``r|`` is the streaming reader: it never seeks, which is the only thing that
            # works over a socket.
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    if not member.isfile():
                        continue
                    name = Path(member.name).name
                    if not name or name.startswith(".") or "/" in member.name:
                        # The unit is asked for a flat list of its own recordings; anything
                        # with a path in it is not something this should be writing.
                        log.warning("skipped an unexpected archive member", member=member.name)
                        continue
                    if on_file_started is not None:
                        on_file_started(name)
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    target = staging / name
                    with open(target, "wb") as handle:
                        while chunk := extracted.read(_READ_SIZE):
                            handle.write(chunk)
                    result.files.append(name)
                    if on_file_done is not None:
                        on_file_done(name)
            # Reaching the end of the iteration is not proof of a complete transfer. A
            # socket that dies exactly on a 512-byte member boundary looks to tarfile like
            # the end of the archive, so the only honest test is whether everything asked
            # for actually arrived.
            missing = (expected or set()) - set(result.files)
            result.complete = not missing
            if missing:
                result.error = f"the stream ended with {len(missing)} file(s) still to come"
    except TransferCancelled as exc:
        result.error = str(exc)
    except (OSError, tarfile.TarError, EOFError) as exc:
        # The expected ending when the car leaves: the socket dies mid-member. Everything
        # already written is still good and is committed by size.
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if reader is not None:
            result.bytes_received = reader.total
        result.seconds = time.monotonic() - started

    return result
