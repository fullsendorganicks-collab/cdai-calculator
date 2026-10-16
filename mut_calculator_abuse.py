"""Mutation check for the calculator abuse caps: every mutant must make tests/test_calculator_abuse_caps.py fail.
Run: python mut_calculator_abuse.py        Each row: (name, file, [(old, new), ...]); every `old` must occur exactly once in the file."""
import os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
ROOT = os.path.dirname(os.path.abspath(__file__))
CL, RL, CIP, DL, MAIN, REQ, PYV = "calculator_leads.py", "rate_limits.py", "client_ip.py", "durable_limits.py", "main.py", "requirements.txt", ".python-version"
TEST = os.path.join("tests", "test_calculator_abuse_caps.py")
PINT = os.path.join("tests", "test_requirements_pinned.py")
IPD = 'if not rate_limits.allow(limit, ip_bucket(real_client_ip(request)), namespace):'
M = [
 # --- caps removed
 ("send-code IP cap removed", CL, [('_gate: None = Depends(_ip_gate(rate_limits.SEND_CODE_PER_IP, "calc_code_ip"))', '_gate: None = None')]),
 ("verify IP cap removed", CL, [('_gate: None = Depends(_ip_gate(rate_limits.VERIFY_PER_IP, "calc_verify_ip"))', '_gate: None = None')]),
 ("PATCH IP cap removed", CL, [('_gate: None = Depends(_ip_gate(rate_limits.UPDATE_PER_IP, "calc_update_ip"))', '_gate: None = None')]),
 ("view IP cap removed", CL, [('if not rate_limits.allow(rate_limits.VIEW_PER_IP, ip_bucket(real_client_ip(request)), "calc_view_ip"):', 'if False:')]),
 ("address not lower-cased before keying", CL, [('v = (raw or "").strip().lower()', 'v = (raw or "").strip()')]),
 ("view over cap still inserts", CL, [('"calc_view_ip"):\n        return {"status": "ok"}', '"calc_view_ip"):\n        pass')]),
 # --- review round 1 fixes
 ("IPv4-mapped IPv6 collapses into ::/64", CIP, [('        if a.ipv4_mapped is not None:', '        if False:')]),
 ("apostrophe addresses refused again", CL, [('[^@\\s<>\\",;()', '[^@\\s<>\\\'",;()')]),
 ("new-style TLD refused (letters only)", CL, [('\\.[A-Za-z][A-Za-z0-9-]{1,}")', '\\.[A-Za-z]{2,}")')]),
 ("+tag not dropped from the cap key", CL, [('    local = local.split("+", 1)[0]\n', '')]),
 ("Google dots not dropped from the cap key", CL, [('local, domain = local.replace(".", ""), "gmail.com"', 'local, domain = local, "gmail.com"')]),
 ("googlemail not read as gmail", CL, [('local, domain = local.replace(".", ""), "gmail.com"', 'local, domain = local.replace(".", ""), domain')]),
 ("dots dropped for every provider", CL, [('    if domain in _GOOGLE_DOMAINS:\n        local, domain = local.replace(".", ""), "gmail.com"', '    if True:\n        local, domain = local.replace(".", ""), ("gmail.com" if domain in _GOOGLE_DOMAINS else domain)')]),
 # --- limit values
 ("send-code IP limit loosened", RL, [('parse("5/hour")', 'parse("6/hour")')]),
 ("send-code IP window widened", RL, [('parse("5/hour")', 'parse("5/day")')]),
 ("verify IP limit loosened", RL, [('parse("20 per 10 minutes")', 'parse("21 per 10 minutes")')]),
 ("view limit loosened", RL, [('parse("60/minute")', 'parse("61/minute")')]),
 ("PATCH limit loosened", RL, [('parse("120/minute")', 'parse("121/minute")')]),
 ("limiter fails open", RL, [('logger.error("[CALC] rate limiter error: %s", type(e).__name__)\n        return False', 'logger.error("[CALC] rate limiter error: %s", type(e).__name__)\n        return True')]),
 ("sweep never runs", RL, [('    _maybe_sweep()\n    try:', '    try:')]),
 ("sweep drops live keys", RL, [('continue                       # newest hit still inside its window: live', 'pass'), ('continue                   # a hit landed while we waited for the lock', 'pass')]),
 ("sweep keeps expired keys", RL, [('_store.events.pop(key, None)\n            dropped += 1', 'dropped += 1')]),
 ("sweep leaves locks behind", RL, [('        _store.locks.pop(key, None)\n    return dropped', '    return dropped')]),
 # --- client address
 ("IP key is the spoofable first hop", CL, [(IPD, 'if not rate_limits.allow(limit, (request.headers.get("x-forwarded-for") or "x").split(",")[0].strip(), namespace):')]),
 ("view IP key is the spoofable first hop", CL, [('rate_limits.VIEW_PER_IP, ip_bucket(real_client_ip(request)), "calc_view_ip"', 'rate_limits.VIEW_PER_IP, (request.headers.get("x-forwarded-for") or "x").split(",")[0].strip(), "calc_view_ip"')]),
 ("IPv6 not reduced to /64", CIP, [('return str(ipaddress.ip_network(f"{a}/64", strict=False))', 'return ip')]),
 ("IP key ignores the bucket", CL, [(IPD, 'if not rate_limits.allow(limit, real_client_ip(request), namespace):')]),
 # --- validation
 ("e-mail allows whitespace", CL, [('[^@\\s<>\\",;()', '[^@<>\\",;()')]),
 ("e-mail allows control characters", CL, [('\\\\\\x00-\\x1f\\x7f]+@', '\\\\]+@')]),
 ("e-mail allows angle brackets", CL, [('[^@\\s<>\\",;()', '[^@\\s\\",;()')]),
 ("name/company length uncapped", CL, [('    if len(v) > max_len:', '    if False:')]),
 ("name cap loosened", CL, [('NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200,', 'NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 1000, 200,')]),
 ("company cap loosened", CL, [('NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200,', 'NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 100, 2000,')]),
 ("C1 controls not in the control class", CL, [('_CONTROL_RE = re.compile(r"[\\x00-\\x1f\\x7f-\\x9f]")', '_CONTROL_RE = re.compile(r"[\\x00-\\x1f\\x7f]")')]),
 ("JSON size cap loosened", CL, [('JSON_MAX_CHARS = 100, 200, 254, 100, 20000', 'JSON_MAX_CHARS = 100, 200, 254, 100, 2000000')] if False else [('SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200, 254, 100, 20000', 'SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200, 254, 100, 2000000')]),
 ("session_id length uncapped", CL, [('if payload.session_id is not None and len(payload.session_id) > SESSION_ID_MAX:', 'if False:')]),
 ("code shape not checked before the database", CL, [('    if not _CODE_RE.fullmatch(code):', '    if False:')]),
 ("generic refusal text reveals the limit", CL, [('TOO_MANY_DETAIL = "Too many requests. Please try again later."', 'TOO_MANY_DETAIL = "Too many requests (address limit). Please try again later."')]),
 # --- OTP
 ("OTP back to random.randint", CL, [('code = f"{secrets.randbelow(10**6):06d}"', 'code = f"{__import__(\'random\').randint(0, 999999):06d}"')]),
 ("OTP not zero-padded", CL, [('code = f"{secrets.randbelow(10**6):06d}"', 'code = str(secrets.randbelow(10**6))')]),
 ("OTP space shrunk", CL, [('code = f"{secrets.randbelow(10**6):06d}"', 'code = f"{secrets.randbelow(10**4):06d}"')]),
 # --- escaping / subject / copy
 ("name not escaped in OTP mail", CL, [('Hi {_escape(name)} — enter', 'Hi {name} — enter')]),
 ("company not escaped in results mail", CL, [('<h1>Your true CPL for {company_html}</h1>', '<h1>Your true CPL for {company}</h1>')]),
 ("subject control characters not stripped", CL, [('f"Your distortion audit — {_single_line(company)} "', 'f"Your distortion audit — {company} "')]),
 ("subject length uncapped", CL, [(')[:SUBJECT_MAX]\n        company_html', ')\n        company_html')]),
 ("subject cap off by one", CL, [(')[:SUBJECT_MAX]\n        company_html', ')[:SUBJECT_MAX + 1]\n        company_html')]),
 ("newsletter cadence phrase restored", CL, [('The cost layers your dashboard never shows. Free.', 'The cost layers your dashboard never shows. Every two weeks, free.')]),
 ("escaping changes ordinary text", CL, [('company_html = _escape(company)', 'company_html = _escape(company).replace(" ", "&nbsp;")')]),
 # --- logs
 ("address logged on lead creation", CL, [('logger.info("[CALC] Verified + lead created: %s", lead_id)', 'logger.info("[CALC] Verified + lead created: %s", email)')]),
 ("address logged when the OTP mail is sent", CL, [('logger.info("[CALC] OTP email sent")', 'logger.info("[CALC] OTP email sent %s", email)')]),
 ("exception text logged for the OTP mail", CL, [('logger.error("[CALC] OTP email failed: %s", type(e).__name__)', 'logger.error("[CALC] OTP email failed: %s", e)')]),
 ("exception text logged for the results mail", CL, [('logger.error("[CALC] Results email failed: %s", type(e).__name__)', 'logger.error("[CALC] Results email failed: %s", e)')]),
 ("exception text logged for Beehiiv", CL, [('logger.error("[CALC] Beehiiv subscribe failed: %s", type(e).__name__)', 'logger.error("[CALC] Beehiiv subscribe failed: %s", e)')]),
 ("company logged when the results mail is sent", CL, [('logger.info("[CALC] Results email sent")', 'logger.info("[CALC] Results email sent %s", company)')]),
 ("address logged on Beehiiv success", CL, [('logger.info("[CALC] Beehiiv subscribe requested")', 'logger.info("[CALC] Beehiiv subscribe requested %s", email)')]),
 # --- behaviour that must not change
 ("Beehiiv no longer called", CL, [('        _subscribe_beehiiv(email)\n', '')]),
 # --- round 2: durable per-mailbox / global / results caps
 ("send-code global ceiling not enforced", CL, [('    if h_global is None:', '    if False:')]),
 ("global refusal keeps the visitor's mailbox hit", CL, [('        durable_limits.refund(h_mailbox)         # the visitor did nothing wrong: the service ceiling is what refused\n', '')]),
 ("failed send keeps both allowances", CL, [('        durable_limits.refund(h_mailbox)\n        durable_limits.refund(h_global)\n', '        pass\n')]),
 ("failed send keeps the OTP row", CL, [('        if otp_id is not None:', '        if False:')]),
 ("send-code per-mailbox key is the exact address", CL, [('durable_limits.SEND_CODE_PER_MAILBOX, _cap_key(email))', 'durable_limits.SEND_CODE_PER_MAILBOX, email)')]),
 ("send-code global key varies per caller", CL, [('durable_limits.hit(durable_limits.SEND_CODE_GLOBAL, durable_limits.GLOBAL_KEY)', 'durable_limits.hit(durable_limits.SEND_CODE_GLOBAL, email)')]),
 ("verify per-mailbox key is the exact address", CL, [('durable_limits.VERIFY_PER_MAILBOX, _cap_key(email))', 'durable_limits.VERIFY_PER_MAILBOX, email)')]),
 ("results per-mailbox key is the exact address", CL, [('durable_limits.RESULTS_PER_MAILBOX, _cap_key(str(row["email"]).strip().lower()))', 'durable_limits.RESULTS_PER_MAILBOX, str(row["email"]).strip().lower())')]),
 ("JSON size uncapped", CL, [('    if len(text) > JSON_MAX_CHARS:', '    if False:')]),
 ("PATCH results JSON not size-checked", CL, [('    results_json = _json_text(payload.results, "results")\n\n    conn = get_conn()', '    results_json = json.dumps(payload.results or {})\n\n    conn = get_conn()')]),
 ("_num lets booleans through", CL, [('    if isinstance(v, bool) or not isinstance(v, (int, float)):', '    if not isinstance(v, (int, float)):')]),
 ("_num lets strings through", CL, [('    if isinstance(v, bool) or not isinstance(v, (int, float)):', '    if isinstance(v, bool):')]),
 ("_num lets non-finite numbers through", CL, [('        return v if math.isfinite(v) else 0', '        return v')]),
 ("_num does not catch huge ints", CL, [('    except OverflowError:\n        return 0', '    except ZeroDivisionError:\n        return 0')]),
 ("results mail does not coerce reportedCpl", CL, [('reported_cpl = _num(results.get("reportedCpl", 0))', 'reported_cpl = results.get("reportedCpl", 0)')]),
 ("results mail does not coerce cmPct", CL, [('cm_pct       = _num(results.get("cmPct", 0))', 'cm_pct       = results.get("cmPct", 0)')]),
 ("results mail does not coerce hiddenCosts", CL, [('hidden_costs = _num(results.get("hiddenCosts", 0))', 'hidden_costs = results.get("hiddenCosts", 0)')]),
 ("results mail does not coerce cplGapPct", CL, [('cpl_gap_pct  = _num(results.get("cplGapPct", 0))', 'cpl_gap_pct  = results.get("cplGapPct", 0)')]),
 ("PATCH compares raw adSpend", CL, [('_num(i.get("adSpend", 0)) > 0', 'i.get("adSpend", 0) > 0')]),
 ("PATCH compares raw leads", CL, [('and _num(i.get("leads", 0)) > 0', 'and i.get("leads", 0) > 0')]),
 ("PATCH compares raw trueCpl", CL, [('and _num(r.get("trueCpl", 0)) > 0', 'and r.get("trueCpl", 0) > 0')]),
 # durable_limits.py itself
 ("durable: no per-key lock", DL, [('cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (bucket + ":" + k,))', 'cur.execute("SELECT 1")')]),
 ("durable: off by one at the limit", DL, [('if cur.fetchone()[0] >= limit:', 'if cur.fetchone()[0] > limit:')]),
 ("durable: window ignored", DL, [('clock_timestamp() - make_interval(secs => %s)",\n                (bucket, key, window_seconds))', 'clock_timestamp() - make_interval(secs => 0 * %s)",\n                (bucket, key, window_seconds))')] if False else [('at > clock_timestamp() - make_interval(secs => %s)', 'at > clock_timestamp() - make_interval(secs => 0 * %s)')]),
 ("durable: window doubled", DL, [('at > clock_timestamp() - make_interval(secs => %s)', 'at > clock_timestamp() - make_interval(secs => 2 * %s)')]),
 ("durable: keys share one counter per bucket", DL, [('WHERE bucket = %s AND key = %s AND at', 'WHERE bucket = %s AND %s IS NOT NULL AND at')]),
 ("durable: buckets share counters", DL, [('WHERE bucket = %s AND key = %s AND at', 'WHERE %s IS NOT NULL AND key = %s AND at')]),
 ("durable: fails open", DL, [('        logger.error("[CALC] durable limiter error: %s", type(e).__name__)\n        return None', '        logger.error("[CALC] durable limiter error: %s", type(e).__name__)\n        return 1')]),
 ("durable: refund does nothing", DL, [('cur.execute("DELETE FROM calculator_rate_events WHERE id = %s", (handle,))', 'cur.execute("SELECT %s", (handle,))')]),
 ("durable: purge window wrong", DL, [('PURGE_AFTER = "2 days"', 'PURGE_AFTER = "1 day"')]),
 ("durable: purge never runs", DL, [('due = _calls % PURGE_EVERY_CALLS == 0', 'due = False')]),
 ("durable: purge on every call", DL, [('due = _calls % PURGE_EVERY_CALLS == 0', 'due = True')]),
 ("durable: address stored in the clear", DL, [('        k = _hash(key)', '        k = key')]),
 ("durable: table never created", DL, [('        ensure_table()\n', '')]),
 ("durable: table creation not idempotent", DL, [('CREATE TABLE IF NOT EXISTS calculator_rate_events', 'CREATE TABLE calculator_rate_events')]),
 ("durable: purge index missing", DL, [('    "CREATE INDEX IF NOT EXISTS calculator_rate_events_at ON calculator_rate_events (at)",     # serves the purge\n', '')]),
 ("durable: lookup index missing", DL, [('    "CREATE INDEX IF NOT EXISTS calculator_rate_events_bucket_key_at ON calculator_rate_events (bucket, key, at)",\n', '')]),
 ("durable: global limit loosened", DL, [('Cap("global_code", 100, 3600)', 'Cap("global_code", 101, 3600)')]),
 ("durable: mailbox send limit loosened", DL, [('Cap("code_mailbox", 3, 3600)', 'Cap("code_mailbox", 4, 3600)')]),
 ("durable: mailbox send window shortened", DL, [('Cap("code_mailbox", 3, 3600)', 'Cap("code_mailbox", 3, 60)')]),
 ("durable: verify limit loosened", DL, [('Cap("verify_mailbox", 6, 600)', 'Cap("verify_mailbox", 7, 600)')]),
 ("durable: verify window shortened", DL, [('Cap("verify_mailbox", 6, 600)', 'Cap("verify_mailbox", 6, 60)')]),
 ("durable: results limit loosened", DL, [('Cap("results_mailbox", 3, 86400)', 'Cap("results_mailbox", 4, 86400)')]),
 ("durable: results window shortened", DL, [('Cap("results_mailbox", 3, 86400)', 'Cap("results_mailbox", 3, 3600)')]),
 # client address
 # request body cap
 ("body cap middleware not installed", MAIN, [('app.add_middleware(BodyLimitMiddleware)\n\napp.add_middleware(', 'app.add_middleware(')]),
 ("body cap outside CORS (413 loses CORS headers)", MAIN, [('app.add_middleware(BodyLimitMiddleware)\n\napp.add_middleware(', 'app.add_middleware('), ('app.include_router(calculator_router)', 'app.add_middleware(BodyLimitMiddleware)\napp.include_router(calculator_router)')]),
 ("declared length not checked", MAIN, [('                if declared > self.max_bytes:\n                    return await self._reject(send)', '                if False:\n                    return await self._reject(send)')]),
 ("declared length off by one", MAIN, [('                if declared > self.max_bytes:', '                if declared >= self.max_bytes:')]),
 ("unparseable length accepted", MAIN, [('declared = self.max_bytes + 1       # unparseable length: refuse', 'declared = 0')]),
 ("streamed bytes not counted", MAIN, [('                if received > self.max_bytes:\n                    too_large = True', '                if False:\n                    too_large = True')]),
 ("streamed limit off by one", MAIN, [('                if received > self.max_bytes:\n                    too_large = True', '                if received >= self.max_bytes:\n                    too_large = True')]),
 ("limit loosened", MAIN, [('MAX_BODY_BYTES = 65536', 'MAX_BODY_BYTES = 655360')]),
 ("app's own answer leaks after an overflow", MAIN, [('            if too_large and not rejected:\n                if not started:', '            if False:\n                if not started:')]),
 ("silent app after overflow gets no 413", MAIN, [('        if too_large and not started:\n            await self._reject(send)\n\n', '        pass\n\n')]),
 ("crashing app after overflow is re-raised", MAIN, [('            if too_large and not started:\n                started = rejected = True\n                await self._reject(send)\n                return\n            if too_large:\n                return\n            raise', '            raise')]),
 ("body cap applied to non-HTTP scopes", MAIN, [('        if scope["type"] != "http":\n            return await self.app(scope, receive, send)\n', '')]),
 # pinned dependencies
 ("requirements: a range instead of a pin", REQ, [('\nlimits==5.8.0\n', '\nlimits>=5.8.0\n')], PINT),
 ("requirements: a dependency unpinned", REQ, [('fastapi==0.142.4', 'fastapi')], PINT),
 ("requirements: limits pin changed", REQ, [('\nlimits==5.8.0\n', '\nlimits==5.9.0\n')], PINT),
 ("requirements: limits dependency dropped", REQ, [('wrapt==2.5.0\n', '')], PINT),
 ("requirements: Python version unpinned", PYV, [('3.14.3', '3.14')], PINT),
 # --- round 3
 ("e-mail not validated", CL, [('if len(v) > EMAIL_MAX or not _encodable(v) or not _EMAIL_RE.fullmatch(v):', 'if False:')]),
 ("e-mail with a lone surrogate accepted", CL, [('if len(v) > EMAIL_MAX or not _encodable(v) or not _EMAIL_RE.fullmatch(v):', 'if len(v) > EMAIL_MAX or not _EMAIL_RE.fullmatch(v):')]),
 ("control characters accepted in name/company", CL, [('    if _CONTROL_RE.search(v) or not _encodable(v):', '    if not _encodable(v):')]),
 ("lone surrogate accepted in name/company", CL, [('    if _CONTROL_RE.search(v) or not _encodable(v):', '    if _CONTROL_RE.search(v):')]),
 ("lead id not validated as UUID", CL, [('        if str(uuid.UUID(lead_id)) != lead_id.lower():\n            raise ValueError', '        pass')]),
 ("lead id accepts non-canonical UUID forms", CL, [('        if str(uuid.UUID(lead_id)) != lead_id.lower():\n            raise ValueError', '        uuid.UUID(lead_id)')]),
 ("NaN/Infinity not mapped to null", CL, [('json.loads(_strip_bad_escapes(json.dumps(v)), parse_constant=lambda _c: None)', 'json.loads(_strip_bad_escapes(json.dumps(v)))')]),
 ("NUL and surrogates not stripped from JSON", CL, [('json.loads(_strip_bad_escapes(json.dumps(v)),', 'json.loads(json.dumps(v),')]),
 ("escaped backslash not protected in JSON strip", CL, [('r"(\\\\\\\\)|(\\\\u[dD]', 'r"(\\\\\\\\x)|(\\\\u[dD]')]),
 ("valid surrogate pairs stripped", CL, [('|(\\\\u[dD][89abAB][0-9a-fA-F]{2}\\\\u[dD][c-fC-F][0-9a-fA-F]{2})|', '|(\\\\u[dD][89abAB][0-9a-fA-F]{2}\\\\u[dD][c-fC-F][0-9a-fA-F]{2}x)|')]),
 ("lone surrogates left in JSON", CL, [('|\\\\u0000|\\\\u[dD][89a-fA-F][0-9a-fA-F]{2}")', '|\\\\u0000")')]),
 ("size cap measured before sanitising", CL, [('json.dumps(json.loads(_strip_bad_escapes(json.dumps(v)), parse_constant=lambda _c: None))\n    except', 'json.dumps(json.loads(_strip_bad_escapes(json.dumps(v)), parse_constant=lambda _c: None))\n        if len(json.dumps(v)) > JSON_MAX_CHARS:\n            raise HTTPException(status_code=413, detail=f"{label} too large")\n    except HTTPException:\n        raise\n    except')]),
 ("send-code per-mailbox cap not enforced", CL, [('    if h_mailbox is None:\n        raise _too_many()\n    h_global = durable_limits.hit(durable_limits.SEND_CODE_GLOBAL', '    if False:\n        raise _too_many()\n    h_global = durable_limits.hit(durable_limits.SEND_CODE_GLOBAL')]),
 ("verify per-mailbox cap not enforced", CL, [('    if h_mailbox is None:\n        raise _too_many()\n    if durable_limits.hit(durable_limits.VERIFY_GLOBAL', '    if False:\n        raise _too_many()\n    if durable_limits.hit(durable_limits.VERIFY_GLOBAL')]),
 ("verify global ceiling not enforced", CL, [('    if durable_limits.hit(durable_limits.VERIFY_GLOBAL, durable_limits.GLOBAL_KEY) is None:', '    if False:')]),
 ("verify global refusal keeps the mailbox hit", CL, [('        durable_limits.refund(h_mailbox)         # the ceiling refused, not this visitor\'s guessing\n', '')]),
 ("results per-mailbox cap not enforced", CL, [('                    if h_mailbox is not None:\n                        sent = send_results_email', '                    if True:\n                        sent = send_results_email')]),
 ("failed results send keeps the mailbox hit", CL, [('                        durable_limits.refund(h_mailbox)\n                        durable_limits.refund(h_claim)', '                        durable_limits.refund(h_claim)')]),
 ("failed results send keeps the claim", CL, [('                        durable_limits.refund(h_mailbox)\n                        durable_limits.refund(h_claim)', '                        durable_limits.refund(h_mailbox)')]),
 ("results mail not guarded by a per-lead mutex", CL, [('h_claim = durable_limits.hit(durable_limits.RESULTS_CLAIM, lead_id)', 'h_claim = -1')]),
 ("lead marked sent even when the send failed", CL, [('                    if sent:\n                        _mark_sent(lead_id)', '                    if True:\n                        _mark_sent(lead_id)')]),
 ("marked sent before the send", CL, [('                    h_mailbox = durable_limits.hit(durable_limits.RESULTS_PER_MAILBOX, _cap_key(str(row["email"]).strip().lower()))\n', '                    h_mailbox = durable_limits.hit(durable_limits.RESULTS_PER_MAILBOX, _cap_key(str(row["email"]).strip().lower()))\n                    _mark_sent(lead_id)\n')]),
 ("mark-sent not retried", CL, [('    for _ in range(3):\n        try:\n            c = get_conn()', '    for _ in range(1):\n        try:\n            c = get_conn()')]),
 ("mark-sent retried without limit", CL, [('    for _ in range(3):\n        try:\n            c = get_conn()', '    for _ in range(5):\n        try:\n            c = get_conn()')]),
 ("already-sent leads are mailed again", CL, [('        if meaningful and not row["email_sent"]:', '        if meaningful:')]),
 ("connect timeout missing from get_conn", CL, [('cursor_factory=RealDictCursor, connect_timeout=5)', 'cursor_factory=RealDictCursor)')]),
 ("connect timeout missing from the limiter", DL, [('os.getenv("DATABASE_URL"), connect_timeout=CONNECT_TIMEOUT_S)', 'os.getenv("DATABASE_URL"))')]),
 ("lock_timeout missing", DL, [('    cur.execute("SET LOCAL lock_timeout = \'2s\'")', '    pass')]),
 ("lock_timeout loosened", DL, [('SET LOCAL lock_timeout = \'2s\'', 'SET LOCAL lock_timeout = \'30s\'')]),
 ("statement_timeout missing", DL, [('    cur.execute("SET LOCAL statement_timeout = \'5s\'")', '    pass')]),
 ("durable: verify global limit loosened", DL, [('Cap("global_verify", 300, 3600)', 'Cap("global_verify", 301, 3600)')]),
 ("durable: verify global window shortened", DL, [('Cap("global_verify", 300, 3600)', 'Cap("global_verify", 300, 60)')]),
 ("durable: claim allows two holders", DL, [('Cap("results_claim", 1, 3600)', 'Cap("results_claim", 2, 3600)')]),
 ("durable: claim expires too soon", DL, [('Cap("results_claim", 1, 3600)', 'Cap("results_claim", 1, 60)')]),
 ("durable: claim never expires", DL, [('Cap("results_claim", 1, 3600)', 'Cap("results_claim", 1, 3600000)')]),
 ("durable: row level security not enabled", DL, [('    "ALTER TABLE calculator_rate_events ENABLE ROW LEVEL SECURITY",\n', '')]),
 ("durable: table rights not revoked", DL, [("EXECUTE format('REVOKE ALL ON TABLE calculator_rate_events FROM %I', r);", "NULL;")]),
 ("durable: sequence rights not revoked", DL, [("IF seq IS NOT NULL THEN EXECUTE format('REVOKE ALL ON SEQUENCE %s FROM %I', seq, r); END IF;", "NULL;")]),
 ("durable: DDL run even when the table exists", DL, [('                if not complete:', '                if True:')]),
 ("durable: missing index not noticed", DL, [('indexname = ANY(%s)) = %s"', 'indexname = ANY(%s)) >= %s"'), ('(list(_INDEXES), len(_INDEXES)))', '(list(_INDEXES), 0))')]),
 ("durable: table-prepare failure logged on every request", DL, [('            if not _ddl_failure_logged:', '            if True:')]),
 ("durable: dropped table not re-ensured", DL, [('        if isinstance(e, psycopg2.errors.UndefinedTable):\n            _ready = False', '        if isinstance(e, psycopg2.errors.UndefinedTable):\n            pass')]),
 ("durable: purge unbounded", DL, [('LIMIT %s)", (PURGE_BATCH,))', 'LIMIT 100000000 + %s * 0)", (PURGE_BATCH,))')]),
 ("durable: purge stops after one batch", DL, [('            if n < PURGE_BATCH:\n                break', '            break')]),
 ("durable: purge not single-flight", DL, [('                if not cur.fetchone()[0]:\n                    conn.commit()\n                    return total', '                if False:\n                    conn.commit()\n                    return total')]),
 ("durable: purge deletes everything", DL, [("f\"WHERE at < NOW() - INTERVAL '{PURGE_AFTER}' LIMIT %s)\"", "f\"WHERE at < NOW() + INTERVAL '{PURGE_AFTER}' LIMIT %s)\"")]),
 ("durable: purge runs on the request path", DL, [('            _purge_thread = threading.Thread(target=purge, daemon=True)\n            _purge_thread.start()', '            purge()')]),
 ("client address: socket address trusted over the header", CIP, [('    xff = request.headers.get("x-forwarded-for")\n    if xff:', '    xff = None\n    if xff:')]),
 ("client address: first forwarded hop used", CIP, [('            ip = hops[-1]', '            ip = hops[0]')]),
 ("client address: non-IP last hop accepted", CIP, [('        if hops and _parse(hops[-1]) is not None:', '        if hops:')]),
 ("client address: no fallback to the socket address", CIP, [('        ip = request.client.host if request.client else "unknown"', '        ip = "unknown"')]),
 ("client address: warning logged on every call", CIP, [('        _warned = True\n', '        pass\n')]),
 ("client address: warning for public callers too", CIP, [('    if a is not None and a.is_global:\n        return\n    with _warn_lock:', '    if False:\n        return\n    with _warn_lock:')]),
 ("client address: warning names the address", CIP, [('"\'unknown\'" if a is None else "a non-public address")', 'ip, ip)')]),
 ("client address: no warning at all", CIP, [('    _warn_once_if_shared(ip)\n    return ip', '    return ip')]),
]
SRC = {f: open(os.path.join(ROOT, f), encoding="utf8").read() for f in {m[1] for m in M}}

