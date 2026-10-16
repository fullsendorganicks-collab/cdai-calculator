# Calculator service: abuse caps, input limits, escaping, log hygiene (Oct 14 2026, revised Oct 15 2026)

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

**Environment.** Render web service `srv-d8nuvaojs32c73e4kpng`, `uvicorn main:app`, Supabase Postgres via `DATABASE_URL` (transaction pooler: the durable limiter uses only transaction-scoped features, `pg_advisory_xact_lock` and plain statements, no session state and no prepared statements), Resend for mail. Python 3.14.3 on Render (`.python-version`); the tests ran on the sandbox's Python 3.13 (see UNVERIFIED).

**Two kinds of cap, chosen by what each protects.**
- *Per-IP caps* (in memory, `rate_limits.py`, `limits==5.8.0` MovingWindowRateLimiter): an IP flood is bounded by the IP, so these need no durability. They reset on a restart and count per process; that is acceptable only because every cap that protects a victim mailbox or our sending domain is durable.
- *Per-mailbox, global and results caps* (PostgreSQL, `durable_limits.py`): restart-proof and multi-worker-proof. Table `calculator_rate_events(id identity pk, bucket text, key text, at timestamptz default now())` with indexes `(bucket, key, at)` and `(at)` (the second serves the purge). `allow(bucket, key, limit, window_seconds)` runs ONE transaction: `pg_advisory_xact_lock(hashtext(bucket:key))`, count rows for the key inside the window, insert one if under the limit, return its id (the handle) else None. The lock makes N concurrent callers record at most `limit` hits. `refund(handle)` deletes the row. Keys are stored as a SHA-256 prefix, never as an address. Rows older than 2 days are purged opportunistically (every 100th call per process). Any database error returns None, i.e. "over the cap": the caller sends nothing.

**Caps** (all checked before any mail; the IP cap also before the body is validated):

| Endpoint | Per IP bucket (memory) | Durable | Over cap |
|---|---|---|---|
| send-code | 5 / hour | 3 / hour per mailbox; 100 / hour for the whole service | 429, generic text; no OTP row, no mail |
| verify (`POST /calculator-lead`) | 20 / 10 min | 6 / 10 min per mailbox, consumed on every attempt before the code lookup | 429, generic text |
| view | 60 / minute | n/a | `{"status":"ok"}`, nothing stored |
| PATCH | 120 / minute | results mail 3 / day per mailbox | 429 (IP); lead saved and mail skipped (mailbox) |

"Mailbox" = `_cap_key(address)`: `+tag` dropped for every provider; dots dropped and googlemail read as gmail for Google addresses only. The stored address and the code lookup stay exact. The 429 body is always `Too many requests. Please try again later.`: it never says which limit tripped and never reveals whether an address exists.

**Global circuit breaker, trade-off stated.** 100 OTP mails per hour across the whole service (bucket `global_code`, key `*`). Legitimate volume is about 0.01 per hour (4 OTP rows in the whole life of the table at the Oct 14 measurement), so the ceiling is four orders of magnitude above real use. An attacker with many IPs can still send up to 100 mails an hour and can also FILL the ceiling and pause the funnel for up to an hour. The alternative is an unbounded mail relay on the sending domain that also sends client email; pausing a lead form is the cheaper failure. When the ceiling refuses a visitor, that visitor's own per-mailbox hit is refunded.

**A failed send does not burn the allowance.** If the OTP mail fails, the OTP row is deleted and both the mailbox and global hits are refunded; a failed results mail refunds its hit and releases the claim (below). A verify attempt is never refunded.

**PATCH race fixed (pre-existing).** Old code read `email_sent`, sent, then set it, so two concurrent saves could both send. Now: save inputs/results, then `UPDATE ... SET email_sent = TRUE WHERE id = %s AND email_sent = FALSE RETURNING id`; only the request that gets a row may send. If the durable results cap refuses, the send fails, or anything is interrupted, `email_sent` goes back to FALSE so a later save can still deliver. While a lead is capped, each save repeats a claim/release pair (two small writes); acceptable at this volume.

**PATCH robustness fixed (pre-existing 500).** `_num()` maps anything that is not a finite real number (string, None, bool, NaN, Infinity, integers too large for a float, lists, dicts) to 0; "no meaningful data" means no mail and a 200. `send_results_email` coerces every number it formats. NaN/Infinity, which Python accepts from a client but `jsonb` rejects, are stored as null instead of failing the save.

