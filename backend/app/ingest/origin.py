"""The address this app is reached on, learned rather than configured.

Needed so the head unit can be pointed at the Backup page while a transfer runs, and the
app cannot work it out for itself. It runs in a bridged container: the addresses it can see
on its own interfaces are the container's (172.17.x.x), while the address that actually
reaches it -- the host's LAN address and the *published* port -- exists only outside the
container and is never communicated inward. Asking the operating system produces a
confident, useless answer.

The browser, on the other hand, has already solved it. Whatever address the dashboard was
opened on is by definition an address on this network that resolves to this app, which is
exactly what the car needs. So it is taken from there.

Learned only from a browser fetching the dashboard itself, never from an API call. Home
Assistant polls the same application under whatever name *it* was configured with -- very
often a container name or a Docker-internal host that nothing in a car could resolve -- and
inheriting that would send the head unit somewhere it cannot reach, silently, while looking
entirely correct in the settings.

Held in memory rather than persisted, because the alternative is worse. A stored address
goes stale when the host moves, and a stale one points the car at somebody else's machine.
This one is re-learned every time anybody opens the dashboard, and the cost of not knowing
it -- after a restart, before the first page load -- is that one window's transfer runs
without putting a page on the car's screen. The manual override exists for anyone who needs
certainty instead.
"""

from __future__ import annotations

from app.core.logging import get_logger

log = get_logger(__name__)

#: Hostnames that are true for this app and useless to the car.
#:
#: A dashboard opened at http://localhost:8199 tells us nothing transferable: the head unit
#: resolving "localhost" would reach *itself*. Better to know the address is unusable than
#: to send the car somewhere confidently wrong.
_UNREACHABLE = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

_origin: str | None = None


def _hostname(host: str) -> str:
    """The host part of a ``Host`` header, with any port and IPv6 brackets removed."""
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.split(":", 1)[0] if host.count(":") == 1 else host


def remember(scheme: str, host: str) -> None:
    """Record the address a browser has just reached the dashboard on."""
    global _origin
    host = (host or "").strip()
    if not host or _hostname(host).lower() in _UNREACHABLE:
        return

    candidate = f"{scheme or 'http'}://{host}"
    if candidate != _origin:
        _origin = candidate
        log.info("learned the address this app is reached on", origin=candidate)


def backup_url() -> str:
    """Where to send the head unit's browser, or "" if nobody has opened the app yet."""
    return f"{_origin}/backup" if _origin else ""


def reset_for_tests() -> None:
    global _origin
    _origin = None
