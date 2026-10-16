"""In-memory abuse caps for the calculator endpoints (Oct 14 2026).

Library: `limits` MovingWindowRateLimiter on MemoryStorage (exact, no burst-at-boundary). Properties the callers rely on:
  * Per process, reset by a restart, and N workers would each count separately. That is acceptable ONLY for per-IP caps: an IP flood is bounded by
    the IP, not by a victim. Everything that protects a victim mailbox or our sending domain is durable (durable_limits.py).
  * Memory: MemoryStorage trims a key's timestamps when its window has passed, but (limits 5.8.0, measured) it never removes the key itself, so a
    stream of distinct keys (one per address) would grow the dict without bound. sweep() below removes keys whose newest hit has expired, at most once
    every SWEEP_INTERVAL_S, so memory is bounded by the number of distinct keys seen inside the longest window (1 day), not by all keys ever seen.
    sweep() reads the library's internals (events, locks, Entry.expiry); that is why requirements pin limits==5.8.0 exactly and a test covers it.
  * A refused hit is not recorded (a blocked caller does not extend their own block), an allowed hit is.
  * Fails CLOSED: if the limiter itself raises, the request is treated as over the cap (a broken limiter must never become an open mail relay).
"""
import logging
import threading
import time

from limits import parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter

logger = logging.getLogger("cdai.calculator_leads")

_store = MemoryStorage()
_limiter = MovingWindowRateLimiter(_store)

# Per-IP caps only. The per-mailbox, global and results-mail caps are durable and live in durable_limits.py.
SEND_CODE_PER_IP = parse("5/hour")        # OTP request (mail from our domain to a caller-chosen address)
VERIFY_PER_IP = parse("20 per 10 minutes")  # OTP guess (6 digits, 10 minute life)
# funnel ping and live save
VIEW_PER_IP = parse("60/minute")
UPDATE_PER_IP = parse("120/minute")   # browser debounce is ~900 ms, about 66 a minute while typing


SWEEP_INTERVAL_S = 60
_sweep_lock = threading.Lock()
_last_sweep = time.monotonic()


def sweep() -> int:
    """Drop every key whose newest hit has already expired (nothing in it can still count). Returns how many keys were dropped."""
    dropped = 0
    now = time.time()
    for key in list(_store.events.keys()):
        entries = _store.events.get(key)
        if entries is not None and entries and entries[0].expiry > now:
            continue                       # newest hit still inside its window: live
        with _store.locks[key]:
            entries = _store.events.get(key)
            if entries is not None and entries and entries[0].expiry > now:
                continue                   # a hit landed while we waited for the lock
            _store.events.pop(key, None)
            dropped += 1
        _store.locks.pop(key, None)
    return dropped


def _maybe_sweep() -> None:
    global _last_sweep
    if time.monotonic() - _last_sweep < SWEEP_INTERVAL_S:
        return
    if not _sweep_lock.acquire(blocking=False):
        return                             # another request is already sweeping
    try:
        _last_sweep = time.monotonic()
        sweep()
    except Exception as e:                 # housekeeping must never change an allow/deny decision
        logger.error("[CALC] rate limiter sweep error: %s", type(e).__name__)
    finally:
        _sweep_lock.release()


def allow(limit, key: str, namespace: str) -> bool:
    """Consume one hit. True = allowed. Any limiter error = False (fail closed)."""
    _maybe_sweep()
    try:
        return bool(_limiter.hit(limit, key, namespace))
    except Exception as e:
        logger.error("[CALC] rate limiter error: %s", type(e).__name__)
        return False


def reset() -> None:
    """Forget every counter (tests only)."""
    _store.reset()
