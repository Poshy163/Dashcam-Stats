"""Signing in, and managing the account that makes it possible.

Three of these are reachable without a session, because the login page calls them before it
has one: ``state``, ``login`` and ``logout``. The rest sit behind the gate like everything
else. :mod:`app.auth.gate` holds that list and is the only place it exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.api.deps import RowId, SessionDep
from app.api.schemas import (
    AuthSessionOut,
    AuthStateOut,
    CredentialClearRequest,
    CredentialRequest,
    LoginRequest,
    LoginResponse,
)
from app.auth import gate, ratelimit, service
from app.core.logging import get_logger
from app.core.settings_service import get_settings_service

log = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# --------------------------------------------------------------------------------------
# Public: what the login page needs
# --------------------------------------------------------------------------------------


@router.get("/state", response_model=AuthStateOut)
async def auth_state(request: Request) -> AuthStateOut:
    """Whether sign-in is on, and whether this browser has done it."""
    required = await service.sign_in_required()
    principal = await gate.authenticate(request)
    entitled = principal is not None or not required
    return AuthStateOut(
        required=required,
        authenticated=principal is not None,
        username=principal.username if principal else None,
        configured=service.credential_configured() if entitled else False,
        misconfigured=service.misconfigured() if entitled else False,
        can_claim_account=entitled and gate.client_is_local(request),
        remember_days=(
            int(get_settings_service().get_nowait("security.remember_days")) if entitled else None
        ),
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Exchange a username and password for a session cookie."""
    if len(body.password) > 1024:
        # Checked here rather than with `max_length` on the model. FastAPI serialises
        # Pydantic's error list, and in v2 each entry carries the offending `input` -- so a
        # model-level bound would have echoed the submitted password back in the 422 body,
        # and from there into any log or proxy that records response bodies.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That password is too long.")

    address = gate.client_ip(request) or "unknown"
    address_key = f"ip:{address}"
    # Keyed on the submitted name, not on a name known to exist: bucketing only real
    # accounts would answer "is this a valid username?" through the throttle itself.
    user_key = f"user:{body.username[:128]}"

    # Only the address bucket refuses anyone -- see `app.auth.ratelimit`. The username and
    # global buckets add delay instead, because a stranger must not be able to lock the
    # owner out of their own footage by failing to sign in on their behalf.
    wait = ratelimit.retry_after(address_key)
    if wait is not None:
        log.warning("login throttled", address=address, retry_after=round(wait))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many sign-in attempts from this address. Try again later.",
            headers={"Retry-After": str(max(1, int(wait)))},
        )
    delay = ratelimit.delay_for(user_key)
    if delay:
        await asyncio.sleep(delay)

    if not await service.verify_credential(body.username, body.password):
        ratelimit.record_failure(address_key, user_key)
        # No username, no password, no note of which was wrong -- in the message or in the
        # log line, which is surfaced verbatim on the Logs page.
        log.warning("failed sign-in attempt", address=address)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")

    ratelimit.record_success(address_key, user_key)
    token, expires_at = await service.create_session(
        remembered=body.remember,
        user_agent=request.headers.get("User-Agent"),
        client_ip=address,
    )
    _set_session_cookie(response, request, token, expires_at, remembered=body.remember)
    log.info("signed in", address=address, remembered=body.remember)
    return LoginResponse(
        authenticated=True,
        username=service.configured_username() or body.username,
        expires_at=expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> Response:
    """Revoke this browser's session and clear its cookie.

    Deliberately succeeds whether or not there was a session to revoke: the browser wants
    the cookie gone either way, and a 401 here would leave someone holding a cookie the
    server has already forgotten.
    """
    token = service.token_from_cookies(request.cookies)
    if token:
        await service.revoke_session(token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response, request)
    return response


# --------------------------------------------------------------------------------------
# Gated: managing the account
# --------------------------------------------------------------------------------------


@router.put("/credential", status_code=status.HTTP_204_NO_CONTENT)
async def set_credential(
    body: CredentialRequest, request: Request, session: SessionDep
) -> Response:
    """Create or replace the sign-in account.

    Every other session is signed out by this, including any the person changing the
    password did not know about — which is usually why they are changing it.
    """
    await service.ensure_credential_loaded()
    _require_local_first_claim(request)
    await _require_current_password(request, body.current_password)
    try:
        await service.set_credential(session, body.username, body.password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    # The caller just signed everything out, including themselves. Issue them a fresh
    # session so changing a password does not bounce them to the login page.
    token, expires_at = await service.create_session(
        remembered=False,
        user_agent=request.headers.get("User-Agent"),
        client_ip=gate.client_ip(request),
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookie(response, request, token, expires_at, remembered=False)
    return response


@router.post("/credential/clear", status_code=status.HTTP_204_NO_CONTENT)
async def clear_credential(
    body: CredentialClearRequest, request: Request, session: SessionDep
) -> Response:
    """Delete the account, ending sign-in entirely.

    ``security.require_login`` is switched off in the same operation. Leaving it on with
    nothing to sign in against is the one state that would lock the operator out of their
    own footage, and :func:`app.auth.service.clear_credential` refuses to create it.
    """
    await service.ensure_credential_loaded()
    await _require_current_password(request, body.current_password)
    await service.clear_credential(session)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response, request)
    return response


@router.get("/sessions", response_model=list[AuthSessionOut])
async def list_sessions(request: Request) -> list[AuthSessionOut]:
    """Every browser currently signed in."""
    principal = _principal(request)
    sessions = await service.list_sessions(
        current_session_id=principal.session_id if principal else None
    )
    return [AuthSessionOut(**asdict(row)) for row in sessions]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: RowId) -> Response:
    if not await service.revoke_session_id(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That session no longer exists.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(request: Request) -> dict[str, int]:
    """Sign out every other browser, leaving this one alone."""
    principal = _principal(request)
    removed = await service.revoke_all_sessions(
        keep_session_id=principal.session_id if principal else None
    )
    log.info("other sessions revoked", count=removed)
    return {"revoked": removed}


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _principal(request: Request) -> service.Principal | None:
    return getattr(request.state, "principal", None)


def _require_local_first_claim(request: Request) -> None:
    """The first account may only be claimed from this machine or this network.

    Sign-in is off by default, which means there is a window -- between the tunnel coming
    up and the password being set -- in which this endpoint is ungated and unauthenticated
    by design. Whoever finds the hostname during it could set a password of their own,
    switch sign-in on, and own the library while the owner gets a login page they cannot
    pass. It is a narrow window and the order in the README avoids it, but "we documented
    the order" is not a control.

    Only the *first* claim is restricted. Changing an existing account is governed by the
    current password instead, which works from anywhere.
    """
    if service.credential_configured() or gate.client_is_local(request):
        return
    log.warning("refused a remote attempt to claim the account", address=gate.client_ip(request))
    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        "Set the first password from your own network, or with the recovery command. "
        "Once an account exists it can be changed from anywhere.",
    )


async def _require_current_password(request: Request, current: str | None) -> None:
    """Re-check the existing password before the account is changed.

    Keyed on an account *existing*, not on sign-in being switched on. Those come apart in a
    state that is easy to reach and unpleasant to be in: an account set but the switch
    still off, where the gate lets everything through, so without this anyone who could
    reach the app could quietly overwrite the owner's password with their own.

    Before any account exists there is nothing to re-check and nothing to protect, and
    asking for a password nobody has set would be theatre.
    """
    if not service.credential_configured():
        return
    username = service.configured_username()
    if username is None:  # pragma: no cover - guarded by the line above
        return
    if not current or not await service.verify_credential(username, current):
        address = gate.client_ip(request) or "unknown"
        log.warning("account change refused: current password wrong", address=address)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Enter your current password to change the account."
        )


