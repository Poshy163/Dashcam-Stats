"""Application entrypoint.

One process serves the API, the built SPA and the background workers. That is deliberate:
the whole point of the deployment story is a single container with one port and no
external services.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.ai.openvino_session import restore_gpu_failure_state
from app.ai.runtime import describe_media_policy
from app.api.errors import install_error_handlers
from app.api.routes import auth, content, heatmap, ingest, media, osd_debug, system
from app.api.schemas import HealthOut
from app.auth import service
from app.auth.gate import AuthGate, request_is_https
from app.auth.service import ensure_credential_loaded, require_login_setting, reset_auth_state
from app.config import get_config
from app.core.logging import configure_logging, get_logger, install_db_sink, shutdown_db_sink
from app.core.settings_service import get_settings_service, init_settings_service
from app.db.session import dispose_engine, get_session_factory, init_db
from app.hardware.ffmpeg import media_health
from app.ingest import origin
from app.ingest.poller import get_poller
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

    # Whether an account exists is answered from process state on every gated request, so
    # it is read once here rather than faulted in by whoever happens to knock first. The
    # reset in front of it matters for the restore path: a staged database is swapped in
    # by init_db above, and it may carry a different account from the one this process
    # started with.
    reset_auth_state()
    await ensure_credential_loaded()

    settings = get_settings_service()
    # The database log sink starts after settings so it honours the configured level from
    # the first entry rather than defaulting and then correcting itself.
    install_db_sink(get_session_factory(), min_level=await settings.log_level())

    pool = get_worker_pool()
    scheduler = get_scheduler()
    # An explicit pause is an operator decision, not process-local state. Restore it before
    # either worker can claim one of the hundreds of queued bulk-reprocess jobs.
    queue.restore_pause_state()
    # The same argument for the epoch a queue reset opens: the counters on the Queue page
    # are scoped to it, and a restart that forgot it would hand the current run the
    # previous one's failures and completions back.
    queue.restore_reset_epoch()
    # Before anything can compile a model on it. The disable is otherwise process-local,
    # and the process is being killed *by* the fault, so every restart re-armed the iGPU
    # and walked into the same native abort -- which is the crash loop itself.
    restore_gpu_failure_state()
    await pool.start()
    await scheduler.start()
    # Its own ticker rather than a scheduler task: the shared scheduler floors every
    # interval at thirty seconds, and the head unit is only on the network for a minute or
    # two while the engine runs.
    await get_poller().start()

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
        await get_poller().stop()
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
        # FastAPI registers this at the *root* by default, which would have been the one
        # route outside the gate's `/api` prefix rule. Nothing here uses OAuth, so rather
        # than carve out an exception the route simply does not exist.
        swagger_ui_oauth2_redirect_url=None,
    )

    app.add_middleware(AuthGate)

    # There is no CORS middleware here any more, and its absence is the tightening its own
    # comment asked for -- "tighten this before exposing the app beyond a private network"
    # is exactly the change being made.
    #
    # `allow_origins=["*"]` bought this application nothing. The SPA is served from this
    # same origin, so its requests were never subject to CORS at all, and the clients the
    # permissiveness was written for -- Home Assistant's REST sensor, curl, a script on
    # another host -- are not browsers and have never been bound by it either. What it did
    # buy was a real hole in the default configuration: with sign-in off, any page the
    # owner happened to visit could read `/api/map/routes` and `/api/plates` straight out
    # of their browser, which is to say the home address and the plate list.

    install_error_handlers(app)

    app.include_router(auth.router)
    app.include_router(system.router)
    app.include_router(content.router)
    app.include_router(media.router)
    app.include_router(heatmap.router)
    app.include_router(osd_debug.router)
    app.include_router(ingest.router)

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

        # `/health` cannot be gated -- the Docker HEALTHCHECK runs `curl -fsS` with no
        # credentials, and a 401 there restarts the container forever. So when the app is
        # guarded, the *detail* goes instead: worker counts, the ffmpeg policy, the version
        # and raw database exception strings are free reconnaissance for anyone who finds
        # the hostname, and the healthcheck reads only the status code.
        payload = HealthOut(
            status=status_text,
            database=database,
            scanner=scanner,
            worker=worker,
            version=get_config().version,
            detail={} if require_login_setting() else detail,
        )
        return JSONResponse(
            status_code=200 if status_text != "unhealthy" else 503,
            content=payload.model_dump(mode="json"),
        )

    _mount_frontend(app)
    return app


#: What the SPA shell is allowed to be cached as, and it is the whole of the blank-screen
#: fix.
#:
#: Neither this file nor the assets carried a ``Cache-Control`` header at all, and "no
#: header" does not mean "do not cache" -- it means the browser guesses, and the guess in
#: every major browser is a fraction of the document's age since ``Last-Modified``. So an
#: ``index.html`` that had been deployed a while was cached for hours *without revalidating*.
#:
#: That is fatal for this particular file, because it is the only unhashed thing in the
#: build. Every page is a lazy chunk called ``Backup-CEsM9GNL.js`` and the hash changes with
#: the contents, so a stale shell asks the server for chunk filenames that no longer exist,
#: gets a 404, and React unmounts to a white screen. A manual refresh fixed it because a
#: refresh is precisely what forces the revalidation that should have been happening anyway.
#:
#: ``no-cache`` is not ``no-store``: the file may still be stored, it just may not be reused
#: without asking. The cost of asking is one request for about a kilobyte, and only on a
#: full page load -- moving between pages is client-side routing and does not fetch the
#: shell at all. (``FileResponse`` answers that request with a 200 rather than a 304;
#: Starlette only does conditional requests in ``StaticFiles``. Not worth machinery for a
#: file this size.)
SHELL_CACHE_CONTROL = "no-cache"

#: And the other half. The asset filenames contain a content hash, so a given URL's bytes
#: can never change -- which makes them safe to cache for a year and never revalidate. This
#: is what stops "revalidate the shell every navigation" from meaning "refetch everything".
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


def _build_tag(index: Path) -> str:
    """A short fingerprint of the built SPA, from the shell's own bytes.

    The shell is the right thing to hash rather than the app's version string: it names
    every content-hashed chunk in the build, so it changes exactly when what a browser
    needs to fetch changes -- and it does so for ``main`` builds too, which all carry the
    version "main" and would otherwise be indistinguishable from each other.
    """
    try:
        return hashlib.sha256(index.read_bytes()).hexdigest()[:8]
    except OSError:
        # A missing shell is already handled by the caller; an unreadable one just means
        # the head unit's URL goes back to being stable, which is what it was before.
        return ""


class _ImmutableAssets(StaticFiles):
    """Vite's hashed build output, served as permanently cacheable."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return response


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
        app.mount("/assets", _ImmutableAssets(directory=assets), name="assets")

    index = FRONTEND_DIST / "index.html"
    # Whatever the head unit is sent has to change when the build does. See `backup_url`.
    origin.set_build_tag(_build_tag(index))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(request: Request, full_path: str) -> Response:
        # Client-side routes must deep-link, so unknown paths fall back to index.html.
        # API prefixes are excluded explicitly: without this a typo'd endpoint would
        # return the HTML shell with a 200 instead of a 404, which is maddening to debug.
        if full_path.startswith(("api/", "media/", "stream/", "health")):
            return JSONResponse({"detail": "Not found"}, status_code=404)

        # A browser has this app open, so it has just proved which address reaches it --
        # the one thing a bridged container cannot discover about itself, and the one the
        # dashcam's own browser needs in order to show the Backup page while a transfer
        # runs. Taken here rather than in middleware precisely because this route serves
        # the dashboard and nothing else: an API caller's idea of this app's address is
        # its own, and Home Assistant's is usually a container name no car could resolve.
        await origin.remember(request.url.scheme, request.headers.get("host", ""))

        redeemed = await _redeem_api_key(request, full_path)
        if redeemed is not None:
            return redeemed

        candidate = FRONTEND_DIST / full_path
        if (
            full_path
            and candidate.is_file()
            and candidate.resolve().is_relative_to(FRONTEND_DIST.resolve())
        ):
            # Unhashed root files -- the favicon, manifest, robots.txt -- so they get the
            # shell's treatment rather than the assets': revalidate, do not assume.
            return FileResponse(candidate, headers={"Cache-Control": SHELL_CACHE_CONTROL})
        return FileResponse(index, headers={"Cache-Control": SHELL_CACHE_CONTROL})


