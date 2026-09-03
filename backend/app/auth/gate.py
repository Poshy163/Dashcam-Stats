"""The request gate.

Pure ASGI rather than ``@app.middleware("http")``. Starlette's ``BaseHTTPMiddleware``
wrapper puts an anyio task and a memory stream around every response, which is a real cost
on the two hot paths this application actually has -- a recordings grid firing fifty
thumbnail requests, and a video seek firing range requests as fast as the scrubber moves.
Nothing here needs to see the response, so nothing here should be in its way.

What stays reachable while signed out is chosen for one reason: the login page has to be
able to render. That means the SPA shell, its bundles, the favicon, and the three auth
endpoints the page itself calls. ``/health`` stays open too, because the Docker healthcheck
runs before anyone has signed in and a container that reports unhealthy the moment sign-in
is enabled would restart forever.

Everything carrying data -- ``/api``, ``/media``, ``/stream``, and the OpenAPI docs, which
sit under ``/api`` and describe the lot -- is refused without a principal. The shell being
public gives away nothing: it is a public open-source bundle that renders a login form
until an API call succeeds.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.service import (
    API_KEY_COOKIE_NAME,
    API_KEY_HEADER,
    API_KEY_PARAM,
    SECURE_API_KEY_COOKIE_NAME,
    Principal,
    resolve_api_key,
    resolve_basic,
    resolve_session,
    sign_in_required,
    token_from_cookies,
)

#: Path prefixes that carry footage, telemetry, settings or system detail.
GATED_PREFIXES = ("/api/", "/media/", "/stream/")

#: Exactly the endpoints the login page needs before it has a session. Matched exactly, not
#: by prefix: ``/api/auth/credential`` and ``/api/auth/sessions`` are emphatically not here.
PUBLIC_PATHS = frozenset(
    {
        "/api/auth/state",
        "/api/auth/login",
        "/api/auth/logout",
        # Not a login-page endpoint, and the one deliberate exception to the paragraph
        # above. The caller is the head unit's detached radio watchdog: a shell script,
        # fifteen seconds from the unit sleeping, reporting whether Bluetooth and the
        # hotspot actually came back. It cannot hold a session, and giving it this app's
        # API key so that it could would put a credential good for every route in this
        # application onto a device the app does not own -- to buy one POST that carries no
        # footage, no telemetry and no settings. It authenticates instead with a token
        # minted for a single transition and compared in constant time; a request without
        # one changes nothing and is told only that it was not accepted.
        "/api/ingest/radio-recovery",
    }
)

#: Methods that change something, and therefore the ones an ``Origin`` is checked on.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Ways of authenticating that a *browser* attaches by itself, and which therefore ride
#: along on a request some other site caused. The API key is here because it is handed out
#: as a cookie once accepted -- without that it would be the one credential in this
#: application that a sibling subdomain could spend. Nothing is lost by including it: a
#: script presenting the key in a header sends no ``Origin`` at all, and the check passes
#: any request that has none.
_COOKIE_BORNE = frozenset({"session", "apikey"})


class AuthGate:
    """Refuses data requests that carry neither a session cookie nor Basic credentials."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not _is_gated(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        if not await sign_in_required():
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        principal = await authenticate(request)
        if principal is None:
            await _challenge(request)(scope, receive, send)
            return

        if principal.method in _COOKIE_BORNE and not _origin_is_ours(request):
            await _refuse_cross_origin()(scope, receive, send)
            return

        scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


async def authenticate(request: Request) -> Principal | None:
    """Resolve the request's principal from its cookie, its API key, or Basic credentials."""
    token = token_from_cookies(request.cookies)
    if token:
        principal = await resolve_session(token)
        if principal is not None:
            return principal
    presented = api_key_from_request(request)
    if presented:
        principal = await resolve_api_key(presented)
        if principal is not None:
            return principal
    header = request.headers.get("Authorization", "")
    if header:
        return await resolve_basic(header, address=client_ip(request) or "unknown")
    return None