**Request body cap before parsing.** Pure-ASGI `BodyLimitMiddleware` in `main.py`, inside CORS (so the 413 carries the CORS header the browser needs): declared Content-Length over 65,536 (or unparseable) is answered 413 without running the app; a streamed or chunked body is counted and the first byte over the limit stops delivery to the app (it sees a disconnect) and whatever the app tries to answer becomes 413.

**Real client address.** Evidence (Render logs of this service, supplied Oct 15): uvicorn's `request.client.host` is already the visitor's true public IP and matches Render's own clientIP request log field. `real_client_ip`: a public (`is_global`) socket address is used and `X-Forwarded-For` is ignored entirely, so a forged header changes nothing; only when the host is missing, not an IP, or private/loopback/link-local/CGNAT/documentation space is the LAST forwarded hop used (if it parses as an IP), else the host or "unknown". IPv6 is reduced to its /64, IPv4-mapped IPv6 to IPv4.

**Fail closed.** In-memory limiter error: refuse. Durable limiter error (database down, table cannot be created): refuse, no OTP row, no mail, no results mail. Cost: a database outage already stopped the calculator; the limiter adds no new way to fail except the table creation (see UNVERIFIED).

**Memory.** limits 5.8.0 `MemoryStorage` trims a key's timestamps after the window but never removes the key (measured), so a stream of distinct addresses would grow its dict. `rate_limits.sweep()` removes keys whose newest hit has expired, at most every 60 s, so memory is bounded by the number of IP buckets seen inside the longest in-memory window (1 hour). The sweep reads library internals, which is why `limits==5.8.0` is pinned exactly and test section 14 covers it. (The engine repo's record claims boundedness without such a sweep; that is wrong for 5.8.0 and should be checked there.)

**Behaviour at 10,000 requests per minute.** Refused per-IP traffic costs O(1) dictionary work and no database connection or mail. Each send-code, verify or results-mail decision costs one short extra database transaction on its own connection (about 2 to 3 connections per send-code, all closed before the request ends); the lookup is by `(bucket, key, at)` index, the table holds at most 2 days of rows, and the global key is a single contended advisory lock held for a few milliseconds. UNVERIFIED on the real pooler (see below).

**Database / DDL, stated honestly.** The service creates `calculator_rate_events` and its two indexes itself at first use, once per process, with `CREATE TABLE/INDEX IF NOT EXISTS` under an advisory lock (the same self-creating pattern as `calculator_views`). There is no separate migration file. Existing tables are queried as before; `calculator_otps` gains `RETURNING id` on its INSERT and a DELETE by id on a failed send.

**What is logged.** Lead id, exception class name, fixed messages. No address, name, company or exception text.

## Changes

- `calculator_leads.py`: durable caps, atomic claim, `_num`, `_json_text`; earlier validation, escaping, `secrets` OTP, subject strip, phrase removal and log cleanup unchanged from round 1.
- `durable_limits.py` (new). `rate_limits.py`: the in-memory per-mailbox and results limits are removed (no dead code). `client_ip.py`, `main.py` (body cap, CORS unchanged), `requirements.in` (new), `requirements.txt` (every dependency pinned), `.python-version` (3.14.3).
- `tests/` and `mut_calculator_abuse.py`.

## Proof

