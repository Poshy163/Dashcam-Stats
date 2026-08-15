"""Throttling for the sign-in paths.

An in-process sliding window, which is the right size for this application: one container,
one uvicorn process, one person signing in. A shared store would add a dependency to a
deployment whose whole premise is that it has none.

**Only the address bucket refuses anyone.** That distinction is the entire design, and the
obvious arrangement gets it backwards. A per-username or global bucket that returns 429
hands any stranger who finds the hostname a way to lock the owner *out*: four bad guesses a
minute against a sixty-per-fifteen-minutes global ceiling keeps it permanently full, and
the owner typing the correct password is refused along with the attacker, indefinitely.
The attacker spends nothing and the escape hatch is a shell on the container.

So the three buckets do different jobs:

* **by address** -- a hard refusal, and the only one. It is the bucket an attacker fills
  for themselves, so filling it costs them their own access and nobody else's.
* **by username** -- a delay, not a refusal. Spreading attempts across addresses still
  converges on one account name, and making each attempt wait is enough to make that
  worthless without ever telling the owner no.
* **globally** -- a delay too, and the backstop for a distributed attempt that matches
  neither of the others.

The delays are bounded and the derivations they guard are capped by a semaphore in
``app.auth.service`` besides, so a flood cannot make the box spend all its time deriving
scrypt hashes -- which was the global bucket's real purpose. Refusing people was never it.
"""

from __future__ import annotations

import time
from collections import deque

#: Failures tolerated per address before it is refused, and per username before its
#: attempts start waiting.
KEY_LIMIT = 10
#: Failures across the whole process before every attempt starts waiting.
GLOBAL_LIMIT = 60
WINDOW_S = 900.0

#: Ceiling on the delay a full username or global bucket imposes. Long enough to make a
#: word list pointless, short enough that the owner mistyping their password twice does not
#: think the app has hung.
MAX_DELAY_S = 3.0

#: Bound on tracked keys, so a flood from many addresses cannot grow this without limit.
MAX_KEYS = 4096

_GLOBAL = "\x00global"

_hits: dict[str, deque[float]] = {}


def retry_after(address_key: str) -> float | None:
    """Seconds until this address may try again, or None when it may try now.

    Deliberately takes one key. The other buckets have :func:`delay_for`, and conflating
    the two is the mistake described at the top of this module.
    """
    return _wait_for(address_key, time.monotonic())


def delay_for(*keys: str) -> float:
    """How long an attempt should be made to wait before it is answered.

    Zero until a bucket is full, then rises with how far past its limit it is, capped at
    :data:`MAX_DELAY_S`. Never refuses.
    """
    now = time.monotonic()
    worst = 0.0
    for key in (*keys, _GLOBAL):
        bucket = _hits.get(key)
        if bucket is None:
            continue
        _prune(bucket, now)
        limit = _limit_for(key)
        if len(bucket) <= limit:
            continue
        worst = max(worst, min(MAX_DELAY_S, 0.25 * (len(bucket) - limit)))
    return worst


def record_failure(*keys: str) -> None:
    now = time.monotonic()
    for key in (*keys, _GLOBAL):
        bucket = _hits.get(key)
        if bucket is None:
            if len(_hits) >= MAX_KEYS:
                _evict(now)
            bucket = _hits.setdefault(key, deque())
        _prune(bucket, now)
        # Bounded per key: the window is what decides when a failure stops counting, and
        # an unbounded deque would let one address decide how much memory this takes.
        if len(bucket) < GLOBAL_LIMIT * 4:
            bucket.append(now)


def record_success(*keys: str) -> None:
    """Forget a key's failures.

    The global bucket keeps its history: one person signing in correctly does not make a
    thousand failures from elsewhere uninteresting. It only ever adds delay, so leaving it
    standing cannot lock anyone out.
    """
    for key in keys:
        _hits.pop(key, None)


def reset() -> None:
    """Drop all state. For tests."""
    _hits.clear()


def _limit_for(key: str) -> int:
    return GLOBAL_LIMIT if key == _GLOBAL else KEY_LIMIT


def _wait_for(key: str, now: float) -> float | None:
    bucket = _hits.get(key)
    if bucket is None:
        return None
    _prune(bucket, now)
    if len(bucket) < _limit_for(key):
        return None
    # The window opens again when the oldest failure still counted falls out of it.
    return max(0.0, bucket[0] + WINDOW_S - now)


def _prune(bucket: deque[float], now: float) -> None:
    cutoff = now - WINDOW_S
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()


def _evict(now: float) -> None:
    for key in [k for k, bucket in _hits.items() if not bucket or bucket[-1] <= now - WINDOW_S]:
        del _hits[key]
    if len(_hits) < MAX_KEYS:
        return
    # Nothing aged out, so the table is full of live attempts. Everything goes except the
    # global bucket -- clearing that one would let a flood large enough to fill this table
    # reset the very counter that is supposed to notice it.
    keep = _hits.get(_GLOBAL)
    _hits.clear()
    if keep is not None:
        _hits[_GLOBAL] = keep
