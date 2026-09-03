"""Asking the access point to bounce the head unit onto 5 GHz.

Why this exists at all: **nothing on the unit can change its band.** Measured on the live
hardware -- the Wi-Fi firmware owns BSSID selection (``dumpsys wifi`` logs "No partial scan
because firmware roaming is supported" every twenty seconds), both radios are one saved
network, and the non-privileged shell on this build has no ``connect-network``, no
``disconnect`` and no roam verb; ``add-suggestion`` exists but needs root approval on a
``user`` build, and ``wifi_frequency_band`` is absent from this ROM. So the selection nudge
in :mod:`app.ingest.band` makes Android re-evaluate every cycle and it *still* cannot move a
healthy 2.4 GHz link -- observed sitting on 2.4 GHz at -57 dBm with the 5 GHz radio of the
same access point twenty decibels stronger at -37 dBm.

The access point can. Disassociating the client makes it re-associate from scratch, and a
fresh association picks the strongest candidate -- which is the 5 GHz radio wherever the car
actually parks. That is the whole of this module: one ``kick-sta`` against UniFi OS.

Two things it is deliberately not. It is not a roaming policy: one bounce per visit, behind
a cooldown, and a failure is never allowed to hold up a backup -- a slow transfer beats no
transfer. And it is not a place for the operator's console password to sit in
``app_settings``: every value there is echoed by ``GET /api/settings`` to any authenticated
browser, so the credential lives in its own table exactly as the sign-in password does (see
:class:`app.db.models.UnifiCredential`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import UnifiCredential
from app.db.session import session_scope

log = get_logger(__name__)

#: The single credential row, same convention as the sign-in account.
CREDENTIAL_ID = 1

#: Ceiling on every call here. The console is on the same LAN as this app; anything slower
#: than this is a console that is not going to answer inside a driveway window either.
REQUEST_TIMEOUT_S = 10.0

#: A client MAC as UniFi wants it, and the only thing ever put into the request body.
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")


class UnifiError(RuntimeError):
    """The console could not be reached, or refused the call."""


@dataclass(frozen=True, slots=True)
class UnifiConfig:
    """Everything needed for one call, resolved from settings plus the credential row."""

    base_url: str
    site: str
    verify_tls: bool
    api_key: str | None
    username: str | None
    password: str | None

    @property
    def configured(self) -> bool:
        return bool(self.base_url) and bool(self.api_key or (self.username and self.password))


def _setting(key: str, default: object) -> object:
    try:
        return get_settings_service().get_nowait(key)
    except Exception:
        return default


async def load_config() -> UnifiConfig:
    """Settings plus the stored credential. Never raises."""
    base = str(_setting("ingest.unifi_url", "") or "").strip().rstrip("/")
    site = str(_setting("ingest.unifi_site", "default") or "default").strip() or "default"
    verify = bool(_setting("ingest.unifi_verify_tls", False))
    api_key = username = password = None
    try:
        async with session_scope() as session:
            row = await session.get(UnifiCredential, CREDENTIAL_ID)
            if row is not None:
                api_key = row.api_key or None
                username = row.username or None
                password = row.password or None
    except Exception as exc:  # pragma: no cover - a database that is already failing
        log.debug("could not read the UniFi credential", error=str(exc))
    return UnifiConfig(base, site, verify, api_key, username, password)


def _build_client(config: UnifiConfig) -> httpx.AsyncClient:
    """The HTTP client for one call.

    ``verify`` defaults off because a UniFi console presents a self-signed certificate for
    its LAN address; requiring a valid chain would mean this feature simply never works.
    It is a setting rather than a constant so an operator who has put a real certificate on
    the console can turn verification back on.
    """
    return httpx.AsyncClient(
        base_url=config.base_url,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        verify=config.verify_tls,
        follow_redirects=False,
    )


def _redact(text: str, config: UnifiConfig) -> str:
    """Keep the key and password out of anything a human or a log will see."""
    for secret in (config.api_key, config.password):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


async def _authenticate(client: httpx.AsyncClient, config: UnifiConfig) -> dict[str, str]:
    """Return the headers that authorise a call, logging in first if there is no API key.

    The API key path is preferred and needs no session: it is a header, it is revocable
    from the console, and it cannot be replayed into a UI login. The username/password path
    exists because API keys are only offered by recent releases, and it has to carry the
    CSRF token UniFi OS hands back, or every write is refused.
    """
    if config.api_key:
        return {"X-API-KEY": config.api_key, "Accept": "application/json"}

    response = await client.post(
        "/api/auth/login",
        json={"username": config.username, "password": config.password},
    )
    if response.status_code >= 400:
        raise UnifiError(f"the console refused the sign-in ({response.status_code})")
    headers = {"Accept": "application/json"}
    # UniFi OS returns the CSRF token in a header on the login response; the session cookie
    # is kept by the client itself.
    token = response.headers.get("x-csrf-token") or response.headers.get("X-CSRF-Token")
    if token:
        headers["X-CSRF-Token"] = token
    return headers


async def _network_api(
    client: httpx.AsyncClient,
    config: UnifiConfig,
    headers: dict[str, str],
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> httpx.Response:
    """Call the Network application behind the UniFi OS proxy.

    The ``/proxy/network`` prefix is what distinguishes a UniFi OS console (a Dream
    Machine, a second-generation Cloud Key) from the old standalone controller. This
    deployment's console answered ``/proxy/network/status`` with a clean 401, which is how
    the shape was established rather than guessed.
    """
    url = f"/proxy/network/api/s/{config.site}{path}"
    if payload is None:
        return await client.get(url, headers=headers)
    return await client.post(url, headers=headers, json=payload)


async def probe() -> tuple[bool, str]:
    """Check the console answers and the credential works. No side effects.

    Used by the Backup page's test button, so it says what is wrong in words the operator
    can act on rather than returning a status code.
    """
    config = await load_config()
    if not config.base_url:
        return False, "No UniFi console address is configured."
    if not config.configured:
        return False, "No UniFi API key or username and password has been saved."
    try:
        async with _build_client(config) as client:
            headers = await _authenticate(client, config)
            response = await _network_api(client, config, headers, "/self")
    except UnifiError as exc:
        return False, str(exc)
    except httpx.RequestError as exc:
        return False, f"Could not reach the UniFi console: {_redact(str(exc), config)}"
    if response.status_code == 401:
        return False, "The UniFi console rejected the credential."
    if response.status_code >= 400:
        return False, f"The UniFi console answered {response.status_code}."
    return True, f"Reached the UniFi console and signed in to site {config.site!r}."


async def kick_client(mac: str) -> tuple[bool, str]:
    """Ask the console to disassociate *mac*, so it re-associates on the stronger radio.

    Returns ``(moved_on, why)``. Never raises: every caller is on the path to a backup, and
    a console that is unreachable, unconfigured or simply says no must cost nothing more
    than a slow transfer.
    """
    normalised = mac.strip().lower()
    if not _MAC.match(normalised):
        return False, f"{mac!r} is not a MAC address."
    config = await load_config()
    if not config.configured:
        return False, "UniFi is not configured."
    try:
        async with _build_client(config) as client:
            headers = await _authenticate(client, config)
            response = await _network_api(
                client,
                config,
                headers,
                "/cmd/stamgr",
                payload={"cmd": "kick-sta", "mac": normalised},
            )
    except UnifiError as exc:
        return False, str(exc)
    except httpx.RequestError as exc:
        return False, f"Could not reach the UniFi console: {_redact(str(exc), config)}"
    if response.status_code >= 400:
        return False, f"The UniFi console answered {response.status_code}."
    return True, f"Asked the access point to reconnect {normalised}."
