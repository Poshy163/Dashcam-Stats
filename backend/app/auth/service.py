"""The sign-in account and the browser sessions issued against it.

Three things make this more than a table lookup.

**It is on the hot path.** The gate runs for every request, and a recordings grid fires
fifty thumbnail requests at once while a video seek fires a range request per drag. A
database round trip per request would put the auth check in front of the media pipeline for
no benefit, so verified tokens are cached in the process for a minute, misses are
single-flighted so a burst of fifty costs one query rather than fifty, and unknown tokens
are remembered as unknown. Revocation still takes effect immediately: cache entries carry
an epoch, and anything that invalidates a session bumps it, which retires the whole cache
in one assignment. Correctness never depends on a TTL expiring.

**Verifying a password is expensive on purpose, which makes it a weapon.** A scrypt
derivation is 32 MiB and something over a tenth of a second, and the ``Authorization:
Basic`` path can be reached by anyone who knows the hostname. Unthrottled, that is a free
way to pin every thread in the process and starve the analysis pipeline behind guessed
passwords. Every derivation therefore goes through :data:`_kdf_slots`, and the Basic path
is rate-limited and negative-cached exactly like the login form.

**Being locked out is the worst outcome here.** This is a self-hosted library of your own
footage; a sign-in that cannot be undone is worse than no sign-in at all. The credential and
the ``security.require_login`` switch are always changed together, :func:`sign_in_required`
treats "required, but no account exists" as off while complaining loudly, and while it is in
that state the account row is re-read every half minute -- which is what lets the recovery
command reopen a locked-out deployment without a restart.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import secrets
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ratelimit
from app.auth.passwords import dummy_verify, hash_password, needs_rehash, verify_password
from app.core.logging import get_logger
from app.core.settings_service import get_settings_service
from app.db.models import AuthCredential, AuthSession
from app.db.session import session_scope

log = get_logger(__name__)

#: The only row id ``auth_credentials`` ever holds. See the model's docstring.
CREDENTIAL_ID = 1

#: Name of the cookie carrying the session token over plain HTTP.
COOKIE_NAME = "dashcam_session"

#: And over HTTPS. The ``__Host-`` prefix is not decoration: this deployment lives at
#: ``dashcam.joshualeaper.dev``, and every other host under ``joshualeaper.dev`` is
#: same-site to it. Without the prefix, any one of them could set a ``dashcam_session``
#: cookie scoped to the apex domain, the browser would send both, and which one this
#: process read would be an accident of ordering. The prefix makes the browser refuse the
#: cookie unless it is ``Secure``, ``Path=/`` and carries no ``Domain`` -- which is
#: precisely the cookie no sibling host is able to write.
SECURE_COOKIE_NAME = "__Host-dashcam_session"

#: Name of the cookie a caller is given once its API key has been accepted.
#:
#: Exists so the head unit's browser can be handed the key exactly once, in the URL it is
#: opened on, and then keep it out of the address bar. The alternative -- leaving ``?k=``
#: on every page of the SPA -- puts the key in the browser history of a screen that sits
#: unlocked in a car, and in the ``Referer`` of anything the page loads.
API_KEY_COOKIE_NAME = "dashcam_key"

#: And over HTTPS, for the reason given on :data:`SECURE_COOKIE_NAME`.
SECURE_API_KEY_COOKIE_NAME = "__Host-dashcam_key"

#: Where an API key may be presented, in the order the gate looks.
#:
#: The query parameter is what makes this feature possible at all: the head unit is handed
#: a URL and nothing else -- no header can be attached to a browser's first navigation --
#: so the key has to survive being written into a link. It is short because it is typed
#: into ``am start`` and read off a car's screen.
API_KEY_PARAM = "k"
API_KEY_HEADER = "X-API-Key"

#: Shortest key accepted. A key is a bearer credential with no username, no throttle worth
#: the name and no second factor, so it is not allowed to be short enough to guess. Keys
#: from :func:`generate_api_key` are far longer; this only refuses a hand-typed one.
MIN_API_KEY_LENGTH = 24

#: How long a verified token is trusted without going back to the database. Bounded by the
#: epoch below rather than by this number, which only decides how stale ``last_used_at``
#: and an expiry that passed mid-window are allowed to get.
CACHE_TTL_S = 60.0

#: How long an unrecognised token is remembered as unrecognised. Without this, spraying
#: random cookie values at ``/media`` costs a connection checkout and a query each time.
NEGATIVE_TTL_S = 30.0

#: ``last_used_at`` is refreshed no more often than this, per session. It exists so the
#: operator can recognise a stale row on the sessions list, and a write per request to
#: maintain a column nobody reads to the second would be the most expensive thing on the
#: hot path -- SQLite has one writer, and the analysis workers already want it.
LAST_USED_REFRESH_S = 300.0

#: How often the account row is re-read while sign-in is switched on but no account is
#: cached. This is the window in which ``recover-login`` takes effect on a running process.
MISCONFIGURED_RECHECK_S = 30.0

#: Ceiling on cached tokens, positive and negative alike.
MAX_CACHED_SESSIONS = 512

#: A session issued without "stay signed in". Short enough that an unattended browser on a
#: shared machine stops being a way in by the next day; the cookie itself is discarded when
#: the browser closes regardless.
DEFAULT_SESSION_HOURS = 12

#: Shortest password accepted. Longer than the eight characters that would be defensible on
#: a LAN, because this one guards a public hostname with a single account and no second
#: factor, and because the database it lives in can be downloaded in one click from
#: Settings > Advanced -- so the realistic attack is offline, against the hash, at whatever
#: rate the attacker's hardware allows rather than at whatever rate this process allows.
MIN_PASSWORD_LENGTH = 12

#: Concurrent scrypt derivations. Each is 32 MiB and roughly an eighth of a second, and
#: ``asyncio.to_thread`` hands them to the loop's default executor -- the same pool the
#: detection stage, the ffmpeg probes, the ingest transfers and the playback remuxer all
#: share. Without this ceiling, a flood of wrong passwords from an unauthenticated caller
#: takes that pool away from the pipeline. Two is enough for any real household and far too
#: few to be worth attacking.
_kdf_slots = asyncio.Semaphore(2)


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making the request, and how they proved it."""

    username: str
    #: ``"session"`` for a cookie, ``"basic"`` for an API client's Authorization header,
    #: ``"apikey"`` for the standing key the head unit is handed in its URL.
    method: str
    session_id: int | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """A row from the sessions list, for the Security panel."""

    id: int
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime
    remembered: bool
    user_agent: str | None
    created_ip: str | None
    current: bool


