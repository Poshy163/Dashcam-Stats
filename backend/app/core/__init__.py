"""Cross-cutting infrastructure: logging, path safety and the settings schema.

Only leaf modules live here. Nothing in this package may import ``app.db``, ``app.api`` or
``app.pipeline`` at module scope -- the database log sink needs ``app.db.models`` and
imports it inside the writer for exactly that reason.
"""

from __future__ import annotations

from app.core.logging import (
    DatabaseLogSink,
    bind_job,
    bind_log_context,
    bind_recording,
    clear_log_context,
    configure_logging,
    db_sink_lifespan,
    get_db_sink,
    get_logger,
    install_db_sink,
    log_context,
    prune_logs,
    redact,
    redact_text,
    set_log_level,
    shutdown_db_sink,
    unbind_log_context,
)
from app.core.paths import (
    PathTraversalError,
    data_root,
    directory_size,
    footage_root,
    is_mount_point,
    is_within,
    is_writable,
    media_root,
    relative_to_footage,
    relative_to_media,
    resolve_footage_path,
    resolve_media_path,
    safe_join,
    same_filesystem,
)

__all__ = [
    "DatabaseLogSink",
    "PathTraversalError",
    "bind_job",
    "bind_log_context",
    "bind_recording",
    "clear_log_context",
    "configure_logging",
    "data_root",
    "db_sink_lifespan",
    "directory_size",
    "footage_root",
    "get_db_sink",
    "get_logger",
    "install_db_sink",
    "is_mount_point",
    "is_within",
    "is_writable",
    "log_context",
    "media_root",
    "prune_logs",
    "redact",
    "redact_text",
    "relative_to_footage",
    "relative_to_media",
    "resolve_footage_path",
    "resolve_media_path",
    "safe_join",
    "same_filesystem",
    "set_log_level",
    "shutdown_db_sink",
    "unbind_log_context",
]
