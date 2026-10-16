# Calculator service: abuse caps, input limits, escaping, log hygiene (Oct 14 2026, revised Oct 16 2026)

Branch `fix/calculator-abuse-caps-oct14` off `origin/main` (2f57426). Not merged, no pull request, nothing deployed. Scope: this repo only (`cdai-calculator`).

## Problem (all read in the code, `calculator_leads.py` at 2f57426)

1. `POST /calculator-send-code` is unauthenticated and unlimited and mails a code to any address from `directives@alloceraintelligence.com`. Email-bombing vector and sending-domain reputation risk (the same domain sends client email).
2. `POST /calculator-lead` has no attempt limit; the OTP is 6 digits from `random.randint` (not cryptographic), 10-minute life. Brute-forceable. A verified fake lead then triggers a results email to the victim address.
3. `name` is interpolated unescaped into the OTP email HTML; `company` unescaped into the results email HTML and subject (HTML and header injection from our own domain).
4. `POST /calculator-view` inserts unbounded rows with an unbounded `session_id`.
5. No size limits on name, company, email, inputs, results.
6. Logs contain email addresses (and company, and exception text that can contain either).
7. The results email says "Every two weeks, free." about the newsletter: an unprovable cadence claim (removed everywhere else by the founder).

Production facts, measured read-only by the founder on Oct 14 2026: `calculator_otps` 4 rows total, 0 in the last 24 h; `calculator_leads` 9; `calculator_views` 314. No abuse seen. The hole is latent, not exploited.

## Design (before code)

**Environment.** Render web service `srv-d8nuvaojs32c73e4kpng`, `uvicorn main:app`, Python 3.14 (`.python-version` 3.14.3; the engine already runs `limits` 5.8.0 on 3.14.3), Supabase Postgres via `DATABASE_URL` (transaction pooler; the limiter uses only transaction-scoped features: `pg_advisory_xact_lock`, `SET LOCAL`, plain statements). The service role is the table owner (`postgres`), as for `calculator_leads`, `calculator_otps` and `calculator_views`. Resend for mail.

**Two kinds of cap, chosen by what each protects.**
- *Per-IP caps* (in memory, `rate_limits.py`, `limits==5.8.0` MovingWindowRateLimiter): an IP flood is bounded by the IP, so these need no durability. They reset on a restart and count per process; acceptable only because every cap that protects a victim mailbox or our sending domain is durable.
- *Per-mailbox, global, claim and results caps* (PostgreSQL, `durable_limits.py`): restart-proof and multi-worker-proof. Table `calculator_rate_events(id identity pk, bucket text, key text, at timestamptz default now())`, indexes `(bucket, key, at)` and `(at)`. `allow(bucket, key, limit, window_seconds)` runs ONE transaction: `SET LOCAL lock_timeout '2s'` and `statement_timeout '5s'`, `pg_advisory_xact_lock(hashtext(bucket:key))`, count rows for the key inside the window, insert one if under the limit, return its id (the handle) else None. The lock makes N concurrent callers record at most `limit` hits. `refund(handle)` deletes the row. Keys are stored as a SHA-256 prefix, never as an address. Any error returns None, i.e. "over the cap": nothing is sent. Every connection has `connect_timeout=5`, so a stuck lock or a slow database refuses the request in seconds instead of hanging it.

**Caps** (all before any mail; the IP cap also before the body is parsed into the request):

| Endpoint | Per IP bucket (memory) | Durable | Over cap |
|---|---|---|---|
| send-code | 5 / hour | 3 / hour per mailbox; 100 / hour service-wide | 429, generic text; no OTP row, no mail |
| verify (`POST /calculator-lead`) | 20 / 10 min | 6 / 10 min per mailbox; 300 / hour service-wide; both consumed on every attempt before the code lookup, after all cheap validation | 429, generic text |
| view | 60 / minute | n/a | `{"status":"ok"}`, nothing stored |
| PATCH | 120 / minute | results mail 3 / day per mailbox; per-lead send mutex 1 per hour | 429 (IP); lead saved and mail skipped (mailbox) |

"Mailbox" = `_cap_key(address)`: `+tag` dropped everywhere; dots dropped and googlemail read as gmail for Google addresses only. The stored address and the code lookup stay exact. The 429 body is always `Too many requests. Please try again later.`

**Service-wide ceilings, trade-off stated.** 100 OTP mails per hour (`global_code`) and 300 verify attempts per hour (`global_verify`), key `*`. Legitimate volume is about 0.01 OTP per hour (4 OTP rows in the whole life of the table at the Oct 14 measurement). A many-IP attacker can still cause up to 100 mails an hour and can FILL either ceiling and pause the funnel for up to an hour; the alternative is an unbounded relay on the sending domain that also sends client email, and unbounded durable writes. When a ceiling refuses a visitor, that visitor's own mailbox hit is refunded.