def _set_session_cookie(
    response: Response,
    request: Request,
    token: str,
    expires_at: datetime,
    *,
    remembered: bool,
) -> None:
    secure = gate.request_is_https(request)
    response.set_cookie(
        # `__Host-` over HTTPS, which locks the cookie to this exact hostname. See the
        # constant's own note: every sibling of dashcam.joshualeaper.dev is same-site to
        # it, and without the prefix any of them could write a cookie of this name.
        service.SECURE_COOKIE_NAME if secure else service.COOKIE_NAME,
        token,
        # No Max-Age without "stay signed in": the cookie dies with the browser, and the
        # row expires on its own a few hours later.
        max_age=(
            max(1, int((expires_at - datetime.now(UTC)).total_seconds())) if remembered else None
        ),
        path="/",
        httponly=True,
        # Lax, not Strict. Strict would drop the cookie on the first request of any inbound
        # link — following a shared journey URL would land on the login page despite being
        # signed in. What Lax does not cover, a sibling subdomain posting here, is covered
        # by the Origin check in the gate instead.
        samesite="lax",
        secure=secure,
    )


def _clear_session_cookie(response: Response, request: Request) -> None:
    secure = gate.request_is_https(request)
    # Both names. A deployment that has been reached over plain HTTP and over the tunnel
    # can hold one of each, and clearing only the one this request would have set leaves
    # the other behind for the browser to keep presenting.
    for name in (service.COOKIE_NAME, service.SECURE_COOKIE_NAME):
        response.delete_cookie(
            name,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure or name == service.SECURE_COOKIE_NAME,
        )