# --------------------------------------------------------------------------------------
# Process-wide state
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _CachedSession:
    epoch: int
    checked_at: float
    expires_at: datetime
    session_id: int
    username: str


#: Bumped by anything that invalidates a *session*: a revocation, a sign-out, a password
#: change, clearing the account.
_session_epoch = 0
#: Bumped only by something that changes the *password*. Kept separate so that one browser
#: signing out does not make the next Home Assistant poll pay for a fresh derivation.
_credential_epoch = 0

_session_cache: dict[str, _CachedSession] = {}
_session_missing: dict[str, tuple[int, float]] = {}
#: In-flight database lookups, keyed by token hash, so fifty simultaneous thumbnail
#: requests behind one cold cookie do one query between them instead of fifty.
_inflight: dict[str, asyncio.Future] = {}
#: When each session's ``last_used_at`` was last written, so the refresh is throttled per
#: session rather than per cache miss.
_last_used_written: dict[int, float] = {}

#: Verified and rejected ``Authorization: Basic`` headers, keyed by an HMAC of the header
#: under a key that exists only for the life of this process. A plain digest here would be
#: an unsalted fast hash of the user's actual password sitting in memory, which is the
#: exact thing scrypt is in this codebase to prevent.
_basic_cache: dict[str, tuple[int, float, str]] = {}
_basic_rejected: dict[str, tuple[int, float]] = {}
_PROCESS_KEY = secrets.token_bytes(32)

#: The account's username, cached so the gate can answer "is there an account?" without
#: touching the database. ``_credential_loaded`` distinguishes "no account" from "not read
#: yet", which matters because the two must not be treated the same.
_credential_username: str | None = None
_credential_loaded = False
_credential_lock = asyncio.Lock()
_credential_rechecked_at = 0.0

