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
Keys are stored hashed (SHA-256, first 32 hex), never as an e-mail address. Rows older than 2 days are purged opportunistically, in bounded
batches, single-flight, on a daemon thread (never on a request).
Table: if it already exists with both indexes nothing is run; otherwise it is created idempotently (same self-creating pattern as calculator_views),
with row level security on and no rights for the Supabase client roles, like the other calculator tables. A role that cannot create it fails closed
and logs ONE line per process.
Every connection has a connect timeout; every decision transaction has lock_timeout 2 s and statement_timeout 5 s, so a stuck lock or a slow
database makes allow() return None (refuse) instead of hanging the request.
"""
import hashlib
import logging
import os
import threading
from typing import NamedTuple, Optional

import psycopg2
import psycopg2.errors

logger = logging.getLogger("cdai.calculator_leads")

PURGE_AFTER = "2 days"          # longest window is 1 day
PURGE_EVERY_CALLS = 100         # opportunistic purge cadence (calls per process)
PURGE_BATCH = 1000              # rows per DELETE
PURGE_MAX_BATCHES = 20          # per purge run
CONNECT_TIMEOUT_S = 5
_DDL_LOCK_ID = 7305210001       # advisory lock id for the CREATE statements (concurrent first use, several workers)
_PURGE_LOCK_ID = 7305210002     # single-flight purge across workers
_INDEXES = ("calculator_rate_events_bucket_key_at", "calculator_rate_events_at")

_DDL = (
    """CREATE TABLE IF NOT EXISTS calculator_rate_events (
           id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
           bucket TEXT NOT NULL,
           key TEXT NOT NULL,
           at TIMESTAMPTZ NOT NULL DEFAULT NOW()
       )""",
    "CREATE INDEX IF NOT EXISTS calculator_rate_events_bucket_key_at ON calculator_rate_events (bucket, key, at)",
    "CREATE INDEX IF NOT EXISTS calculator_rate_events_at ON calculator_rate_events (at)",     # serves the purge
    # Same posture as calculator_leads / otps / views in production: RLS on, no policies, no rights for the PostgREST roles.
    "ALTER TABLE calculator_rate_events ENABLE ROW LEVEL SECURITY",
    """DO $$
       DECLARE r text; seq text;
       BEGIN
         seq := pg_get_serial_sequence('calculator_rate_events', 'id');
         FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
           IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
             EXECUTE format('REVOKE ALL ON TABLE calculator_rate_events FROM %I', r);
             IF seq IS NOT NULL THEN EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM %I', seq, r); END IF;
           END IF;
         END LOOP;
       END $$""",
)


class Cap(NamedTuple):
    bucket: str
    limit: int
    window_seconds: int


SEND_CODE_PER_MAILBOX = Cap("code_mailbox", 3, 3600)
SEND_CODE_GLOBAL = Cap("global_code", 100, 3600)      # circuit breaker across the whole service; key is always "*"
VERIFY_PER_MAILBOX = Cap("verify_mailbox", 6, 600)
VERIFY_GLOBAL = Cap("global_verify", 300, 3600)       # bounds durable verify writes whatever the number of IPs; key is always "*"
RESULTS_PER_MAILBOX = Cap("results_mailbox", 3, 86400)
RESULTS_CLAIM = Cap("results_claim", 1, 3600)         # per-lead mutex around "send the results mail"; expires by itself if the process dies
GLOBAL_KEY = "*"

_ready = False
_ready_lock = threading.Lock()
_ddl_failure_logged = False
_calls = 0
_calls_lock = threading.Lock()
_purge_thread = None


class TableUnavailable(Exception):
    """The table does not exist and could not be created (already logged once per process)."""


def connect():
    return psycopg2.connect(os.getenv("DATABASE_URL"), connect_timeout=CONNECT_TIMEOUT_S)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf8")).hexdigest()[:32]


LOCK_TIMEOUT_S = 2           # a caller that cannot get its key's lock inside this refuses (fails closed); under a burst bigger than the database can serialise
STATEMENT_TIMEOUT_S = 5      # in that time the tail of the queue is refused, which is the safe direction. Module constants so tests can widen them, never code.


def _timeouts(cur) -> None:
    cur.execute(f"SET LOCAL lock_timeout = '{int(LOCK_TIMEOUT_S)}s'")
    cur.execute(f"SET LOCAL statement_timeout = '{int(STATEMENT_TIMEOUT_S)}s'")


def ensure_table() -> None:
    """Make sure the table and both indexes exist, once per process. If they already exist NOTHING is run (so a role without CREATE works once the
    table has been created). Raises TableUnavailable when it is missing and cannot be created."""
    global _ready, _ddl_failure_logged
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        try:
            conn = connect()
            try:
                cur = conn.cursor()
                _timeouts(cur)
                cur.execute("SELECT to_regclass('public.calculator_rate_events') IS NOT NULL AND "
                            "(SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'calculator_rate_events' AND indexname = ANY(%s)) = %s",
                            (list(_INDEXES), len(_INDEXES)))
                complete = cur.fetchone()[0]
                if not complete:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_DDL_LOCK_ID,))   # CREATE ... IF NOT EXISTS can still race across workers
                    for stmt in _DDL:
                        cur.execute(stmt)
                conn.commit()
                _ready = True
            finally:
                conn.close()
        except Exception as e:
            if not _ddl_failure_logged:
                _ddl_failure_logged = True
                logger.error("[CALC] cannot prepare calculator_rate_events (%s); durable caps refuse every request until it exists", type(e).__name__)
            raise TableUnavailable() from e


def purge() -> int:
    """Delete rows older than PURGE_AFTER in batches of PURGE_BATCH (at most PURGE_MAX_BATCHES per run). Single-flight across workers via
    pg_try_advisory_xact_lock: if another purge holds it, do nothing. Returns rows deleted; never raises."""
    total = 0
    try:
        for _ in range(PURGE_MAX_BATCHES):
            conn = connect()
            try:
                cur = conn.cursor()
                _timeouts(cur)
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (_PURGE_LOCK_ID,))
                if not cur.fetchone()[0]:
                    conn.commit()
                    return total
                cur.execute("DELETE FROM calculator_rate_events WHERE id IN (SELECT id FROM calculator_rate_events "
                            f"WHERE at < NOW() - INTERVAL '{PURGE_AFTER}' LIMIT %s)", (PURGE_BATCH,))
                n = cur.rowcount
                conn.commit()
            finally:
                conn.close()
            total += n
            if n < PURGE_BATCH:
                break
    except Exception as e:
        logger.error("[CALC] rate-event purge failed: %s", type(e).__name__)
    return total


def _maybe_purge() -> None:
    """Every PURGE_EVERY_CALLS-th call starts a purge on a daemon thread (one at a time per process); the request never waits for it."""
    global _calls, _purge_thread
    with _calls_lock:
        _calls += 1
        due = _calls % PURGE_EVERY_CALLS == 0
        if due and (_purge_thread is None or not _purge_thread.is_alive()):
            _purge_thread = threading.Thread(target=purge, daemon=True)
            _purge_thread.start()


def allow(bucket: str, key: str, limit: int, window_seconds: int) -> Optional[int]:
    """Record one hit if fewer than `limit` hits for (bucket, key) fall in the last `window_seconds`. Returns the row id, or None (over cap or error)."""
    global _ready
    try:
        ensure_table()
        k = _hash(key)
        conn = connect()
        try:
            cur = conn.cursor()
            _timeouts(cur)
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
    except TableUnavailable:
        return None
    except Exception as e:
        if isinstance(e, psycopg2.errors.UndefinedTable):
            _ready = False                  # the table was dropped under us: re-ensure on the next call (self-heal)
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
            _timeouts(cur)
            cur.execute("DELETE FROM calculator_rate_events WHERE id = %s", (handle,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.error("[CALC] durable limiter refund failed: %s", type(e).__name__)
        return False
