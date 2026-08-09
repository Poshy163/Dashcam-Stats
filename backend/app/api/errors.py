"""Consistent JSON errors.

Internal paths and stack traces never reach a client: a traversal attempt should learn
nothing about the server's filesystem beyond "no".
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger
from app.core.paths import PathTraversalError

log = get_logger(__name__)


def _envelope(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}, "detail": message},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PathTraversalError)
    async def _traversal(request: Request, exc: PathTraversalError) -> JSONResponse:
        # Logged with the offending value, returned without it.
        log.warning("path traversal refused", path=str(request.url.path), error=str(exc))
        return _envelope(
            "invalid_path", "The requested path is not permitted.", status.HTTP_400_BAD_REQUEST
        )

    @app.exception_handler(FileNotFoundError)
    async def _missing(_request: Request, _exc: FileNotFoundError) -> JSONResponse:
        return _envelope(
            "not_found", "The requested file is not available.", status.HTTP_404_NOT_FOUND
        )

    @app.exception_handler(ValueError)
    async def _bad_value(_request: Request, exc: ValueError) -> JSONResponse:
        return _envelope("invalid_request", str(exc), status.HTTP_400_BAD_REQUEST)

    @app.exception_handler(OverflowError)
    async def _out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
        """The backstop, not the fix.

        ``OverflowError`` is an ``ArithmeticError``, not a ``ValueError``, so the handler
        above never sees it and every instance landed on the catch-all below: HTTP 500 with
        a stack trace, written into the log table, for input as ordinary as a date picker
        scrolled back to year one. The parameter bounds in ``deps.py`` are what actually
        prevent it; this catches whatever is added later without them.
        """
        log.warning("value out of range", path=str(request.url.path), error=str(exc))
        return _envelope(
            "out_of_range",
            "A value in the request is out of range.",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception(
            "unhandled API error", path=str(request.url.path), error=f"{type(exc).__name__}: {exc}"
        )
        return _envelope(
            "internal_error",
            "Something went wrong. Check the application logs for details.",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