#: Rate-limits the complaint in :func:`sign_in_required` so a misconfigured deployment does
#: not write one log row per request into the table the Logs page reads.
_last_misconfig_warning = 0.0


def _bump_sessions() -> None:
    global _session_epoch
    _session_epoch += 1
    _session_cache.clear()
    _session_missing.clear()
    _last_used_written.clear()
    # Single-flight entries are retired with everything else. They hold futures created on
    # whichever event loop was running when the lookup started, so an entry that outlives
    # its loop -- between tests, or after a restart-in-place -- is one that can never be
    # awaited again. Cancel rather than drop, so anything already waiting is released
    # instead of hanging on a future nobody will complete.
    for pending in _inflight.values():
        if not pending.done():
            pending.cancel()
    _inflight.clear()


def _bump_credential() -> None:
    global _credential_epoch
    _credential_epoch += 1
    _basic_cache.clear()
    _basic_rejected.clear()
    _bump_sessions()


def reset_auth_state() -> None:
    """Drop every cached answer. Called on startup and between tests."""
    global _credential_username, _credential_loaded, _credential_rechecked_at
    _credential_username = None
    _credential_loaded = False
    _credential_rechecked_at = 0.0
    _bump_credential()


# --------------------------------------------------------------------------------------
# Is sign-in on?
# --------------------------------------------------------------------------------------


def credential_configured() -> bool:
    """Whether an account exists, from the process cache. Never touches the database."""
    return _credential_username is not None


def configured_username() -> str | None:
    return _credential_username


def require_login_setting() -> bool:
    try:
        return bool(get_settings_service().get_nowait("security.require_login"))
    except Exception:
        # Reached only before the settings service is initialised, which means the app is
        # still starting and is not serving requests yet.
        return False


def misconfigured() -> bool:
    """Sign-in is switched on and there is nothing to sign in against.

    Surfaced through ``/api/auth/state`` so the Security panel can say so. Failing open is
    the right behaviour; doing it silently on a public hostname is not, because the page
    would otherwise report a protected deployment while serving an unprotected one.
    """
    return require_login_setting() and not credential_configured()


async def sign_in_required() -> bool:
    """Whether the gate should challenge this request.

    The switch alone is not enough: turning it on without an account would lock every route,
    including the page that would let someone fix it. The settings service refuses to reach
    that state and :func:`clear_credential` unwinds it, so this is the backstop for a
    hand-edited database or a restored backup -- and it fails open, because a self-hosted
    footage library that nobody can open is a worse outcome than one that is briefly
    unguarded while the operator reads the log line below.
    """
    global _last_misconfig_warning
    if not require_login_setting():
        return False
    await ensure_credential_loaded()

    # Re-read the account row every half minute while sign-in is on. One indexed primary-key
    # get per thirty seconds is nothing next to what it buys: `recover-login` deletes the
    # row from outside this process, and without this the process would go on challenging
    # against an account that no longer exists -- nobody able to sign in, and the rescue
    # tool the cause of it. Checked before `credential_configured()` rather than after,
    # because the state recovery leaves behind is a *stale yes*, not a no.
    await _recheck_credential()
    if credential_configured():
        return True

    now = time.monotonic()
    if now - _last_misconfig_warning > 60.0:
        _last_misconfig_warning = now
        log.error(
            "sign-in is switched on but no account is configured; serving without "
            "authentication. Set a username and password in Settings > Access, or run "
            "the recovery command, to close this."
        )
    return False


async def ensure_credential_loaded() -> None:
    """Read the account into the process cache, once.

    Faulted in on demand rather than only at start-up. Making the gate depend on a lifespan
    hook having run would mean any path that builds the app without one -- a test, an
    embedding, a future worker process -- silently serves unauthenticated, and that is the
    one failure mode this module must not have.
    """
    global _credential_username, _credential_loaded, _credential_rechecked_at
    if _credential_loaded:
        return
    async with _credential_lock:
        if _credential_loaded:
            return
        async with session_scope() as session:
            row = await session.get(AuthCredential, CREDENTIAL_ID)
            _credential_username = row.username if row is not None else None
        _credential_loaded = True
        _credential_rechecked_at = time.monotonic()
    _bump_credential()


