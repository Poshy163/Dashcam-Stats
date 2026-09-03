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

Held in memory *and* written down, which is a correction of an earlier judgement rather
than belt and braces. This was memory-only at first, on the reasoning that a stored address
goes stale when the host moves while a re-learned one cannot, and that the cost of not
knowing it -- after a restart, before the first page load -- was one window's transfer
running without a page on the car's screen.

That cost was measured wrong. The dashboard is opened when somebody wants to look at
footage, and the car arrives on the driveway when somebody comes home; there is no reason
for the first to have happened since the last restart, and in practice it usually has not.
The deployment this was written for restarted at 13:55Z, ran two transfers of sixty-two
files each at 05:01Z and 05:42Z the following morning with nothing on the screen either
time, and only learned its own address at 05:44Z -- ninety-three seconds after the second
one had started moving. "One window" was every window.

So the last learned address is persisted, and memory stays in front of it. Staleness is
handled by the thing that made memory attractive in the first place: any dashboard load
overwrites both, immediately, and the stored value is only ever consulted before the first
load of a fresh process. It also sits below ``ingest.unit_display_url``, which remains the
answer for anyone who wants certainty rather than inference.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.auth.service import API_KEY_PARAM, api_key_enabled, configured_api_key
from app.core.logging import get_logger
from app.core.settings_service import get_settings_service

log = get_logger(__name__)

#: Where the learned address is kept across restarts. Read-only in the UI: it is reported
#: so the operator can see where the car will be sent, not offered as somewhere to type.
LEARNED_KEY = "ingest.learned_origin"

#: Hostnames that are true for this app and useless to the car.
#:
#: A dashboard opened at http://localhost:8199 tells us nothing transferable: the head unit
#: resolving "localhost" would reach *itself*. Better to know the address is unusable than
#: to send the car somewhere confidently wrong.
_UNREACHABLE = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

_origin: str | None = None

#: A fingerprint of the built SPA, set once at startup by :mod:`app.main`.
#:
#: Injected rather than read from here, because the build lives behind ``FRONTEND_DIST`` in
#: ``app.main`` and this module is imported by it.
_build_tag: str = ""

#: Query parameter carrying that fingerprint on the head unit's URL.
BUILD_PARAM = "v"

#: Marks the one URL the head unit is *sent* (:func:`backup_url`) as the constrained kiosk
#: view. The dashcam's in-dash browser runs on a weak Mali GPU that recomposites the whole
#: 1080p surface every frame for as long as any CSS animation is running -- and the shared
#: layout pulses a "queue busy" dot forever, because the server is essentially always
#: processing. Measured on the live unit that kept Chrome's GPU process at ~63% of a core
#: while merely displaying the page, stealing CPU from the recorder and CarPlay. Desktop
#: clients choose their own URL and never carry this flag, so they keep the full-motion UI;
#: the unit gets a static one that lets the browser go idle between updates.
KIOSK_PARAM = "kiosk"


def set_build_tag(tag: str) -> None:
    global _build_tag
    _build_tag = (tag or "").strip()


def _hostname(host: str) -> str:
    """The host part of a ``Host`` header, with any port and IPv6 brackets removed."""
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.split(":", 1)[0] if host.count(":") == 1 else host


async def remember(scheme: str, host: str) -> None:
    """Record the address a browser has just reached the dashboard on."""
    global _origin
    host = (host or "").strip()
    if not host or _hostname(host).lower() in _UNREACHABLE:
        return

    candidate = f"{scheme or 'http'}://{host}"
    if candidate == _origin:
        return
    _origin = candidate
    log.info("learned the address this app is reached on", origin=candidate)

    # Only on a change, which is once per process for a deployment reached at one address
    # -- not a write per page load. Failing to store it is not worth failing a page render
    # over: memory has the value, and the only thing lost is the next restart's first
    # window.
    try:
        await get_settings_service().set(LEARNED_KEY, candidate, internal=True)
    except Exception as exc:  # pragma: no cover - a settings write that fails is logged only
        log.warning("could not store the address for the next restart", error=str(exc))


def _stored() -> str:
    try:
        return str(get_settings_service().get_nowait(LEARNED_KEY) or "").strip()
    except Exception:
        return ""


