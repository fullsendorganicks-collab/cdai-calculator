"""Mutation check for the calculator abuse caps: every mutant must make tests/test_calculator_abuse_caps.py fail.
Run: python mut_calculator_abuse.py        Each row: (name, file, [(old, new), ...]); every `old` must occur exactly once in the file."""
import os, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
ROOT = os.path.dirname(os.path.abspath(__file__))
CL, RL, CIP = "calculator_leads.py", "rate_limits.py", "client_ip.py"
TEST = os.path.join("tests", "test_calculator_abuse_caps.py")
IPD = 'if not rate_limits.allow(limit, ip_bucket(real_client_ip(request)), namespace):'
M = [
 # --- caps removed
 ("send-code IP cap removed", CL, [('_gate: None = Depends(_ip_gate(rate_limits.SEND_CODE_PER_IP, "calc_code_ip"))', '_gate: None = None')]),
 ("verify IP cap removed", CL, [('_gate: None = Depends(_ip_gate(rate_limits.VERIFY_PER_IP, "calc_verify_ip"))', '_gate: None = None')]),
 ("PATCH IP cap removed", CL, [('_gate: None = Depends(_ip_gate(rate_limits.UPDATE_PER_IP, "calc_update_ip"))', '_gate: None = None')]),
 ("view IP cap removed", CL, [('if not rate_limits.allow(rate_limits.VIEW_PER_IP, ip_bucket(real_client_ip(request)), "calc_view_ip"):', 'if False:')]),
 ("send-code per-address cap removed", CL, [('if not rate_limits.allow(rate_limits.SEND_CODE_PER_EMAIL, _cap_key(email), "calc_code_email"):', 'if False:')]),
 ("verify per-address cap removed", CL, [('if not rate_limits.allow(rate_limits.VERIFY_PER_EMAIL, _cap_key(email), "calc_verify_email"):', 'if False:')]),
 ("results-email per-address cap removed", CL, [('if should_email and rate_limits.allow(\n                rate_limits.RESULTS_EMAIL_PER_ADDRESS,\n                _cap_key(str(row["email"]).strip().lower()), "calc_results_email"):', 'if should_email:')]),
 ("results-email cap keyed case-sensitively", CL, [('_cap_key(str(row["email"]).strip().lower()), "calc_results_email"', '_cap_key(str(row["email"])), "calc_results_email"')]),
 ("address not lower-cased before keying", CL, [('v = (raw or "").strip().lower()', 'v = (raw or "").strip()')]),
 ("view over cap still inserts", CL, [('"calc_view_ip"):\n        return {"status": "ok"}', '"calc_view_ip"):\n        pass')]),
 ("over-cap send-code still stores OTP and mails", CL, [('    if not rate_limits.allow(rate_limits.SEND_CODE_PER_EMAIL, _cap_key(email), "calc_code_email"):\n        raise _too_many()\n', '    _over = not rate_limits.allow(rate_limits.SEND_CODE_PER_EMAIL, _cap_key(email), "calc_code_email")\n'),
                                                         ('    sent = send_otp_email(name, email, code)\n    if not sent:', '    sent = send_otp_email(name, email, code)\n    if _over:\n        raise _too_many()\n    if not sent:')]),
 ("verify cap checked after the code lookup", CL, [('    if not rate_limits.allow(rate_limits.VERIFY_PER_EMAIL, _cap_key(email), "calc_verify_email"):\n        raise _too_many()\n', '    _over = not rate_limits.allow(rate_limits.VERIFY_PER_EMAIL, _cap_key(email), "calc_verify_email")\n'),
                                                   ('        otp = cur.fetchone()\n', '        otp = cur.fetchone()\n        if _over and not otp:\n            raise _too_many()\n')]),
 # --- review round 1 fixes
 ("IPv4-mapped IPv6 collapses into ::/64", CIP, [('        if a.ipv4_mapped is not None:', '        if False:')]),
 ("apostrophe addresses refused again", CL, [('[^@\\s<>\\",;()', '[^@\\s<>\\\'",;()')]),
 ("new-style TLD refused (letters only)", CL, [('\\.[A-Za-z][A-Za-z0-9-]{1,}")', '\\.[A-Za-z]{2,}")')]),
 ("send-code cap keyed on the exact address", CL, [('rate_limits.SEND_CODE_PER_EMAIL, _cap_key(email), "calc_code_email"', 'rate_limits.SEND_CODE_PER_EMAIL, email, "calc_code_email"')]),
 ("verify cap keyed on the exact address", CL, [('rate_limits.VERIFY_PER_EMAIL, _cap_key(email), "calc_verify_email"', 'rate_limits.VERIFY_PER_EMAIL, email, "calc_verify_email"')]),
 ("results cap keyed on the exact address", CL, [('_cap_key(str(row["email"]).strip().lower()), "calc_results_email"', 'str(row["email"]).strip().lower(), "calc_results_email"')]),
 ("+tag not dropped from the cap key", CL, [('    local = local.split("+", 1)[0]\n', '')]),
 ("Google dots not dropped from the cap key", CL, [('local, domain = local.replace(".", ""), "gmail.com"', 'local, domain = local, "gmail.com"')]),
 ("googlemail not read as gmail", CL, [('local, domain = local.replace(".", ""), "gmail.com"', 'local, domain = local.replace(".", ""), domain')]),
 ("dots dropped for every provider", CL, [('    if domain in _GOOGLE_DOMAINS:\n        local, domain = local.replace(".", ""), "gmail.com"', '    if True:\n        local, domain = local.replace(".", ""), ("gmail.com" if domain in _GOOGLE_DOMAINS else domain)')]),
 # --- limit values
 ("send-code IP limit loosened", RL, [('parse("5/hour")', 'parse("6/hour")')]),
 ("send-code address limit loosened", RL, [('parse("3/hour")', 'parse("4/hour")')]),
 ("send-code IP window widened", RL, [('parse("5/hour")', 'parse("5/day")')]),
 ("verify IP limit loosened", RL, [('parse("20 per 10 minutes")', 'parse("21 per 10 minutes")')]),
 ("verify address limit loosened", RL, [('parse("6 per 10 minutes")', 'parse("7 per 10 minutes")')]),
 ("verify address window shortened", RL, [('parse("6 per 10 minutes")', 'parse("6 per 1 minute")')]),
 ("view limit loosened", RL, [('parse("60/minute")', 'parse("61/minute")')]),
 ("PATCH limit loosened", RL, [('parse("120/minute")', 'parse("121/minute")')]),
 ("results-email limit loosened", RL, [('parse("3/day")', 'parse("4/day")')]),
 ("results-email window shortened", RL, [('parse("3/day")', 'parse("3/hour")')]),
 ("limiter fails open", RL, [('logger.error("[CALC] rate limiter error: %s", type(e).__name__)\n        return False', 'logger.error("[CALC] rate limiter error: %s", type(e).__name__)\n        return True')]),
 ("sweep never runs", RL, [('    _maybe_sweep()\n    try:', '    try:')]),
 ("sweep drops live keys", RL, [('continue                       # newest hit still inside its window: live', 'pass'), ('continue                   # a hit landed while we waited for the lock', 'pass')]),
 ("sweep keeps expired keys", RL, [('_store.events.pop(key, None)\n            dropped += 1', 'dropped += 1')]),
 ("sweep leaves locks behind", RL, [('        _store.locks.pop(key, None)\n    return dropped', '    return dropped')]),
 # --- client address
 ("IP key is the spoofable first hop", CL, [(IPD, 'if not rate_limits.allow(limit, (request.headers.get("x-forwarded-for") or "x").split(",")[0].strip(), namespace):')]),
 ("view IP key is the spoofable first hop", CL, [('rate_limits.VIEW_PER_IP, ip_bucket(real_client_ip(request)), "calc_view_ip"', 'rate_limits.VIEW_PER_IP, (request.headers.get("x-forwarded-for") or "x").split(",")[0].strip(), "calc_view_ip"')]),
 ("real_client_ip uses first hop", CIP, [('return hops[-1]', 'return hops[0]')]),
 ("real_client_ip ignores the header", CIP, [('return hops[-1]', 'return request.client.host if request.client else "unknown"')]),
 ("IPv6 not reduced to /64", CIP, [('return str(ipaddress.ip_network(f"{a}/64", strict=False))', 'return ip')]),
 ("IP key ignores the bucket", CL, [(IPD, 'if not rate_limits.allow(limit, real_client_ip(request), namespace):')]),
 # --- validation
 ("e-mail not validated", CL, [('if len(v) > EMAIL_MAX or not _EMAIL_RE.fullmatch(v):', 'if False:')]),
 ("e-mail length uncapped", CL, [('if len(v) > EMAIL_MAX or not _EMAIL_RE.fullmatch(v):', 'if not _EMAIL_RE.fullmatch(v):')]),
 ("e-mail allows whitespace", CL, [('[^@\\s<>\\",;()', '[^@<>\\",;()')]),
 ("e-mail allows control characters", CL, [('\\\\\\x00-\\x1f\\x7f]+@', '\\\\]+@')]),
 ("e-mail allows angle brackets", CL, [('[^@\\s<>\\",;()', '[^@\\s\\",;()')]),
 ("name/company length uncapped", CL, [('    if len(v) > max_len:', '    if False:')]),
 ("name cap loosened", CL, [('NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200,', 'NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 1000, 200,')]),
 ("company cap loosened", CL, [('NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200,', 'NAME_MAX, COMPANY_MAX, EMAIL_MAX, SESSION_ID_MAX, JSON_MAX_CHARS = 100, 2000,')]),
 ("control characters accepted in name/company", CL, [('    if _CONTROL_RE.search(v):', '    if False:')]),
 ("C1 controls not in the control class", CL, [('_CONTROL_RE = re.compile(r"[\\x00-\\x1f\\x7f-\\x9f]")', '_CONTROL_RE = re.compile(r"[\\x00-\\x1f\\x7f]")')]),
 ("JSON size uncapped", CL, [('    if size > JSON_MAX_CHARS:', '    if False:')]),
 ("JSON size cap loosened", CL, [('JSON_MAX_CHARS = 100, 200, 254, 100, 20000', 'JSON_MAX_CHARS = 100, 200, 254, 100, 2000000')] if False else [('SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200, 254, 100, 20000', 'SESSION_ID_MAX, JSON_MAX_CHARS = 100, 200, 254, 100, 2000000')]),
 ("session_id length uncapped", CL, [('if payload.session_id is not None and len(payload.session_id) > SESSION_ID_MAX:', 'if False:')]),
 ("lead id not validated as UUID", CL, [('    try:\n        uuid.UUID(lead_id)\n    except ValueError:\n        raise HTTPException(status_code=404, detail="Lead not found")\n', '')]),
 ("code shape not checked before the database", CL, [('    if not _CODE_RE.fullmatch(code):', '    if False:')]),
 ("PATCH JSON size not checked", CL, [('    _check_json_size(payload.inputs, "inputs")\n    _check_json_size(payload.results, "results")\n\n    conn = get_conn()\n    cur  = conn.cursor()\n    try:\n        cur.execute("""\n            UPDATE', '    conn = get_conn()\n    cur  = conn.cursor()\n    try:\n        cur.execute("""\n            UPDATE')]),
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
 ("results mail sent without setting email_sent", CL, [('"UPDATE calculator_leads SET email_sent = TRUE WHERE id = %s"', '"SELECT %s"')]),
]
SRC = {f: open(os.path.join(ROOT, f), encoding="utf8").read() for f in {m[1] for m in M}}