async def _redeem_api_key(request: Request, full_path: str) -> Response | None:
    """Trade a ``?k=`` in the URL for a cookie, and send the browser back without it.

    This is the whole of how the dashcam's head unit signs in. It is opened with
    ``am start -a android.intent.action.VIEW -d <url>`` and that URL is its only chance to
    present anything -- there is no keyboard in front of it, and no way to attach a header
    to a browser's first navigation.

    The redirect is the point, not politeness. Left in the address bar the key would be in
    the history of a screen that sits unlocked in a parked car, in the ``Referer`` of every
    tile the map loads, and in the URL the operator screenshots when something goes wrong.
    Redeeming it once and moving to the clean path keeps it to a single request.

    Returns None when there is no key to redeem, so the ordinary path is untouched.
    """
    presented = request.query_params.get(service.API_KEY_PARAM, "")
    if not presented:
        return None
    if await service.resolve_api_key(presented) is None:
        # Deliberately not an error page. A wrong key should land on the same login form as
        # no key at all -- telling the car's screen which of the two it was tells anyone
        # holding a guess the same thing.
        return None

    remaining = [
        (k, v) for k, v in request.query_params.multi_items() if k != service.API_KEY_PARAM
    ]
    query = urlencode(remaining)
    secure = request_is_https(request)
    response = RedirectResponse(f"/{full_path}{'?' + query if query else ''}", status_code=303)
    # This response carries the key as a Set-Cookie. Nothing may hold on to it.
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        service.SECURE_API_KEY_COOKIE_NAME if secure else service.API_KEY_COOKIE_NAME,
        presented,
        # No Max-Age. The key is a standing credential the operator revokes by blanking the
        # setting, not one that expires; the cookie lasting the browser session is enough,
        # and the unit is handed the URL again on the next transfer regardless.
        path="/",
        httponly=True,
        # Strict, unlike the session cookie. Nothing ever links into this app holding an
        # API key, so there is no inbound-link case to preserve -- and this cookie speaks
        # for the whole account with no password behind it.
        samesite="strict",
        secure=secure,
    )
    return response


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