def run(m):
    name, f, edits = m[:3]
    test = m[3] if len(m) > 3 else TEST
    s = SRC[f]
    for old, new in edits:
        if s.count(old) != 1:
            return f"  [HARNESS] anchor count {s.count(old)} != 1: {name}", 1
        s = s.replace(old, new)
    d = tempfile.mkdtemp()
    try:
        for x in os.listdir(ROOT):
            if x.endswith((".py", ".txt", ".in")) or x == ".python-version": shutil.copy(os.path.join(ROOT, x), d)
        shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(d, "tests"), ignore=shutil.ignore_patterns("__pycache__"))
        open(os.path.join(d, f), "w", encoding="utf8").write(s)
        r = subprocess.run([sys.executable, test], cwd=d, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        failing = [l.strip()[6:] for l in out.splitlines() if l.strip().startswith("FAIL")]
        if r.returncode == 0:
            return f"  [SURVIVED] {name}", 1
        return f"  [KILLED] {name}  <- {(failing[0] if failing else 'crash/exit ' + str(r.returncode))[:100]}", 0
    finally:
        shutil.rmtree(d, ignore_errors=True)

ONLY = [x for x in os.environ.get("MUT_ONLY", "").split("|") if x]      # optional: re-run only mutants whose name contains one of these
if ONLY: M = [m for m in M if any(o in m[0] for o in ONLY)]
with ThreadPoolExecutor(max_workers=4) as ex:
    results = list(ex.map(run, M))
bad = sum(b for _, b in results)
for line, _ in results: print(line)
print(f"\n{len(M) - bad}/{len(M)} mutants killed"); sys.exit(1 if bad else 0)