def run(m):
    name, f, edits = m
    s = SRC[f]
    for old, new in edits:
        if s.count(old) != 1:
            return f"  [HARNESS] anchor count {s.count(old)} != 1: {name}", 1
        s = s.replace(old, new)
    d = tempfile.mkdtemp()
    try:
        for x in os.listdir(ROOT):
            if x.endswith(".py"): shutil.copy(os.path.join(ROOT, x), d)
        shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(d, "tests"), ignore=shutil.ignore_patterns("__pycache__"))
        open(os.path.join(d, f), "w", encoding="utf8").write(s)
        r = subprocess.run([sys.executable, TEST], cwd=d, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        failing = [l.strip()[6:] for l in out.splitlines() if l.strip().startswith("FAIL")]
        if r.returncode == 0:
            return f"  [SURVIVED] {name}", 1
        return f"  [KILLED] {name}  <- {(failing[0] if failing else 'crash/exit ' + str(r.returncode))[:100]}", 0
    finally:
        shutil.rmtree(d, ignore_errors=True)

with ThreadPoolExecutor(max_workers=4) as ex:
    results = list(ex.map(run, M))
bad = sum(b for _, b in results)
for line, _ in results: print(line)
print(f"\n{len(M) - bad}/{len(M)} mutants killed"); sys.exit(1 if bad else 0)