**Row growth bound.** Maximum durable rows written per hour, independent of the number of IPs: 100 (`global_code`) + 300 (`global_verify`) + at most 100 `code_mailbox` rows (each is paired with a global one) + at most 300 `verify_mailbox` rows + results rows (3 per mailbox per day, and a results hit needs an existing verified lead, itself bounded by the verify ceiling) + one claim row per lead save that wins the mutex. Rows older than 2 days are purged. Anything above these ceilings is refused before it writes.

**A failed send does not burn the allowance.** Failed OTP mail: the OTP row is deleted and both hits refunded. Failed results mail: the mailbox hit and the claim are refunded. A verify attempt is never refunded (except when the global ceiling, not the guess, refused it).

**Exactly-once results mail (rewritten; the first atomic-claim design could leave a lead stuck with `email_sent = TRUE` and no mail).** `email_sent` is no longer used as the claim. Per lead, a durable mutex (`results_claim`, key = lead id, 1 per 3,600 s, expires by itself) is taken first; concurrent saves serialise on the advisory lock so one wins. The winner takes the mailbox hit, sends, and ONLY after the mail really went out sets `email_sent = TRUE` (up to 3 tries, each on a fresh connection, since the request's own connection may be dead). On a failed send both hits are refunded so the next save can deliver. If the process dies between the claim and the send, the orphan claim blocks that lead for up to an hour and then a later save sends. The claim row of a successful send stays, so a failed mark cannot cause a second mail within the hour.

**PATCH robustness.** `_num()` maps anything that is not a finite real number to 0 (no mail, 200). `send_results_email` coerces every number it formats. Stored JSON is sanitised: NUL characters and lone surrogates (in strings and keys, as `\u0000` and unpaired surrogate escapes) are removed while valid surrogate pairs and escaped backslashes are kept, NaN/Infinity become null, and the 20,000-character cap is measured after sanitising. Names and addresses that cannot be encoded as UTF-8 (lone surrogate) are 400, not a 500 at insert. The lead id must be the canonical UUID text (`str(UUID(x)) == x.lower()`), so `urn:uuid:` forms, braces, trailing newlines and unhyphenated forms are 404.

**Request body cap before parsing.** Pure-ASGI `BodyLimitMiddleware` in `main.py`, inside CORS: declared Content-Length over 65,536 (or unparseable) is answered 413 without running the app; a streamed body is counted and the first byte over the limit stops delivery to the app and whatever it answers becomes 413.

**Real client address (corrected on review).** The round-2 rule "trust `request.client.host` when public" was WRONG: uvicorn 0.54's proxy-headers handling, in the always-trust mode used on Render (`FORWARDED_ALLOW_IPS='*'`), rewrites `scope['client']` to the FIRST `X-Forwarded-For` entry, which a visitor controls (Render's `ip:0` log lines show the rewrite is active on this service). The tests now prove this premise through uvicorn's real `ProxyHeadersMiddleware`. Rule now (the engine's proven method): read the RAW header and use its LAST hop (Render appends the true client last); only if the header is absent, empty or its last hop is not an IP fall back to `client.host`, then `unknown`. Tested through `ProxyHeadersMiddleware` with `trusted_hosts='*'` and `'127.0.0.1'`: forged first entries never change the counted address. If the resolved caller is `unknown` or non-public (every visitor could then share one bucket) ONE warning per process is logged, naming no address. IPv6 is reduced to its /64, IPv4-mapped IPv6 to IPv4.