def api_key_from_request(request: Request) -> str:
    """An API key presented as a cookie, a header, or a query parameter.

    All three, because one caller needs each. The head unit arrives with the key in the
    query string, since a browser navigation carries nothing else; it is given the cookie
    on that first request and uses it for every fetch the page makes afterwards; and a
    script or a Home Assistant sensor is better served by a header than by a key in a URL
    that ends up in access logs.
    """
    from_cookie = request.cookies.get(SECURE_API_KEY_COOKIE_NAME) or request.cookies.get(
        API_KEY_COOKIE_NAME
    )
    return (
        from_cookie
        or request.headers.get(API_KEY_HEADER, "")
        or request.query_params.get(API_KEY_PARAM, "")
    )


def _is_gated(path: str) -> bool:
    return path not in PUBLIC_PATHS and path.startswith(GATED_PREFIXES)


def _origin_is_ours(request: Request) -> bool:
    """Refuse a state-changing request a cookie was attached to by another site.

    ``SameSite=Lax`` is scoped to the registrable domain, and this deployment's is
    ``joshualeaper.dev`` -- so every other host under it is same-site, and a page on any of
    them can POST here with the session cookie riding along. That reaches
    ``/api/retention/run``, ``/api/system/database/restore`` and ``/api/settings``, which is
    enough to delete footage, replace the database or switch sign-in back off.

    Only cookie-authenticated requests are checked. A browser sends ``Origin`` on every
    unsafe request including its own; Home Assistant and curl send none and authenticate
    with Basic anyway, so nothing scripted is affected.
    """
    if request.method not in UNSAFE_METHODS:
        return True
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    host = request.headers.get("Host")
    if not host:
        return False
    scheme = "https" if request_is_https(request) else "http"
    return origin == f"{scheme}://{host}"


def _challenge(request: Request) -> JSONResponse:
    headers = {}
    if request.headers.get("Authorization"):
        # Sent only to a caller that already tried to speak Basic, which is a script or a
        # Home Assistant sensor with the wrong credentials. Sending it unconditionally
        # would put the browser's own native password box in front of the login page, and
        # there is no way to sign out of one of those.
        headers["WWW-Authenticate"] = 'Basic realm="Dashcam Analyser", charset="UTF-8"'
    return JSONResponse(
        status_code=401,
        content={
            "error": {"code": "unauthenticated", "message": "Sign in to continue."},
            "detail": "Sign in to continue.",
        },
        headers=headers,
    )


def _refuse_cross_origin() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "cross_origin_refused",
                "message": "This request came from another site.",
            },
            "detail": "This request came from another site.",
        },
    )


# --------------------------------------------------------------------------------------
# What the proxy tells us about the caller
# --------------------------------------------------------------------------------------


def _peer_is_trusted(request: Request) -> bool:
    """Whether the socket's peer is entitled to speak for someone else.

    Forwarded headers are claims, not facts, and they are only worth reading when the thing
    making the claim is the proxy in front of us. With the tunnel, that is ``cloudflared``
    on the Docker network -- a private address. A request arriving from a public address is
    talking to a directly published port, and anything it says about who it really is was
    written by whoever sent it.
    """
    host = request.client.host if request.client else None
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def client_ip(request: Request) -> str | None:
    """The caller's address as best it can be known.

    Behind a Cloudflare Tunnel, ``CF-Connecting-IP`` is set by Cloudflare and cannot be
    influenced from outside, so it is preferred. ``X-Forwarded-For`` is next for any other
    proxy, and the socket's peer address last -- and is the only thing used at all when the
    peer is not one we would let speak for someone else.
    """
    peer = request.client.host if request.client else None
    if not _peer_is_trusted(request):
        return peer
    forwarded = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or peer
    return peer


def client_is_local(request: Request) -> bool:
    """Whether the caller is on this machine or this network, rather than out on the internet."""
    resolved = client_ip(request)
    try:
        address = ipaddress.ip_address(resolved or "")
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def request_is_https(request: Request) -> bool:
    """Whether the *browser's* leg of this request used TLS.

    Cloudflare terminates TLS and reaches the container over plain HTTP, so the scheme on
    this end says nothing. ``X-Forwarded-Proto`` carries the answer, and is read directly
    rather than through uvicorn's proxy-header handling -- that defaults to trusting only
    ``127.0.0.1``, and ``cloudflared`` is a different container, so the scheme would have
    read ``http`` forever and the cookie would never have been marked ``Secure``.
    """
    if _peer_is_trusted(request):
        proto = request.headers.get("X-Forwarded-Proto")
        if proto:
            return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"
