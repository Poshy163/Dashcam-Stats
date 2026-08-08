"""The REST API.

Designed to be consumed by something other than this UI — Home Assistant, a script, a
dashboard — so responses are stable, paginated, and free of internal filesystem detail.
"""

from __future__ import annotations

from app.api.deps import Pagination, PaginationDep, SessionDep, SettingsDep
from app.api.errors import install_error_handlers

__all__ = [
    "Pagination",
    "PaginationDep",
    "SessionDep",
    "SettingsDep",
    "install_error_handlers",
]