**Table DDL and security posture.** The service creates `calculator_rate_events` itself at first use (same self-creating pattern as `calculator_views`); there is no separate migration. If the table and both indexes already exist NOTHING is run, so a role without CREATE works once the table exists. Otherwise: `CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and, for the roles that exist (`anon`, `authenticated`), `REVOKE ALL` on the table and its identity sequence: the same posture production already has on the other three calculator tables (RLS on, no policies, no rights for the client roles; verified read-only on production). If it cannot be created, the limiter fails closed and logs ONE line per process. If the table is dropped later, the failing call is refused and the next call re-creates it.

**Purge.** Rows older than 2 days are deleted in batches of 1,000 (at most 20 batches per run), single-flight across workers through `pg_try_advisory_xact_lock`, on a daemon thread started every 100th limiter call and never on the request path.

**Memory.** limits 5.8.0 `MemoryStorage` never removes a key by itself (measured), so `rate_limits.sweep()` drops expired keys at most every 60 s. The sweep reads library internals, hence the exact pin.

**Behaviour at 10,000 requests per minute.** Refused per-IP traffic costs O(1) dictionary work and no database connection. Each send-code, verify or results decision costs short extra transactions on their own connections, all closed before the request ends; the lookup is by `(bucket, key, at)`; the global keys are single contended advisory locks held for a few milliseconds, bounded by `lock_timeout` (a burst that cannot get the lock inside 2 s is refused, fail closed). Test: 120 and 330 simultaneous requests are decided exactly (100 and 300 allowed).

## Changes

- `calculator_leads.py`, `durable_limits.py`, `rate_limits.py`, `client_ip.py`, `main.py`, `requirements.in`, `requirements.txt` (every dependency pinned to the versions in Render's Oct 8 build log; `limits` and its dependencies as installed by the engine on Python 3.14.3), `.python-version`, `tests/`, `mut_calculator_abuse.py`.

## Proof

- `tests/test_calculator_abuse_caps.py`: 299 checks, run 3 times with identical results, and once in a clean venv with the exact pinned package versions. Throwaway local PostgreSQL 16 with the three production tables and Supabase-like `anon`/`authenticated` roles with default grants; Resend replaced by a recorder; nothing leaves the machine. Round 3 adds: client address through uvicorn's real proxy-header middleware; the exactly-once mutex (concurrent saves, failed send, dropped connection after a failed and after a successful send, flaky mark-sent, process-death simulation with back-dated claim); RLS and revoked rights (with a control proving the default grants exist); self-healing after a dropped table or index; a role without CREATE (works when the table exists, one log line when it does not); connect and lock/statement timeouts (a held advisory lock makes `allow()` refuse after about 2 s, not hang); the service-wide verify ceiling (boundary, time travel, 330 concurrent); hostile JSON and identifiers; validation-before-consumption; bounded, single-flight, off-path purge.
- `tests/test_requirements_pinned.py`: fails on any `requirements.txt` line that is not `name==version`, an unpinned direct dependency, `limits` not 5.8.0, a missing limits dependency, or an unpinned Python version.
- `mut_calculator_abuse.py`: 172 mutants, each killed by a named assertion. Result: 172 of 172 killed in the full run, no survivors (the full run was executed on exactly the committed code and test files).

## UNVERIFIED

- Python 3.14: the tests ran on the sandbox's Python 3.13, with the exact pinned package versions in a clean venv. Render runs 3.14 and the engine runs `limits` 5.8.0 on 3.14.3, but this service's own code and tests were not run on 3.14, and psycopg2-binary's cp314 wheel was not exercised here.
- The production database: the service role CAN create tables (`calculator_views` exists, owner `postgres`, created by this service's own code) and advisory transaction locks were verified working in production by a probe. Not measured: the added per-request latency through the real pooler, and the per-request connection count under load.
- That the deployed WordPress page equals the file in the engine repo (`docs/website/homepage-full-with-marquee.html`); checked there: any non-OK answer shows a generic message and re-enables the button, so 429/413/400 cannot leave a stuck spinner.
- Windows are proven by time travel (patched clock and SQL back-dating), not by waiting real hours.
- After deploy, send one request with a forged `X-Forwarded-For` and confirm in the log which address is counted (the unit tests prove the rule, not Render's header handling).

## What this cannot stop (inherent)

1. A many-IP attacker can still cause up to the service-wide ceilings (100 OTP mails and 300 verify attempts per hour) and can fill them to pause the funnel for up to an hour.
2. A third party who knows an address can burn that address's per-mailbox attempts (6 verifies per 10 minutes, 3 codes per hour) and delay that one person's unlock by up to an hour (no data exposure, no way past the code).
3. The front end is WordPress and not in this repo.

Documented, not coded: while a mailbox is results-capped each save repeats one claim/refund pair (two small writes); a Resend error raised after Resend accepted the mail counts as a failed send (so one duplicate mail is possible in that case); three database failures in a row right after a successful send can leave `email_sent` FALSE, and the lead is mailed once more after the claim expires an hour later; 5 codes per hour per IP bucket can stop a sixth person behind one office NAT.

## Rollback

Revert the commit and `DROP TABLE calculator_rate_events;`. No other data change. Env vars unchanged.

## Founder decisions needed

None to merge. Recommended as defence in depth: a Resend monthly sending limit and a Beehiiv spend limit.

## Earlier review rounds (kept for the record)

Round 1: IPv4-mapped IPv6 collapsed into one bucket; valid addresses with an apostrophe or new-style TLD were refused; `+tag`/Google-dot spellings multiplied buckets (now `_cap_key`). Round 2 (this record's first revision): durable caps, atomic claim, `_num`, body cap, pins. Round 3 (this revision): client address premise corrected, stuck-claim redesign, RLS/revoke, no-CREATE role, timeouts, verify ceiling, hostile JSON, self-heal, bounded purge.
