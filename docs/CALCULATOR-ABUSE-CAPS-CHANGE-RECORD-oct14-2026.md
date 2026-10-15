# Calculator service: abuse caps, input limits, escaping, log hygiene (Oct 14 2026)

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

**Environment.** Render web service, `uvicorn main:app`, ONE process (Procfile). Supabase Postgres via `DATABASE_URL`, one short-lived connection per request (unchanged). Resend for mail. Limits live in process memory: a restart resets them, and with N workers they would be N times looser (today N = 1; if the Procfile ever gets `--workers`, the numbers below must be divided or the store moved to Redis/Postgres, which is an open item, not built).

**Library.** `limits==5.8.0` `MovingWindowRateLimiter` on `MemoryStorage` (exact sliding window, no burst at a window boundary; a refused hit is not recorded, so a blocked caller cannot extend their own block). Exactly this one line was added to `requirements.txt`; other lines untouched (they were already unpinned).

**Real client address.** `client_ip.py` (same semantics as the engine repo's): Render appends the true peer as the LAST `X-Forwarded-For` entry and does not clear the client's own entries, so only the last entry is trusted; the first hop is attacker-controlled. IPv6 is reduced to its /64 (one subscriber can rotate addresses inside a /64).

**Caps** (`rate_limits.py`; checked before any database or email work; consumed on every attempt, success or failure):

| Endpoint | Per IP bucket | Per normalised address (strip + lower) | Over cap |
|---|---|---|---|
| send-code | 5 / hour | 3 / hour | 429, generic text |
| verify (`POST /calculator-lead`) | 20 / 10 min | 6 / 10 min | 429, generic text |
| view | 60 / minute | n/a | `{"status":"ok"}`, nothing stored |
| PATCH | 120 / minute | n/a | 429, generic text |
| results email | n/a | 3 / day, consumed immediately before the send | lead still saved, email skipped, 200 |

The 429 body is always `Too many requests. Please try again later.`: it never says which limit tripped and never reveals whether an address exists. The per-IP cap is a FastAPI dependency, so it is consumed even when the body then fails validation. Order inside a request: IP cap, input validation (400/413), address cap, database, mail. A request refused for validation therefore consumes the IP cap but not the address cap (keys are only created from validated addresses).

Why 6 verify attempts per address per 10 minutes: against 10^6 codes that is a 6 in 10^6 chance per code lifetime per process, independent of how many IPs the attacker has. Why 3 sends per address per hour: at most 3 OTP mails per victim per hour, 3 results mails per day.

**Fail closed.** If the limiter raises, `allow()` returns False: send-code, verify and PATCH answer 429, view stores nothing, no results mail is sent. A broken limiter must never become an open relay. The cost is that a limiter fault blocks the calculator; it logs the exception class so it is visible.

**Behaviour at 10,000 requests per minute.** Each request does O(1) dictionary work under a per-key lock before touching the database, so refused traffic costs no database connection and no mail. Measured finding (limits 5.8.0, tested here): `MemoryStorage` trims a key's timestamps after the window but never removes the key, so a stream of distinct addresses would grow its dict without bound. `rate_limits.sweep()` removes keys whose newest hit has expired, at most every 60 s, from a non-blocking lock so only one request sweeps. Memory is then bounded by the number of distinct keys seen inside the longest window (1 day for the results-email key; keys are only created for addresses that passed validation and, for that key, a verified lead). The sweep reads library internals (`events`, `locks`, `Entry.expiry`), which is why the version is pinned exactly and section 14 of the test covers it. A sweep error is logged and never changes an allow/deny decision. The engine repo's equivalent change record states memory is bounded without this sweep; that statement is wrong for limits 5.8.0 and should be checked there (separate repo, not touched here).

**Database.** No schema change, no new table, no new index, no new query. Tables touched, all as before: `calculator_otps` (INSERT, SELECT by `email, code, verified, expires_at`, UPDATE by id), `calculator_leads` (INSERT, UPDATE ... WHERE id, UPDATE email_sent), `calculator_views` (INSERT; the existing idempotent `CREATE TABLE IF NOT EXISTS` is unchanged). The refused paths make strictly fewer queries than before. Not verified against production indexes (read-only access was not used): the OTP lookup was already unindexed-or-not as before and, at 4 rows, is irrelevant.

**When it cannot tell.** Limiter fault: refuse (above). Unparseable client address: the raw string is used as the key (`ip_bucket` returns it unchanged), so it is still limited. Missing `RESEND_API_KEY`: unchanged (send returns False, send-code answers 500).

**What is logged.** Lead id, exception class name, fixed messages. No email address, name, company, or exception text (psycopg2 and HTTP-client messages can embed the row or URL). No hash of the address was added; correlation is by lead id.

## Changes

- `calculator_leads.py`: caps wired in; validation (`_clean_email` conservative regex, no whitespace/control characters/angle brackets, <= 254; name <= 100; company <= 200; control characters in name/company rejected with 400; `session_id` <= 100 else not stored; PATCH `lead_id` must parse as UUID else 404 before any database call; inputs and results each <= 20,000 characters JSON-encoded else 413 (400 if not serialisable); verify code must be 6 ASCII digits else the usual 400 without a database call); OTP from `secrets.randbelow(10**6)` zero-padded; `html.escape` on name (OTP mail) and company (results mail body); results subject has control characters replaced by spaces and is capped at 200 characters; "Every two weeks, free." became "Free."; every log line stripped of address, name, company and exception text.
- `rate_limits.py` (new), `client_ip.py` (new), `requirements.txt` (+`limits==5.8.0`).
- `tests/` and `mut_calculator_abuse.py` (new). `main.py`, CORS, routes and both email layouts are untouched (route table asserted equal to the original five).
- Unchanged on purpose: normal flow send-code, verify, lead created, PATCH sends exactly one results mail and sets `email_sent`; Beehiiv stays fire-and-forget (test proves a failing Beehiiv call still returns the lead id and logs only the exception class); old unverified OTP rows are still kept.

## Proof

- `tests/test_calculator_abuse_caps.py`: 118 checks, run 3 times, same result each time. Throwaway local PostgreSQL 16 with the three production tables, `resend.Emails.send` replaced by a recorder under a fake key, Beehiiv unconfigured except where `requests.post` is patched; nothing leaves the machine.
  Covers: exact boundaries (Nth allowed, N+1th refused) for every cap; per-IP and per-address independence; first-hop and IPv6 /64 spoofing; refused requests write no OTP row and send no mail; the 7th verify attempt is refused even with the correct code and leaves the code unused; the 6th attempt with the right code succeeds; 3 results mails per address per day (case-insensitive), email sent once per lead; escaping in both mails; CR/LF cannot reach the subject; subject cap; oversize and malformed input refused with no write; non-UUID id with zero database calls; `secrets` is the OTP source and the module does not use `random`; no address, name or company in any log line (including failing Resend and Beehiiv); fail-closed behaviour; limiter memory after 10,000 distinct keys; byte comparison of the OTP mail and the results mail against the ORIGINAL main file (`tests/fixtures/calculator_leads_original_main.py`, also checked equal to `git show 2f57426:calculator_leads.py`): identical except the single phrase change.
- `mut_calculator_abuse.py`: 69 mutants (each cap removed or loosened or its window changed, spoofable first hop, bucket ignored, OTP back to `random`, escaping removed, subject strip and cap removed, size caps removed, UUID check removed, phrase restored, address and exception text logged, over-cap request still stores/sends, sweep removed or wrong, limiter fails open, Beehiiv no longer called, and so on). Result: 69 of 69 killed (first run: 67 of 69; the two survivors were a missing test of the Beehiiv success-path log and a mutant that only removed one of two redundant live-key guards in the sweep; the test was extended and the mutant now removes both guards. The second guard alone is a race-only double check and is equivalent in single-threaded tests). Each is killed by a named assertion (the harness prints the first failing assertion).

## UNVERIFIED

- Behaviour on Render itself (real `X-Forwarded-For` shape, one process): taken from Render's public statement and the engine repo's earlier finding, not tested against the live service.
- That the live service runs with one worker: from the Procfile only.
- Timing of the windows in real time: unit tests check configured amount and window length and the boundary counts; they do not wait an hour for a window to roll.
- `RecursionError` on pathologically nested JSON: FastAPI's own 422 encoder can raise before our code runs (framework behaviour, reproduced in tests); the test asserts nothing is written, not that the status is clean.
- No request-body size limit exists before parsing (uvicorn/Render defaults apply). The 20,000-character caps act after parse.
- The live database was not touched. Production indexes were not checked.

## What this cannot stop

- A distributed attacker using many IPs: per-IP buckets are per address, so a botnet gets 5 sends/hour per IP. The per-address caps (3 OTP mails/hour, 3 results mails/day per victim, 6 guesses/10 min) still bound the harm to any one victim, but not the total volume of mail sent to many different victims. The backstop is the Resend account: **recommended founder action: set a Resend monthly sending limit / spend alert, and a Beehiiv subscriber/spend limit.**
- A restart resets all counters (an attacker who can force restarts gets a fresh budget).
- A real person's mailbox can be sent up to 3 OTP mails an hour by anyone who knows the address (the cap bounds, not removes, this). The code email says "If you didn't request this, you can ignore this email."
- Concurrent PATCHes on one lead before `email_sent` is set can still both send (pre-existing race); the 3-per-day address cap bounds the effect. Not changed (smallest-change rule); named open item.
- A legitimate user behind a shared IP (office NAT) shares the 5 sends/hour bucket.

## Rollback

Revert the commit. No migration, no data change, no new table. Env vars unchanged.

## Founder decisions needed

None to merge. Recommended: Resend/Beehiiv spend limits (above); decide whether the `--workers` setting is ever raised (then these caps must move to a shared store first).

## Independent review (Oct 15, agent given the code and questions) and what was done
**Fixed (each with a test and a mutant):** (1) an IPv4-mapped IPv6 address (`::ffff:1.2.3.4`) collapsed every such caller into one `::/64` bucket: now bucketed as its IPv4 address; (2) the address check refused valid addresses with an apostrophe and new-style TLDs (`xn--...`): now accepted (still refuses spaces, brackets, quotes, commas, numeric TLDs, over 254); (3) plus-tag and Google-dot variants of one mailbox each got their own per-address bucket, so a mailbox could be mail-bombed: the per-address caps (send-code, verify, results email) now key on the real mailbox (`_cap_key`: `+tag` dropped everywhere; dots dropped and googlemail=gmail for Google only); the stored address and the code lookup stay exact.
**Accepted trade-offs, stated plainly:** a third party who knows an address can burn that address's per-address attempts (6 verify per 10 min, 3 codes per hour) and delay that one person's calculator unlock by up to an hour: no data exposure, no way past the code; the alternative was a brute-forceable code. 5 codes per hour per IP bucket can stop a 6th person behind one office NAT in the same hour. A hit is consumed even if Resend or the database then fails. A distributed attacker with many IPs is not stopped (Resend's account limit is the backstop: set a monthly sending limit).
**Checked against the live front end** (`docs/website/homepage-full-with-marquee.html` in the engine repo, the page that calls these endpoints): send-code shows "Something went wrong. Please try again." for any non-OK response and re-enables the button; verify shows the server's `detail` text only for "Invalid or expired code", otherwise the same generic line; autosave only logs. So 429, 413 and the new 400s cannot leave a stuck spinner. UNVERIFIED: that the deployed WordPress page equals this file.
**Unverified:** that Render appends the true client address LAST in `X-Forwarded-For` for THIS service. Evidence for the sibling engine service: its access log shows Stripe's public address (3.18.12.63) as the client on `/billing/webhook`. After deploy, send one request with a forged `X-Forwarded-For` and confirm in the log that the caps count by the real address; if every visitor shared one bucket the calculator would stop sending codes after 5 requests an hour (roll back by reverting the commit).
