"""Footage discovery and change detection."""

from __future__ import annotations

from app.scanner.discovery import (
    Scanner,
    ScanSummary,
    pending_count,
    queue_unprocessed,
)
from app.scanner.fingerprint import fingerprint_file, full_hash
from app.scanner.naming import (
    ParsedName,
    find_time_pair,
    parse_recording_name,
    resolve_camera,
)

__all__ = [
    "ParsedName",
    "ScanSummary",
    "Scanner",
    "find_time_pair",
    "fingerprint_file",
    "full_hash",
    "parse_recording_name",
    "pending_count",
    "queue_unprocessed",
    "resolve_camera",
]