def callback_endpoint() -> tuple[str, int] | None:
    """Host and port the head unit should post back to, or ``None`` if unknown.

    The same learned address the car's browser is sent to, minus the browser. A watchdog
    reporting in from the unit has exactly the problem this module exists for -- it cannot
    ask the app where the app is -- and it has less to work with than the browser does,
    since it speaks a hand-rolled HTTP request over ``nc`` rather than following a URL. So
    it is handed a host and a port and nothing else.

    ``ingest.unit_display_url`` wins where it is set, for the same reason it wins for the
    display: an operator who had to override the learned address had a reason, and that
    reason applies to every route from the car back to here, not just the one carrying
    pixels.
    """
    override = str(_get_setting("ingest.unit_display_url")).strip()
    base = override or _origin or _stored()
    if not base:
        return None
    parts = urlsplit(base if "//" in base else f"http://{base}")
    host = parts.hostname or ""
    if not host or host.lower() in _UNREACHABLE:
        return None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    # The watchdog's request is plaintext over the LAN. An HTTPS deployment is reached
    # through a proxy this app cannot speak for, so rather than post a token in the clear
    # to a port that will not answer, it reports nothing and the old next-arrival repair
    # remains in charge.
    if parts.scheme == "https":
        return None
    return host, port


def _get_setting(key: str) -> str:
    try:
        return str(get_settings_service().get_nowait(key) or "")
    except Exception:
        return ""


def with_api_key(url: str) -> str:
    """Attach the API key to a URL bound for the head unit, if there is one to attach.

    Applied to the operator's own ``ingest.unit_display_url`` as well as to the learned
    address, because the alternative is a trap: that setting's example is a bare
    ``http://host:port/backup``, anyone filling it in copies the example, and with sign-in
    on the car then gets a login form it has no keyboard to answer. The override exists to
    change *where* the car is sent, not to opt out of being able to get in.
    """
    if not url or not api_key_enabled():
        return url
    query = urlsplit(url).query
    # An operator who pasted a full URL complete with `?k=` keeps theirs rather than
    # collecting a second one.
    if any(key == API_KEY_PARAM for key, _ in parse_qsl(query, keep_blank_values=True)):
        return url
    return f"{url}{'&' if query else '?'}{urlencode({API_KEY_PARAM: configured_api_key()})}"


def backup_url() -> str:
    """Where to send the head unit's browser, or "" if this app has never been opened.

    Carries the API key when one is configured, because the head unit has no other way to
    present it: it is handed this URL by ``am start`` and has nobody to fill in a login
    form. The key is redeemed for a cookie on arrival and taken back out of the address bar
    -- see ``_redeem_api_key`` in :mod:`app.main`.

    Carries the build fingerprint for a blunter reason. ``Cache-Control: no-cache`` on the
    shell governs copies fetched *after* it started being sent, and does nothing about one
    a browser already holds -- so an upgrade leaves every existing client entitled to serve
    its stale ``index.html`` until that entry expires on its own. That was observed on the
    live unit: Chrome kept an old shell, asked for ``Backup-E4HX69ll.js`` and ``ui-*.js``,
    got two 404s, and rendered a white screen with no way to press refresh in a parked car.

    A new build means a new URL, which is a different cache entry, which cannot be stale.
    The head unit is the one client that is *sent* its address rather than choosing it, so
    this is available here and nowhere else -- and it is the difference between the screen
    recovering by itself after a deploy and needing somebody to go and touch it.
    """
    base = _origin or _stored()
    if not base:
        return ""
    url = f"{base}/backup"
    if _build_tag:
        url = f"{url}?{urlencode({BUILD_PARAM: _build_tag})}"
    # The head unit is the only client sent this URL, so it is the only one flagged as the
    # kiosk. The key is appended last, after the kiosk marker, so it stays easy to redact.
    return with_api_key(as_kiosk(url))


def as_kiosk(url: str) -> str:
    """Mark *url* as the head unit's low-power kiosk view, idempotently.

    See :data:`KIOSK_PARAM`. Applied to both the learned :func:`backup_url` and the
    operator's ``ingest.unit_display_url`` override, so anything the unit is *sent* carries
    it and nothing a desktop chooses for itself does.
    """
    if not url:
        return url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == KIOSK_PARAM for key, _ in pairs):
        return url
    pairs.append((KIOSK_PARAM, "1"))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def redacted(url: str) -> str:
    """The same URL with the API key masked, for anything a human will look at.

    The key is the account's equal, and the places this URL gets shown -- a log line on the
    Logs page, the Test button's own result, the screenshot of either that ends up in a
    bug report -- are all places it should not be legible.
    """
    if not url:
        return url
    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == API_KEY_PARAM for key, _ in pairs):
        return url
    masked = [(k, "<key>" if k == API_KEY_PARAM else v) for k, v in pairs]
    return urlunsplit(parts._replace(query=urlencode(masked, safe="<>")))


def reset_for_tests() -> None:
    """Forget both pieces of process-global state this module holds.

    The build tag is included because importing ``app.main`` sets it as a side effect --
    ``create_app()`` runs at module scope -- so without this every test in the suite would
    inherit the fingerprint of whatever happened to be built in the tree.
    """
    global _origin, _build_tag
    _origin = None
    _build_tag = ""
