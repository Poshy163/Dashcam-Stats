"""Shared API dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SettingsService, get_settings_service
from app.db.session import get_session

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def settings_dep() -> SettingsService:
    return get_settings_service()


SettingsDep = Annotated[SettingsService, Depends(settings_dep)]

#: Hard ceiling on page size. Every list endpoint paginates because the library is
#: expected to reach terabytes, and an unbounded page would let one request pull the
#: entire detection table into memory.
MAX_PAGE_SIZE = 200


@dataclass(slots=True)
class Pagination:
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def pages(self, total: int) -> int:
        return max(1, -(-total // self.page_size))


def pagination(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
) -> Pagination:
    return Pagination(page=page, page_size=page_size)


PaginationDep = Annotated[Pagination, Depends(pagination)]


# Authentication would attach here: a dependency resolving the current principal, applied
# either per-router or globally in the app factory. The routes are written so adding it
# does not change their signatures — deliberately not implemented, since the deployment
# is a trusted LAN and half-built auth is worse than none.
