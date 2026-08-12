"""Application entrypoint.

One process serves the API, the built SPA and the background workers. That is deliberate:
the whole point of the deployment story is a single container with one port and no
external services.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import Response

from app.ai.runtime import describe_media_policy
from app.api.errors import install_error_handlers
from app.api.routes import content, heatmap, media, osd_debug, system
from app.api.schemas import HealthOut
from app.config import get_config
from app.core.logging import configure_logging, get_logger, install_db_sink, shutdown_db_sink
from app.core.settings_service import get_settings_service, init_settings_service
from app.db.session import dispose_engine, get_session_factory, init_db
from app.hardware.ffmpeg import media_health
from app.pipeline.stages import warm_models
from app.workers import queue
from app.workers.scheduler import get_scheduler
from app.workers.worker import get_worker_pool

log = get_logger(__name__)

#: Where the Vite build lands in the image. Absent in a backend-only dev run, which is
#: fine — the dev server proxies to this process instead.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

DESCRIPTION = """
Self-hosted dashcam footage analysis.

Indexes recordings, recovers GPS and speed from the camera's burned-in overlay, detects
and tracks vehicles, reads licence plates, and groups everything into journeys.

**Telemetry caveat:** this footage carries no GPS metadata. Position, speed and time are
read by OCR from an on-screen overlay that updates once per second, so coordinates carry
the precision the camera prints (about 11 m) and *heading and distance are derived* from
consecutive fixes rather than measured. G-force and event markers do not exist in this
footage and are never reported.
""".strip()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = get_config()
    config.ensure_dirs()
    configure_logging(config.log_level)
    log.info(
        "starting dashcam analyser",
        version=config.version,
        data_dir=str(config.data_dir),
        footage_dir=str(config.footage_dir),
    )

    await init_db()
    await init_settings_service(get_session_factory())

    settings = get_settings_service()
    # The database log sink starts after settings so it honours the configured level from
    # the first entry rather than defaulting and then correcting itself.
    install_db_sink(get_session_factory(), min_level=await settings.log_level())

    pool = get_worker_pool()
    scheduler = get_scheduler()
    # An explicit pause is an operator decision, not process-local state. Restore it before
    # either worker can claim one of the hundreds of queued bulk-reprocess jobs.
    queue.restore_pause_state()
    await pool.start()
    await scheduler.start()

    # Deliberately not awaited: compiling the models takes about a minute on the iGPU, and
    # blocking here would delay the health check and the UI for no benefit. The first job
    # simply waits on the same shared cache this fills.
    warm = asyncio.create_task(warm_models(), name="warm-models")

    try:
        yield
    finally:
        log.info("shutting down")
        warm.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await warm
        await scheduler.stop()
        await pool.stop()
        await shutdown_db_sink()
        await dispose_engine()


def create_app() -> FastAPI:
    config = get_config()
    app = FastAPI(
        title="Dashcam Analyser",
        description=DESCRIPTION,
        version=config.version,
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    if config.auth_username and config.auth_password:

        @app.middleware("http")
        async def optional_basic_auth(request: Request, call_next):
            if request.url.path == "/health":
                return await call_next(request)
            supplied = request.headers.get("Authorization", "")
            valid = False
            if supplied.startswith("Basic "):
                try:
                    decoded = base64.b64decode(supplied[6:], validate=True).decode("utf-8")
                    username, password = decoded.split(":", 1)
                    valid = hmac.compare_digest(
                        username, config.auth_username or ""
                    ) and hmac.compare_digest(password, config.auth_password or "")
                except (ValueError, UnicodeDecodeError):
                    valid = False
            if not valid:
                return Response(
                    "Authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Dashcam Analyser"'},
                )
            return await call_next(request)

    # Permissive because the intended deployment is a trusted LAN with no auth and users
    # may hit the API from Home Assistant or a script on another host. Tighten this before
    # exposing the app beyond a private network.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    app.include_router(system.router)
    app.include_router(content.router)
    app.include_router(media.router)
    app.include_router(heatmap.router)
    app.include_router(osd_debug.router)

    @app.get("/health", response_model=HealthOut, tags=["health"])
    async def health() -> Response:
        """Component health, used by the Docker HEALTHCHECK.

        Returns a non-200 when something is genuinely broken. A healthcheck that always
        says 200 is worse than none — it turns a dead container into a silently dead one.
        """
        detail: dict[str, object] = {}
        database = "healthy"
        try:
            from sqlalchemy import text

            factory = get_session_factory()
            async with factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            database = "unhealthy"
            detail["database_error"] = f"{type(exc).__name__}: {exc}"

        pool = get_worker_pool()
        scheduler = get_scheduler()
        worker = "healthy" if pool.healthy else "unhealthy"
        scanner = "healthy" if scheduler.healthy else "unhealthy"
        detail["active_jobs"] = len(pool.current_jobs())
        detail["workers"] = pool.worker_count

        # Never stat the footage path here. It is a hard NFS mount in production, and one
        # delayed metadata call inside the health endpoint blocks Uvicorn's only event loop.
        # The scanner reports mount availability without making UI liveness depend on the
        # storage server answering this request.

        # The Intel media slot, which is degraded rather than fatal on purpose. A stuck
        # ffmpeg child stops hardware decode and GPU inference but not the queue, and
        # returning 503 here would have Docker restart a container that is still working --
        # which is the loop this whole area exists to get out of.
        media = media_health()
        detail["media"] = media
        detail["policy"] = describe_media_policy()

        components = {"database": database, "scanner": scanner, "worker": worker}
        unhealthy = [k for k, v in components.items() if v != "healthy"]
        status_text = "healthy" if not unhealthy else "degraded"
        if media.get("status") != "healthy" and status_text == "healthy":
            status_text = "degraded"
        if database == "unhealthy":
            status_text = "unhealthy"

        payload = HealthOut(
            status=status_text,
            database=database,
            scanner=scanner,
            worker=worker,
            version=get_config().version,
            detail=detail,
        )
        return JSONResponse(
            status_code=200 if status_text != "unhealthy" else 503,
            content=payload.model_dump(mode="json"),
        )

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built SPA at / without shadowing the API."""
    if not FRONTEND_DIST.is_dir():
        log.warning("frontend build not found; serving API only", path=str(FRONTEND_DIST))

        @app.get("/", include_in_schema=False)
        async def _no_ui() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "Dashcam Analyser API is running, but the web UI was not built.",
                    "docs": "/api/docs",
                }
            )

        return

    assets = FRONTEND_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = FRONTEND_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(request: Request, full_path: str) -> Response:
        # Client-side routes must deep-link, so unknown paths fall back to index.html.
        # API prefixes are excluded explicitly: without this a typo'd endpoint would
        # return the HTML shell with a 200 instead of a 404, which is maddening to debug.
        if full_path.startswith(("api/", "media/", "stream/", "health")):
            return JSONResponse({"detail": "Not found"}, status_code=404)

        candidate = FRONTEND_DIST / full_path
        if (
            full_path
            and candidate.is_file()
            and candidate.resolve().is_relative_to(FRONTEND_DIST.resolve())
        ):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()


def main() -> None:
    import uvicorn

    config = get_config()
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        log_config=None,  # structlog owns logging; uvicorn's config would fight it
        access_log=False,
    )


if __name__ == "__main__":
    main()
