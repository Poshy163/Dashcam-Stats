"""Optional sign-in.

Off unless the operator turns it on in Settings > Access, which is the right default for
the trusted-LAN deployment this application was built for. Turning it on is what makes
putting it on a public hostname reasonable.

* :mod:`app.auth.passwords` -- scrypt hashing, standard library only.
* :mod:`app.auth.service` -- the account, the sessions, and the caching that keeps the
  check off the database on the hot path.
* :mod:`app.auth.ratelimit` -- login throttling.
* :mod:`app.auth.gate` -- the ASGI middleware that refuses unauthenticated data requests.
* :mod:`app.auth.recover` -- the way back in, run from the container.
"""

from __future__ import annotations

from app.auth.gate import AuthGate, authenticate, client_ip, request_is_https
from app.auth.service import (
    COOKIE_NAME,
    Principal,
    SessionInfo,
    clear_credential,
    configured_username,
    create_session,
    credential_configured,
    ensure_credential_loaded,
    list_sessions,
    reset_auth_state,
    resolve_session,
    revoke_all_sessions,
    revoke_session,
    revoke_session_id,
    set_credential,
    sign_in_required,
    verify_credential,
)

__all__ = [
    "COOKIE_NAME",
    "AuthGate",
    "Principal",
    "SessionInfo",
    "authenticate",
    "clear_credential",
    "client_ip",
    "configured_username",
    "create_session",
    "credential_configured",
    "ensure_credential_loaded",
    "list_sessions",
    "request_is_https",
    "reset_auth_state",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "revoke_session_id",
    "set_credential",
    "sign_in_required",
    "verify_credential",
]