async def _recheck_credential() -> None:
    global _credential_username, _credential_rechecked_at
    if time.monotonic() - _credential_rechecked_at < MISCONFIGURED_RECHECK_S:
        return
    async with _credential_lock:
        if time.monotonic() - _credential_rechecked_at < MISCONFIGURED_RECHECK_S:
            return
        _credential_rechecked_at = time.monotonic()
        async with session_scope() as session:
            row = await session.get(AuthCredential, CREDENTIAL_ID)
            found = row.username if row is not None else None
    if found != _credential_username:
        _credential_username = found
        _bump_credential()
        log.info("account reloaded from the database", configured=found is not None)


# --------------------------------------------------------------------------------------
# The account
# --------------------------------------------------------------------------------------


def normalise_username(raw: str) -> str:
    """Fold a username to one canonical spelling.

    NFKC because an accented name has more than one Unicode encoding that renders
    identically. Left unnormalised they are two different accounts, and the operator could
    easily end up locked out by the one they cannot reproduce on the keyboard in front of
    them.
    """
    return unicodedata.normalize("NFKC", raw).strip()


async def set_credential(session: AsyncSession, username: str, password: str) -> None:
    """Create or replace the account. Every existing browser session is signed out.

    Revoking on a password change is the whole point of changing it: the usual reason is
    that the old one is believed to be known, and leaving thirty-day cookies alive would
    mean the change accomplished nothing for a month.
    """
    global _credential_username, _credential_loaded
    name = normalise_username(username)
    if not name:
        raise ValueError("A username is required.")
    if len(name) > 128:
        raise ValueError("That username is too long.")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in name):
        raise ValueError("A username cannot contain spaces or control characters.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > 1024:
        # Not a policy about good passwords -- a bound on what is fed to a memory-hard KDF.
        raise ValueError("That password is too long.")

    encoded = await _derive(hash_password, password)
    row = await session.get(AuthCredential, CREDENTIAL_ID)
    if row is None:
        session.add(AuthCredential(id=CREDENTIAL_ID, username=name, password_hash=encoded))
    else:
        row.username = name
        row.password_hash = encoded
    await session.execute(delete(AuthSession))
    await session.commit()

    _credential_username = name
    _credential_loaded = True
    _bump_credential()
    log.info("sign-in account set", username=name)


async def clear_credential(session: AsyncSession) -> None:
    """Delete the account, its sessions, and the switch that depends on it.

    The switch goes with it deliberately. Leaving ``security.require_login`` on with
    nothing to sign in against is the one state this design refuses to be in. The ordering
    is safe either way round: if the settings write fails, the credential is already gone,
    and :func:`sign_in_required` fails open on exactly that.
    """
    global _credential_username, _credential_loaded
    await session.execute(delete(AuthSession))
    await session.execute(delete(AuthCredential))
    await session.commit()

    _credential_username = None
    _credential_loaded = True
    _bump_credential()
    await get_settings_service().set("security.require_login", False)
    log.info("sign-in account cleared; sign-in switched off")


async def verify_credential(username: str, password: str) -> bool:
    """Check a username and password against the stored account.

    Answers in the same time whether the account is missing, the username is wrong or the
    password is wrong. A login endpoint that is quick to reject an unknown username hands
    out a list of valid ones.
    """
    async with session_scope() as session:
        row = await session.get(AuthCredential, CREDENTIAL_ID)
        if row is None:
            await _derive(dummy_verify)
            return False
        encoded = row.password_hash
        stored_username = row.username

    ok = await _derive(verify_password, password, encoded)
    # Compared after the derivation, not before, so a wrong username costs exactly what a
    # wrong password costs. Encoded to bytes first because `compare_digest` raises
    # TypeError on `str` arguments outside ASCII -- one accented character in a username
    # would otherwise have turned every sign-in attempt into a 500, which is a lockout with
    # extra steps.
    if not ok or not hmac.compare_digest(
        normalise_username(username).encode("utf-8"), stored_username.encode("utf-8")
    ):
        return False

    if needs_rehash(encoded):
        # The parameters in `passwords` have moved on since this was stored, and the
        # correct password has just been offered -- which is the only moment a stronger
        # hash can be made without asking the user for anything.
        upgraded = await _derive(hash_password, password)
        async with session_scope() as session:
            await session.execute(
                update(AuthCredential)
                .where(AuthCredential.id == CREDENTIAL_ID)
                .values(password_hash=upgraded)
            )
        log.info("password hash upgraded to current parameters")
    return True


async def _derive(fn, *args):
    """Run a KDF call off the event loop, with the concurrency ceiling applied."""
    async with _kdf_slots:
        return await asyncio.to_thread(fn, *args)


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_from_cookies(cookies: dict[str, str]) -> str:
    """The session token a browser sent, preferring the host-locked cookie."""
    return cookies.get(SECURE_COOKIE_NAME) or cookies.get(COOKIE_NAME) or ""


async def create_session(
    *,
    remembered: bool,
    user_agent: str | None,
    client_ip: str | None,
) -> tuple[str, datetime]:
    """Issue a session. Returns the raw token -- the only time it exists -- and its expiry."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    if remembered:
        lifetime = timedelta(days=int(get_settings_service().get_nowait("security.remember_days")))
    else:
        lifetime = timedelta(hours=DEFAULT_SESSION_HOURS)
    expires_at = now + lifetime

    async with session_scope() as session:
        # Cheap to do here and nowhere else: a login is rare, and it is the one moment the
        # table is guaranteed to be open for writing anyway.
        await session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        session.add(
            AuthSession(
                token_hash=_hash_token(token),
                created_at=now,
                expires_at=expires_at,
                last_used_at=now,
                remembered=remembered,
                user_agent=(user_agent or "")[:256] or None,
                created_ip=(client_ip or "")[:64] or None,
            )
        )
    return token, expires_at


async def resolve_session(token: str) -> Principal | None:
    """Turn a cookie value into a principal, or None if it is not a live session."""
    if not token:
        return None
    await ensure_credential_loaded()
    username = _credential_username
    if username is None:
        return None

    token_hash = _hash_token(token)
    now_wall = datetime.now(UTC)
    now = time.monotonic()

    cached = _session_cache.get(token_hash)
    if cached is not None:
        if cached.epoch != _session_epoch:
            _session_cache.pop(token_hash, None)
        elif cached.expires_at <= now_wall:
            # Caught locally so an expired cookie does not go on costing a query per
            # request until the entry ages out.
            _session_cache.pop(token_hash, None)
            return None
        elif now - cached.checked_at < CACHE_TTL_S:
            return Principal(username=username, method="session", session_id=cached.session_id)

    missing = _session_missing.get(token_hash)
    if missing is not None:
        epoch, seen_at = missing
        if epoch == _session_epoch and now - seen_at < NEGATIVE_TTL_S:
            return None
        _session_missing.pop(token_hash, None)

    inflight = _inflight.get(token_hash)
    if inflight is not None:
        # Somebody is already asking. Fifty thumbnail requests arriving together behind one
        # cold cookie is the normal case, not the exceptional one, and fifty simultaneous
        # checkouts against a pool of eight is how that becomes a stall.
        return await asyncio.shield(inflight)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[token_hash] = future
    try:
        principal = await _load_session(token_hash, username, now_wall, now)
    except Exception as exc:
        future.set_exception(exc)
        # Retrieved here so a future nobody happened to await does not have the loop
        # complain about an exception that was in fact handled.
        future.exception()
        raise
    else:
        future.set_result(principal)
        return principal
    finally:
        # In a `finally`, and this is the whole point of it: `except Exception` does not
        # catch `CancelledError`, so the two lines above used to leave the entry behind
        # whenever the waiter was cancelled -- and a cancelled session lookup is not
        # exotic here. The docstring above describes fifty thumbnail requests arriving
        # together behind one cold cookie; navigating away part-way through cancels them.
        #
        # What was left behind is worse than a leak. Every later request presenting that
        # cookie finds the orphan and awaits it, and it is a future nobody will ever
        # complete -- so the session stops resolving until the process restarts. In the
        # test suite, where each test gets its own event loop, the orphan is a future
        # belonging to a *closed* loop, and awaiting it is exactly the "Event loop is
        # closed" that has been failing CI since this cache was introduced.
        _inflight.pop(token_hash, None)
        if not future.done():
            future.cancel()


async def _load_session(
    token_hash: str, username: str, now_wall: datetime, now: float
) -> Principal | None:
    async with session_scope() as session:
        row = (
            await session.execute(select(AuthSession).where(AuthSession.token_hash == token_hash))
        ).scalar_one_or_none()
        if row is None or row.expires_at <= now_wall:
            _session_cache.pop(token_hash, None)
            _remember(_session_missing, token_hash, (_session_epoch, now))
            return None
        session_id = row.id
        expires_at = row.expires_at
        stale = (now_wall - row.last_used_at).total_seconds() > LAST_USED_REFRESH_S

    _remember(
        _session_cache,
        token_hash,
        _CachedSession(
            epoch=_session_epoch,
            checked_at=now,
            expires_at=expires_at,
            session_id=session_id,
            username=username,
        ),
    )

    if stale and now - _last_used_written.get(session_id, 0.0) > LAST_USED_REFRESH_S:
        _last_used_written[session_id] = now
        # Its own transaction, after the read has closed. Upgrading the read scope to a
        # write would put the auth check behind SQLite's single writer, which the analysis
        # workers hold for seconds at a time.
        async with session_scope() as write:
            await write.execute(
                update(AuthSession)
                .where(AuthSession.id == session_id)
                .values(last_used_at=now_wall)
            )

    return Principal(username=username, method="session", session_id=session_id)


def _remember(store: dict, key: str, value: object) -> None:
    if len(store) >= MAX_CACHED_SESSIONS:
        store.clear()
    store[key] = value


async def revoke_session(token: str) -> None:
    """Sign out one browser, by its cookie."""
    if not token:
        return
    async with session_scope() as session:
        await session.execute(
            delete(AuthSession).where(AuthSession.token_hash == _hash_token(token))
        )
    _bump_sessions()


async def revoke_session_id(session_id: int) -> bool:
    async with session_scope() as session:
        result = await session.execute(delete(AuthSession).where(AuthSession.id == session_id))
        removed = bool(result.rowcount)
    _bump_sessions()
    return removed


async def revoke_all_sessions(*, keep_session_id: int | None = None) -> int:
    """Sign out every browser, optionally sparing the one asking.

    A caller holding Basic credentials rather than a cookie has no session to spare, so for
    them this really does mean every browser -- including one the operator may be reading
    the page in. That is the correct reading of the request from a client which is, by
    construction, not a browser.
    """
    stmt = delete(AuthSession)
    if keep_session_id is not None:
        stmt = stmt.where(AuthSession.id != keep_session_id)
    async with session_scope() as session:
        removed = int((await session.execute(stmt)).rowcount or 0)
    _bump_sessions()
    return removed


async def list_sessions(*, current_session_id: int | None) -> list[SessionInfo]:
    now = datetime.now(UTC)
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(AuthSession)
                    .where(AuthSession.expires_at > now)
                    .order_by(AuthSession.last_used_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        SessionInfo(
            id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
            remembered=row.remembered,
            user_agent=row.user_agent,
            created_ip=row.created_ip,
            current=row.id == current_session_id,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------------------
# The API key, for callers that cannot be asked to type anything
# --------------------------------------------------------------------------------------
#
# There is exactly one caller this exists for: the dashcam's own head unit. When a transfer
# starts the app opens its Backup page on the car's screen, and there is nobody in the
# driver's seat to fill in a login form -- the unit is handed a URL by ``am start`` and that
# is the whole of its opportunity to authenticate.
#
# It is a bearer credential and it is *full access*: presenting it is presenting the
# account. That is a deliberate choice by the operator rather than an oversight, and it is
# why it is off until a key is set, why it is one setting to blank out, and why the key is
# taken out of the URL on arrival rather than left in the history of a screen that lives in
# a car. Anything that can read the key can read the footage.


def generate_api_key() -> str:
    """A fresh key. 32 bytes of urandom, URL-safe so it survives being put in a link."""
    return secrets.token_urlsafe(32)


def configured_api_key() -> str:
    """The operator's key, or "" when the feature is switched off."""
    try:
        return str(get_settings_service().get_nowait("security.api_key") or "").strip()
    except Exception:
        # Only before the settings service is up, which means no request has been served.
        return ""


def api_key_enabled() -> bool:
    return len(configured_api_key()) >= MIN_API_KEY_LENGTH


async def resolve_api_key(presented: str) -> Principal | None:
    """Turn a presented key into the account's principal, or None.

    No cache and no rate limit, unlike the Basic path, because there is nothing expensive
    here to protect: this is a comparison of two strings, not a scrypt derivation. The
    comparison is constant-time all the same -- a bearer credential checked with ``==``
    leaks its prefix to anyone patient enough to measure, and unlike a password there is no
    KDF in front of it to hide behind.
    """
    if not presented:
        return None
    expected = configured_api_key()
    if len(expected) < MIN_API_KEY_LENGTH:
        return None
    if not hmac.compare_digest(presented, expected):
        return None
    await ensure_credential_loaded()
    username = _credential_username
    if username is None:
        return None
    return Principal(username=username, method="apikey")


# --------------------------------------------------------------------------------------
# HTTP Basic, for API clients
# --------------------------------------------------------------------------------------


async def resolve_basic(header: str, *, address: str) -> Principal | None:
    """Verify an ``Authorization: Basic`` header against the account.

    Home Assistant polls ``/api/ingest/status`` as a REST sensor and people drive this API
    from scripts; neither can hold a cookie. Verifying is a full scrypt derivation, which
    would be absurd on a five-second poll, so a header that checks out is remembered for
    the cache window -- and so is one that does not, because a Home Assistant instance
    configured with the wrong password retries forever and would otherwise buy an
    unauthenticated caller a derivation every time.

    Throttled on the same buckets as the login form. This path is reachable by anyone who
    knows the hostname, and without a limiter it is a password oracle that also happens to
    starve the thread pool the analysis pipeline runs on.
    """
    if not header.startswith("Basic "):
        return None
    await ensure_credential_loaded()
    if _credential_username is None:
        return None

    key = hmac.new(_PROCESS_KEY, header.encode("utf-8"), hashlib.sha256).hexdigest()
    now = time.monotonic()

    hit = _basic_cache.get(key)
    if hit is not None:
        epoch, checked_at, username = hit
        if epoch == _credential_epoch and now - checked_at < CACHE_TTL_S:
            return Principal(username=username, method="basic")
        _basic_cache.pop(key, None)

    rejected = _basic_rejected.get(key)
    if rejected is not None:
        epoch, seen_at = rejected
        if epoch == _credential_epoch and now - seen_at < CACHE_TTL_S:
            return None
        _basic_rejected.pop(key, None)

    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None

    address_key = f"ip:{address}"
    user_key = f"user:{username[:128]}"
    if ratelimit.retry_after(address_key) is not None:
        return None
    delay = ratelimit.delay_for(user_key)
    if delay:
        await asyncio.sleep(delay)

    if not await verify_credential(username, password):
        ratelimit.record_failure(address_key, user_key)
        _remember(_basic_rejected, key, (_credential_epoch, now))
        return None

    ratelimit.record_success(address_key, user_key)
    resolved = _credential_username or username
    _remember(_basic_cache, key, (_credential_epoch, now, resolved))
    return Principal(username=resolved, method="basic")


__all__ = [
    "API_KEY_COOKIE_NAME",
    "API_KEY_HEADER",
    "API_KEY_PARAM",
    "CACHE_TTL_S",
    "COOKIE_NAME",
    "CREDENTIAL_ID",
    "DEFAULT_SESSION_HOURS",
    "MIN_API_KEY_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "SECURE_API_KEY_COOKIE_NAME",
    "SECURE_COOKIE_NAME",
    "Principal",
    "SessionInfo",
    "api_key_enabled",
    "clear_credential",
    "configured_api_key",
    "configured_username",
    "create_session",
    "credential_configured",
    "ensure_credential_loaded",
    "generate_api_key",
    "list_sessions",
    "misconfigured",
    "normalise_username",
    "require_login_setting",
    "reset_auth_state",
    "resolve_api_key",
    "resolve_basic",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "revoke_session_id",
    "set_credential",
    "sign_in_required",
    "token_from_cookies",
    "verify_credential",
]
