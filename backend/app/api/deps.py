"""Shared API dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Query
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

#: The largest integer SQLite can store, and therefore the largest that can name a row.
#:
#: This is a storage limit, not a policy: every primary key in the schema is an
#: autoincrementing integer, so an id outside ``[1, 2**63 - 1]`` does not identify a row
#: that is missing -- it identifies nothing that could ever exist. Passed through anyway,
#: it reaches the driver and raises ``OverflowError`` at bind time, which is an
#: ``ArithmeticError`` rather than a ``ValueError`` and so lands on the catch-all handler:
#: HTTP 500 and a stack trace written into the log table. Twenty-three parameters across
#: six route modules were unbounded, and the live logs carry the 500s to prove it.
SQLITE_MAX_INT = 2**63 - 1

#: A row id from the client. Bounded so a malformed one is refused at the edge.
RowId = Annotated[int, Path(ge=1, le=SQLITE_MAX_INT)]

#: The same, as an optional query filter.
RowIdFilter = Annotated[int | None, Query(ge=1, le=SQLITE_MAX_INT)]

#: Ceiling on the page number, for the same reason and with the same arithmetic behind it:
#: ``offset = (page - 1) * page_size`` is bound as a SQLite integer. A billion pages is
#: seventy thousand times the largest set this application can produce.
MAX_PAGE = 1_000_000_000


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
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
) -> Pagination:
    return Pagination(page=page, page_size=page_size)


PaginationDep = Annotated[Pagination, Depends(pagination)]


# Authentication would attach here: a dependency resolving the current principal, applied
# either per-router or globally in the app factory. The routes are written so adding it
# does not change their signatures — deliberately not implemented, since the deployment
# is a trusted LAN and half-built auth is worse than none.