- `tests/test_calculator_abuse_caps.py`: 240 checks, run 3 times with identical results (also once under the exact pinned package versions in a clean venv). Throwaway local PostgreSQL 16 with the three production tables (the rate-event table is created by the service, and a test proves that), Resend replaced by a recorder under a fake key, nothing leaves the machine. New in round 2: client address rules and end-to-end forged-header tests; durable contract (boundary, independence, refund, hashed keys); fail-closed with the database down; refund on failed send; time travel for EVERY cap (N allowed, N+1 refused, still refused 5 s before the window ends, allowed again after it: send-code per IP, send-code per mailbox, global, verify per IP, verify per mailbox, view, PATCH, results per day) using a patched clock for the in-memory caps and SQL back-dating for the durable ones; purge removes only rows older than 2 days; concurrency (20 simultaneous send-codes for one mailbox: exactly 3 OTP rows and mails; 20 simultaneous verifies: exactly 6 reach the code lookup; 120 simultaneous send-codes: exactly 100 allowed; 8 simultaneous saves of one lead: exactly one mail; 10 leads of one mailbox at once: exactly 3 mails), the commit-to-count window widened with a 30 ms pause so a missing lock cannot pass by luck; junk numbers (36 combinations) never fail; body cap by declared length, chunked, exactly at the limit, deep nesting, CORS header on the 413, and the middleware driven directly at ASGI level.
- `tests/test_requirements_pinned.py`: fails on any `requirements.txt` line that is not `name==version`, an unpinned direct dependency, `limits` not 5.8.0, a missing limits dependency, or an unpinned Python version.
- `mut_calculator_abuse.py`: 136 mutants, one or more per safeguard (each cap removed, loosened, re-windowed or mis-keyed; lock removed; off-by-one; fail-open; refunds removed; purge wrong; claim not atomic or never released; `_num` and coercion removed; NaN mapping removed; client address trust errors; body cap removed, loosened, off by one, outside CORS, or leaking the app's answer; pins loosened). Result: 136 of 136 killed (full run: 133 of 136; the three survivors were two results-mail coercion mutants, which the test only exercised with junk that happened not to raise, and the "non-HTTP scope" mutant, which the lifespan-scope test could not distinguish; the tests were strengthened with per-field junk and a websocket scope carrying a Content-Length, and the three mutants re-run and killed. The full run was not repeated after those test additions, which only add checks). Each is killed by a named assertion (the harness prints the first failing assertion).

## UNVERIFIED

- The live Render/Supabase path: that the database role may `CREATE TABLE` / `CREATE INDEX` (if it may not, the durable limiter fails closed and send-code answers 429 until the table exists; mitigation: run the DDL in `durable_limits._DDL` once before deploy), that advisory transaction locks behave as tested through the transaction pooler, and the added per-request latency.
- That the deployed WordPress page equals the file in the engine repo (`docs/website/homepage-full-with-marquee.html`); checked there: any non-OK response from these endpoints shows a generic message and re-enables the button, so 429/413/400 cannot leave a stuck spinner.
- Python 3.14.3: the tests ran on the sandbox's Python 3.13 (with the exact pinned package versions, verified in a clean venv); psycopg2-binary 2.9.13's cp314 wheel was not exercised. The limits internals used by `sweep()` are version-pinned, not version-tested on 3.14.
- Render's behaviour for `request.client.host` is from the logs supplied on Oct 15, not re-tested; after deploy send one request with a forged `X-Forwarded-For` and confirm the cap counts by the real address.
- Windows are proven by time travel (patched clock and SQL back-dating), not by waiting real hours.

## What this cannot stop (inherent)

1. A many-IP attacker can still cause up to the global ceiling (100 OTP mails per hour) and can fill it to pause the funnel for up to an hour.
2. A third party who knows an address can burn that address's per-mailbox attempts (6 verifies per 10 minutes, 3 codes per hour) and delay that one person's calculator unlock by up to an hour: no data exposure, no way past the code.
3. The front end is WordPress and not in this repo: what it sends and how it handles refusals cannot be changed or tested here.

Also true and by design: 5 codes per hour per IP bucket can stop a sixth person behind one office NAT in the same hour; a verify attempt is consumed even if the database then fails.

## Rollback

Revert the commit and `DROP TABLE calculator_rate_events;`. No other data change. Env vars unchanged. To roll back the pins alone, restore the previous `requirements.txt`.

## Founder decisions needed

None to merge. Recommended as defence in depth, not as a fix for any item above: a Resend monthly sending limit and a Beehiiv spend limit.

## Round 1 independent review (kept for the record)

Fixed with a test and a mutant each: an IPv4-mapped IPv6 address collapsed every such caller into one `::/64` bucket (now bucketed as its IPv4 address); valid addresses with an apostrophe or a new-style TLD were refused (now accepted; spaces, brackets, quotes, commas, numeric TLDs and over 254 characters are still refused); `+tag` and Google-dot spellings of one mailbox each had their own bucket (now keyed by `_cap_key`). Checked against the live front end source in the engine repo: any non-OK answer shows a generic message and re-enables the button.
