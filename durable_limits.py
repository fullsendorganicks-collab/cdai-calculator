"""Durable (PostgreSQL) abuse caps for the calculator service (Oct 15 2026).

Why a table: the in-memory caps in rate_limits.py reset on a restart and count per process. The caps that bound the harm to a victim mailbox and to
our sending domain (per-mailbox code requests, per-mailbox guesses, per-mailbox results mails, and one global ceiling on OTP mails) must survive a
restart and hold across several workers, so they live in calculator_rate_events. The per-IP caps stay in memory (cheap, and an IP flood needs no
durability: it is bounded by the IP, not by a victim).

Contract
  allow(bucket, key, limit, window_seconds) -> row id (the "handle") when the hit was recorded, None when over the cap OR when anything failed.
  refund(handle) deletes that hit (used when the mail it paid for was never sent, so a failed send does not burn the visitor's allowance).
One transaction per allow(): pg_advisory_xact_lock on (bucket, key) serialises concurrent callers for the same key, so N concurrent requests can never
record more than `limit` hits. Fails CLOSED: a database error is "over the cap" (None), and the caller sends nothing.
Keys are stored hashed (SHA-256, first 32 hex), never as an e-mail address. Rows older than 2 days are purged opportunistically.
Table: created idempotently on first use per process (same pattern as calculator_views); no migration step.
"""
import hashlib
import logging
import os
import threading
from typing import NamedTuple, Optional

import psycopg2

logger = logging.getLogger("cdai.calculator_leads")

PURGE_AFTER = "2 days"          # longest window is 1 day
PURGE_EVERY_CALLS = 100         # opportunistic purge cadence (calls per process)
_DDL_LOCK_ID = 7305210001       # advisory lock id for the CREATE statements (concurrent first use, several workers)

_DDL = (
    """CREATE TABLE IF NOT EXISTS calculator_rate_events (
           id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
           bucket TEXT NOT NULL,
           key TEXT NOT NULL,
           at TIMESTAMPTZ NOT NULL DEFAULT NOW()
       )""",
    "CREATE INDEX IF NOT EXISTS calculator_rate_events_bucket_key_at ON calculator_rate_events (bucket, key, at)",
    "CREATE INDEX IF NOT EXISTS calculator_rate_events_at ON calculator_rate_events (at)",     # serves the purge
)


class Cap(NamedTuple):
    bucket: str
    limit: int
    window_seconds: int


SEND_CODE_PER_MAILBOX = Cap("code_mailbox", 3, 3600)
SEND_CODE_GLOBAL = Cap("global_code", 100, 3600)      # circuit breaker across the whole service; key is always "*"
VERIFY_PER_MAILBOX = Cap("verify_mailbox", 6, 600)
RESULTS_PER_MAILBOX = Cap("results_mailbox", 3, 86400)
GLOBAL_KEY = "*"

_ready = False
_ready_lock = threading.Lock()
_calls = 0
_calls_lock = threading.Lock()


def connect():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf8")).hexdigest()[:32]


def ensure_table() -> None:
    """Create the table and indexes once per process. Raises on failure (callers fail closed)."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_DDL_LOCK_ID,))   # CREATE ... IF NOT EXISTS can still race across workers
            for stmt in _DDL:
                cur.execute(stmt)
            conn.commit()
            _ready = True
        finally:
            conn.close()


def purge() -> int:
    """Delete rows older than PURGE_AFTER. Returns rows deleted; never raises."""
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM calculator_rate_events WHERE at < NOW() - INTERVAL '{PURGE_AFTER}'")
            n = cur.rowcount
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception as e:
        logger.error("[CALC] rate-event purge failed: %s", type(e).__name__)
        return 0


def _maybe_purge() -> None:
    global _calls
    with _calls_lock:
        _calls += 1
        due = _calls % PURGE_EVERY_CALLS == 0
    if due:
        purge()


def allow(bucket: str, key: str, limit: int, window_seconds: int) -> Optional[int]:
    """Record one hit if fewer than `limit` hits for (bucket, key) fall in the last `window_seconds`. Returns the row id, or None (over cap or error)."""
    try:
        ensure_table()
        k = _hash(key)
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (bucket + ":" + k,))
            cur.execute(
                "SELECT count(*) FROM calculator_rate_events WHERE bucket = %s AND key = %s AND at > clock_timestamp() - make_interval(secs => %s)",
                (bucket, k, window_seconds))
            if cur.fetchone()[0] >= limit:
                conn.commit()
                handle = None
            else:
                cur.execute("INSERT INTO calculator_rate_events (bucket, key, at) VALUES (%s, %s, clock_timestamp()) RETURNING id", (bucket, k))
                handle = cur.fetchone()[0]
                conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("[CALC] durable limiter error: %s", type(e).__name__)
        return None
    _maybe_purge()
    return handle


def hit(cap: Cap, key: str) -> Optional[int]:
    return allow(cap.bucket, key, cap.limit, cap.window_seconds)


def refund(handle: Optional[int]) -> bool:
    """Give back a hit (the mail it paid for was not sent). Never raises."""
    if handle is None:
        return False
    try:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM calculator_rate_events WHERE id = %s", (handle,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.error("[CALC] durable limiter refund failed: %s", type(e).__name__)
        return False
