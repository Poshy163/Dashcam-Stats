"""Password hashing.

``hashlib.scrypt`` rather than bcrypt or argon2, because it is in the standard library and
those are not. This image already carries OpenVINO, OpenCV and two model runtimes; adding a
compiled dependency with its own wheel-availability story, for one function called once per
login, is not a trade worth making. scrypt is memory-hard, which is the property that
matters here, and it ships with Python.

The parameters below cost about 32 MiB and 125 ms per verification. That is the point: it
is negligible for a person signing in and ruinous for anyone working through a word list
against ``dashcam.joshualeaper.dev`` -- or against a stolen copy of the database, which is
downloadable in one click from Settings > Advanced and now carries this hash.

Every hash records the parameters it was made with, so raising them later does not
invalidate stored passwords -- :func:`needs_rehash` says which ones are behind, and the
login path upgrades them in place the next time the correct password is offered.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: CPU/memory cost. Memory use is ``128 * N * R`` bytes -- 32 MiB at these values, and
#: about 125 ms on the machine this was written on.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

#: Passed explicitly, and it has to be.
#:
#: ``hashlib.scrypt`` defaults ``maxmem=0``, which means OpenSSL's own 32 MiB ceiling, and
#: ``128 * N * R`` reaches exactly that at ``N = 2**15``. Left implicit, the first attempt
#: to raise the cost would not have produced a slower hash -- it would have raised
#: ``ValueError: memory limit exceeded`` from inside :func:`hash_password`, on the
#: operator's next password change, which is to say the upgrade path this module advertises
#: would have been unusable at the moment it was first needed. The headroom here covers
#: ``N = 2**17`` without touching this line again.
SCRYPT_MAXMEM = 192 * 1024 * 1024

_PREFIX = "scrypt"


def hash_password(password: str) -> str:
    """Hash ``password`` into a self-describing string safe to store."""
    salt = secrets.token_bytes(SALT_BYTES)
    derived = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        (
            _PREFIX,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64(salt),
            _b64(derived),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored hash.

    A malformed or unrecognised hash returns False rather than raising. The only way one
    gets into the table is a hand-edited database or a partially written row, and neither
    should turn every request into a 500 -- it should mean "this password does not match",
    which sends the operator to the recovery command.
    """
    parsed = _parse(encoded)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    return hmac.compare_digest(_derive(password, salt, n, r, p), expected)


def needs_rehash(encoded: str) -> bool:
    """True when a stored hash was made with weaker parameters than the current ones."""
    parsed = _parse(encoded)
    if parsed is None:
        return True
    n, r, p, _salt, _expected = parsed
    return (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)


def dummy_verify() -> None:
    """Burn one verification's worth of time against a throwaway hash.

    Called when there is no credential to check against, so that "no account is
    configured" and "wrong password" take the same time to answer. Without it the login
    endpoint tells an anonymous caller whether the deployment has an account at all, purely
    from how fast it says no.
    """
    global _dummy
    if _dummy is None:
        # Built on first use rather than at import: this costs a full derivation, and
        # importing the app should not pay for a code path most deployments never take.
        _dummy = hash_password(secrets.token_urlsafe(16))
    verify_password("", _dummy)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=KEY_BYTES,
        maxmem=SCRYPT_MAXMEM,
    )


def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != _PREFIX:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return None
    # scrypt raises on parameters it considers out of range, and the values here come from
    # the database rather than from this module -- a bad row must not become a 500.
    if n < 2 or n & (n - 1) or r < 1 or p < 1 or not salt or not expected:
        return None
    # Refused here rather than left to raise out of ``scrypt`` further down. A row asking
    # for more memory than the ceiling allows is a bad row, and a bad row must read as
    # "this password does not match" rather than as a 500 on every sign-in attempt.
    if 128 * n * r > SCRYPT_MAXMEM:
        return None
    return n, r, p, salt, expected


#: Filled by the first :func:`dummy_verify` call and reused after that.
_dummy: str | None = None
