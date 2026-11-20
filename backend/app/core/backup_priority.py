"""Coordinate server-side footage transfer and video processing in the shared event loop."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.core.settings_service import get_settings_service

_active_footage_transfers = 0


@asynccontextmanager
async def prioritize_footage_transfer() -> AsyncIterator[None]:
    """Give a footage pass priority across its listeners and retries.

    Registering a transfer never waits for the database or for previously admitted
    jobs. Cancelling one transfer releases only its own claim on the share.
    """
    global _active_footage_transfers
    _active_footage_transfers += 1
    try:
        yield
    finally:
        _active_footage_transfers -= 1


def backup_priority_active() -> bool:
    return _active_footage_transfers > 0 and bool(
        get_settings_service().get_nowait("processing.prioritize_footage_backup")
    )
