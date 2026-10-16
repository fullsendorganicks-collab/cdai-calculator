"""Abuse caps, validation, escaping and log hygiene for the calculator service (Oct 14 2026).

Throwaway local PostgreSQL with the three production tables; Resend replaced by a recorder (fake key, nothing leaves the machine); Beehiiv
unconfigured except where a test patches `requests.post`. Run from anywhere:  python tests/test_calculator_abuse_caps.py   (exit 1 on any FAIL)
Limits are per process, so every section starts with rate_limits.reset().
"""
import importlib.util, logging, os, re, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
import local_pg
local_pg.start()
os.environ["RESEND_API_KEY"] = "re_fake_test_key"
os.environ.pop("BEEHIIV_API_KEY", None); os.environ.pop("BEEHIIV_PUBLICATION_ID", None)

import psycopg2
import resend
from fastapi.testclient import TestClient
from fastapi import Request

SENT = []
def _record(params): SENT.append(params); return {"id": "x"}
resend.Emails.send = _record            # the ONLY path to a mailbox in the code under test; it records and returns

import calculator_leads as cl
import rate_limits as rl
import durable_limits as dl
import client_ip
import limits.storage.memory as lmem
from main import app

# Time travel for the in-memory limiter: limits reads time.time() in its storage module and rate_limits reads time in sweep/_maybe_sweep.
_real_time = time
class Clock:
    off = 0.0
    def time(self): return _real_time.time() + self.off
    def monotonic(self): return _real_time.monotonic() + self.off
    def __getattr__(self, name): return getattr(_real_time, name)
clock = Clock(); lmem.time = clock; rl.time = clock

http = TestClient(app)
count, fails = [0], []
def check(c, label):
    count[0] += 1; print(("  PASS  " if c else "  FAIL  ") + label)
    if not c: fails.append(label)

GENERIC = "Too many requests. Please try again later."
_n = [0]
def ip():
    _n[0] += 1; return f"10.{(_n[0] // 62500) % 250}.{(_n[0] // 250) % 250}.{_n[0] % 250 + 1}"
def H(addr=None): return {"X-Forwarded-For": addr or ip()}
def db(sql, args=()):
    c = psycopg2.connect(os.environ["DATABASE_URL"]); cur = c.cursor(); cur.execute(sql, args)
    out = cur.fetchall() if cur.description else None
    c.commit(); c.close(); return out
def n(table): return db(f"SELECT count(*) FROM {table}")[0][0]
def fill(cap, count, key=None):
    """Put `count` hits in the window in ONE statement. A hit-per-call loop of 300 takes seconds on a loaded machine, and the oldest hits then age out
    of a window that a 'N seconds before expiry' check expects them to still be in (found as a flaky failure in a repeat run)."""
    key = dl.GLOBAL_KEY if key is None else key
    dl.hit(cap, key)
    db("INSERT INTO calculator_rate_events (bucket, key) SELECT bucket, key FROM calculator_rate_events, generate_series(1, %s) WHERE bucket = %s",
       (count - 1, cap.bucket))
def reset():
    rl.reset(); SENT.clear(); dl._fail_until = 0.0
    db("DO $$ BEGIN IF to_regclass('calculator_rate_events') IS NOT NULL THEN DELETE FROM calculator_rate_events; END IF; END $$")
def events(bucket=None):
    return db("SELECT count(*) FROM calculator_rate_events" + (" WHERE bucket=%s" if bucket else ""), (bucket,) if bucket else ())[0][0]
def backdate(bucket, seconds):
    db("UPDATE calculator_rate_events SET at = at - make_interval(secs => %s) WHERE bucket=%s", (seconds, bucket))
def send_code(email="a@example.com", name="Ann", company="Acme", addr=None):
    return http.post("/calculator-send-code", json={"name": name, "company": company, "email": email}, headers=H(addr))
def verify(email="a@example.com", code="000000", addr=None, name="Ann", company="Acme", **kw):
    return http.post("/calculator-lead", json={"name": name, "company": company, "email": email, "code": code, **kw}, headers=H(addr))
def last_code(email): return db("SELECT code FROM calculator_otps WHERE email=%s ORDER BY created_at DESC LIMIT 1", (email.strip().lower(),))[0][0]
GOOD_R = {"reportedCpl": 10, "trueCpl": 15, "hiddenCosts": 500, "cmPct": 12, "cplGapPct": 50}; GOOD_I = {"adSpend": 1000, "leads": 100}
def patch(lead, addr=None, results=GOOD_R, inputs=GOOD_I):
    return http.patch(f"/calculator-lead/{lead}", json={"results": results, "inputs": inputs}, headers=H(addr))
def new_lead(email="l@example.com", name="Ann", company="Acme"):
    """Lead row inserted directly (so cap tests on PATCH do not depend on the OTP caps)."""
    r = db("INSERT INTO calculator_leads (name, company, email, inputs, results) VALUES (%s,%s,%s,'{}','{}') RETURNING id", (name, company, email))
    return str(r[0][0])
def email_sent_flag(lead): return db("SELECT email_sent FROM calculator_leads WHERE id=%s", (lead,))[0][0]
def seed_otp(email, code="123456"):
    db("INSERT INTO calculator_otps (email, name, company, code) VALUES (%s, 'A', 'B', %s)", (email, code))
def full_flow(email, **kw):
    assert send_code(email, **kw).status_code == 200
    r = verify(email, last_code(email), **kw); assert r.status_code == 200, r.text
    return r.json()["id"]

print("-- 0. the durable table creates itself on first use (same pattern as calculator_views)")
check(db("SELECT to_regclass('calculator_rate_events')")[0][0] is None, "table does not exist before the service first needs it")
reset(); r = send_code("first@example.com", addr="1.1.1.1")
check(r.status_code == 200 and db("SELECT to_regclass('calculator_rate_events')")[0][0] is not None, "first send-code creates calculator_rate_events")
check(sorted(x[0] for x in db("SELECT indexname FROM pg_indexes WHERE tablename='calculator_rate_events'")) == ["calculator_rate_events_at", "calculator_rate_events_bucket_key_at", "calculator_rate_events_pkey"], "indexes (bucket,key,at) and (at) exist")
check([x[0] for x in db("SELECT column_name FROM information_schema.columns WHERE table_name='calculator_rate_events' ORDER BY ordinal_position")] == ["id", "bucket", "key", "at"], "columns id, bucket, key, at")
dl._ready = False
check(dl.hit(dl.Cap("t", 1, 60), "k") is not None, "ensure_table is idempotent (second creation is harmless)")
reset()

print("-- 1. the normal flow is unchanged")
reset()
r = send_code("Flow@Example.com ")
check(r.status_code == 200 and r.json() == {"status": "sent"}, "send-code answers 200 {'status':'sent'}")
check(len(SENT) == 1 and SENT[0]["to"] == "flow@example.com" and SENT[0]["from"] == "directives@alloceraintelligence.com", "exactly one OTP mail, to the normalised address")
code = last_code("flow@example.com")
check(re.fullmatch(r"[0-9]{6}", code) is not None and code in SENT[0]["subject"], "OTP is 6 digits and is in the subject")
check(verify("flow@example.com", "999999" if code != "999999" else "000001").status_code == 400, "wrong code is 400")
r = verify("flow@example.com", code)
lead = r.json().get("id")
check(r.status_code == 200 and lead, "right code creates the lead and returns {id}")
check(db("SELECT email, name FROM calculator_leads WHERE id=%s", (lead,)) == [("flow@example.com", "Ann")], "lead row stored with normalised email")
check(db("SELECT verified FROM calculator_otps WHERE email='flow@example.com'") == [(True,)], "OTP marked verified")
check(verify("flow@example.com", code).status_code == 400, "a used code cannot be replayed")
SENT.clear()
check(patch(lead).status_code == 200 and len(SENT) == 1 and email_sent_flag(lead) is True, "PATCH sends exactly one results mail and sets email_sent")
patch(lead); patch(lead)
check(len(SENT) == 1, "further PATCHes send no second results mail")
check(cl.BEEHIIV_API_KEY is None, "Beehiiv unconfigured in this test (fire-and-forget path is a no-op)")

print("-- 2. send-code: 5 per hour per IP bucket")
reset(); a = "203.0.113.7"; rows0 = n("calculator_otps")
codes = [send_code(f"s{i}@example.com", addr=a).status_code for i in range(6)]
check(codes == [200] * 5 + [429], f"5th allowed, 6th refused (got {codes})")
check(n("calculator_otps") - rows0 == 5 and len(SENT) == 5, "the refused request wrote no OTP row and sent no mail")
r = send_code("s9@example.com", addr=a)
check(r.json() == {"detail": GENERIC}, "refusal body is the generic text")
check(send_code("s10@example.com", addr="203.0.113.8").status_code == 200, "another IP is unaffected")
reset()
spoof = [send_code(f"sp{i}@example.com", addr=f"1.1.1.{i}, 203.0.113.9").status_code for i in range(6)]
check(spoof == [200] * 5 + [429], f"forged first X-Forwarded-For hops give no fresh bucket (got {spoof})")
reset()
v6 = [send_code(f"v6{i}@example.com", addr=f"2001:db8:1:2::{i + 1}").status_code for i in range(6)]
check(v6 == [200] * 5 + [429], f"IPv6 addresses in one /64 share a bucket (got {v6})")
check(send_code("v6x@example.com", addr="2001:db8:1:3::1").status_code == 200, "a different /64 is a different bucket")

print("-- 3. send-code: 3 per hour per normalised address, independent of the IP cap")
reset(); rows0 = n("calculator_otps")
c1 = [send_code("Victim@Example.com", addr=ip()).status_code for _ in range(3)]
c2 = send_code(" victim@example.com ", addr=ip())
check(c1 == [200] * 3 and c2.status_code == 429 and c2.json() == {"detail": GENERIC}, f"3rd allowed, 4th (case/space variant) refused with the same generic text (got {c1},{c2.status_code})")
check(n("calculator_otps") - rows0 == 3 and len(SENT) == 3, "the refused request wrote no OTP row and sent no mail")
check(send_code("other@example.com", addr=ip()).status_code == 200, "a different address is unaffected")
b = "198.51.100.9"; reset()
check([send_code("same@example.com", addr=b).status_code for _ in range(4)] == [200, 200, 200, 429], "address cap trips on a single IP too")
check(send_code("fresh@example.com", addr=b).status_code == 200, "address cap does not block other addresses from that IP")
reset(); rows0 = n("calculator_otps")
for i in range(8): send_code(f"burn{i}@example.com", addr=ip())
check(n("calculator_otps") - rows0 == 8, "eight different addresses from eight IPs all pass (no shared global cap)")

print("-- 4. verify: 20 per 10 min per IP bucket, 6 per 10 min per address")
reset(); a = "203.0.113.20"
codes = [verify(f"w{i}@example.com", addr=a).status_code for i in range(21)]
check(codes == [400] * 20 + [429], f"20th attempt reaches the code check, 21st refused (got {codes[-3:]})")
check(verify("w99@example.com", addr=a).json() == {"detail": GENERIC}, "refusal body is the generic text")
reset(); spoof = [verify(f"x{i}@example.com", addr=f"9.9.9.{i}, 203.0.113.21").status_code for i in range(21)]
check(spoof[20] == 429, "forged first hops do not bypass the verify IP cap")
reset(); send_code("brute@example.com", addr=ip()); good = last_code("brute@example.com"); bad = "000000" if good != "000000" else "000001"
leads0 = n("calculator_leads")
codes = [verify("brute@example.com", bad, addr=ip()).status_code for _ in range(6)]
r7 = verify("brute@example.com", good, addr=ip())
check(codes == [400] * 6, "six wrong guesses reach the code check")
check(r7.status_code == 429 and r7.json() == {"detail": GENERIC}, "the 7th attempt is refused even with the CORRECT code")
check(n("calculator_leads") == leads0 and db("SELECT verified FROM calculator_otps WHERE email='brute@example.com'") == [(False,)], "refused attempt created no lead and did not consume the code")
check(verify("BRUTE@example.com ", good, addr=ip()).status_code == 429, "case/space variants share the same address bucket")
reset(); send_code("late@example.com", addr=ip()); good = last_code("late@example.com"); bad = "000000" if good != "000000" else "000001"
for _ in range(5): verify("late@example.com", bad, addr=ip())
check(verify("late@example.com", good, addr=ip()).status_code == 200, "the 6th attempt (the Nth allowed) with the right code succeeds")
reset(); send_code("t@example.com", addr=ip())
for _ in range(6): verify("t@example.com", "123456", addr=ip())
check(verify("u@example.com", addr=ip()).status_code == 400, "an exhausted address does not block another address")

print("-- 5. view: 60 per minute per IP bucket; session_id <= 100")
reset(); a = "203.0.113.30"; v0 = n("calculator_views")
res = [http.post("/calculator-view", json={"session_id": f"s{i}"}, headers=H(a)) for i in range(61)]
check(all(r.status_code == 200 and r.json() == {"status": "ok"} for r in res), "every ping answers {'status':'ok'}, including the refused one")
check(n("calculator_views") - v0 == 60, f"60 stored, the 61st stored nothing (stored {n('calculator_views') - v0})")
http.post("/calculator-view", json={"session_id": "other"}, headers=H("203.0.113.31"))
check(n("calculator_views") - v0 == 61, "another IP is unaffected")
reset(); v0 = n("calculator_views")
for i in range(61): http.post("/calculator-view", json={"session_id": "z"}, headers=H(f"5.5.5.{i}, 203.0.113.32"))
check(n("calculator_views") - v0 == 60, "forged first hops do not bypass the view cap")
reset(); v0 = n("calculator_views")
r = http.post("/calculator-view", json={"session_id": "s" * 101}, headers=H())
check(r.json() == {"status": "ok"} and n("calculator_views") == v0, "101-character session_id answers ok and is not stored")
http.post("/calculator-view", json={"session_id": "s" * 100}, headers=H())
http.post("/calculator-view", json={}, headers=H())
check(n("calculator_views") - v0 == 2, "100-character and absent session_id are stored (as before)")

print("-- 6. PATCH: 120 per minute per IP bucket, id must be a UUID")
reset(); lead = new_lead(); a = "198.51.100.5"; calls = [0]
real_conn = cl.get_conn
def counting_conn(): calls[0] += 1; return real_conn()
cl.get_conn = counting_conn
codes = [patch(lead, addr=a, inputs={"adSpend": 0, "k": i}).status_code for i in range(121)]
check(codes[119] == 200 and codes[120] == 429, f"120th allowed, 121st refused (got {codes[119]},{codes[120]})")
check(calls[0] == 120, f"the refused PATCH made no database call (calls {calls[0]})")
check(db("SELECT inputs->>'k' FROM calculator_leads WHERE id=%s", (lead,)) == [("119",)], "the refused PATCH wrote nothing")
check(http.patch(f"/calculator-lead/{lead}", json={}, headers=H(a)).json() == {"detail": GENERIC}, "refusal body is the generic text")
reset(); calls[0] = 0
r = http.patch("/calculator-lead/not-a-uuid", json={}, headers=H())
check(r.status_code == 404 and calls[0] == 0, "non-UUID lead id is 404 with no database call")
check(http.patch("/calculator-lead/' OR 1=1 --", json={}, headers=H()).status_code == 404 and calls[0] == 0, "SQL-looking id is 404 with no database call")
check(patch("00000000-0000-0000-0000-000000000000").status_code == 404 and calls[0] == 1, "unknown but well-formed id is 404")
reset()
for i in range(121): http.patch(f"/calculator-lead/{lead}", json={}, headers=H(f"6.6.6.{i}, 198.51.100.6"))
check(http.patch(f"/calculator-lead/{lead}", json={}, headers=H("7.7.7.7, 198.51.100.6")).status_code == 429, "forged first hops do not bypass the PATCH cap")
cl.get_conn = real_conn

print("-- 7. results email: at most 3 per address per day, consumed before sending")
reset()
leads = [new_lead("Same@Example.com" if i % 2 else "same@example.com") for i in range(4)]
codes = [patch(L, addr=ip()).status_code for L in leads]
check(codes == [200] * 4, "all four PATCHes answer 200 (the lead is still saved)")
check(len(SENT) == 3, f"only 3 results mails reach one address, any letter case (sent {len(SENT)})")
check([email_sent_flag(L) for L in leads] == [True, True, True, False], "the 4th lead keeps email_sent FALSE")
SENT.clear(); patch(leads[3], addr=ip())
check(len(SENT) == 0, "the capped lead sends nothing on a later PATCH either")
patch(leads[0], addr=ip()); check(len(SENT) == 0, "an already-sent lead never sends again")
L = new_lead("different@example.com"); patch(L, addr=ip())
check(len(SENT) == 1, "another address still gets its mail")
SENT.clear(); L = new_lead("nodata@example.com"); patch(L, addr=ip(), inputs={"adSpend": 0, "leads": 0})
patch(L, addr=ip(), inputs=GOOD_I)
check(len(SENT) == 1, "a PATCH without real data does not use up the address cap or send")

print("-- 8. validation: refused with no database write")
reset(); rows = lambda: (n("calculator_otps"), n("calculator_leads"))
r0 = rows(); sent0 = len(SENT)
bad_emails = ["not-an-email", "a@b.c", "x@example.com\r\nBcc: v@example.org", "a b@example.com", "<script>@example.com", "a@example.com>", "x" * 250 + "@example.com", "", "a\x00b@example.com", "a@@example.com"]
codes = [send_code(e, addr=ip()).status_code for e in bad_emails]
check(all(c == 400 for c in codes), f"malformed e-mails refused 400 by send-code (got {codes})")
check(all(verify(e, "123456", addr=ip()).status_code == 400 for e in bad_emails), "malformed e-mails refused 400 by verify")
check(rows() == r0 and len(SENT) == sent0, "no OTP row, lead row or mail from any of them")
check(send_code("ok@example.com", name="n" * 101, addr=ip()).status_code == 400, "name over 100 refused")
check(send_code("ok@example.com", company="c" * 201, addr=ip()).status_code == 400, "company over 200 refused")
check(send_code("ok@example.com", name="a\nb", addr=ip()).status_code == 400, "newline in name refused")
check(send_code("ok@example.com", company="a\rb", addr=ip()).status_code == 400, "carriage return in company refused")
check(send_code("ok@example.com", company="a\x07b", addr=ip()).status_code == 400, "control character in company refused")
check(send_code("ok@example.com", name="a\u0085b", addr=ip()).status_code == 400, "C1 control character in name refused")
check(rows() == r0, "none of those wrote a row")
check(send_code("e" * 242 + "@example.com", name="N" * 100, company="C" * 200, addr=ip()).status_code == 200, "values exactly at the limits accepted (254-char address)")
check(send_code("ok2@example.com", name="Zoë O'Brien", company="Müller & Söhne", addr=ip()).status_code == 200, "ordinary non-ASCII names accepted")
reset(); send_code("sz@example.com", addr=ip()); good = last_code("sz@example.com"); l0 = n("calculator_leads")
check(verify("sz@example.com", good, addr=ip(), inputs={"k": "v" * 20001}).status_code == 413, "oversize inputs refused 413 by verify")
check(verify("sz@example.com", good, addr=ip(), results={"k": "v" * 20001}).status_code == 413, "oversize results refused 413 by verify")
check(verify("sz@example.com", good, addr=ip(), name="n" * 101).status_code == 400, "overlong name refused by verify")
check(n("calculator_leads") == l0 and db("SELECT verified FROM calculator_otps WHERE email='sz@example.com'") == [(False,)], "none of those created a lead or used the code")
calls = [0]; cl.get_conn = counting_conn
r = verify("sz@example.com", "12345a", addr=ip())
check(r.status_code == 400 and calls[0] == 0, "a code that is not 6 ASCII digits is refused without a database call")
cl.get_conn = real_conn
check(verify("sz@example.com", good, addr=ip(), inputs={"k": "v" * 15000}).status_code == 200, "inputs under the cap accepted")
lead = new_lead(); before = db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,))
check(patch(lead, addr=ip(), inputs={"k": "v" * 20001}).status_code == 413 and patch(lead, addr=ip(), results={"k": "v" * 20001}).status_code == 413, "oversize PATCH inputs / results refused 413")
check(db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,)) == before, "the refused PATCH wrote nothing")
deep = '{"a":' * 5000 + "1" + "}" * 5000
quiet = TestClient(app, raise_server_exceptions=False)      # FastAPI's own 422 encoder can recurse on hostile nesting; that is framework behaviour, we only assert nothing is written
r = quiet.patch(f"/calculator-lead/{lead}", content='{"inputs":' + deep + "}", headers={**H(), "Content-Type": "application/json"})
check(r.status_code in (400, 413, 422, 500) and db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,)) == before, f"pathologically nested JSON writes nothing (status {r.status_code})")
r = http.post("/calculator-lead", json={"name": "x"}, headers=H()); check(r.status_code == 422, "missing fields are a 422 and write nothing")
reset(); a = "203.0.113.40"
for _ in range(20): http.post("/calculator-lead", json={"name": "x"}, headers=H(a))
check(verify("q@example.com", addr=a).status_code == 429, "malformed requests still consume the per-IP cap")

print("-- 9. OTP is generated with secrets, zero-padded")
reset(); src = open(os.path.join(ROOT, "calculator_leads.py")).read()
check("import random" not in src and "random." not in src.replace("gen_random_uuid", ""), "calculator_leads.py does not use the random module")
seen = []; real_rb = cl.secrets.randbelow
cl.secrets.randbelow = lambda m: (seen.append(m), 42)[1]
send_code("pad@example.com", addr=ip()); cl.secrets.randbelow = real_rb
check(seen == [10 ** 6] and last_code("pad@example.com") == "000042" and "000042" in SENT[-1]["subject"], "secrets.randbelow(10**6) is the source and the code is zero-padded to 6")

print("-- 10. escaping")
reset()
evil_name = '<script>alert(1)</script>"><img src=x onerror=1>'
send_code("esc@example.com", name=evil_name, addr=ip())
h = SENT[0]["html"]
check("<script>alert(1)</script>" not in h and "<img src=x" not in h and "&lt;script&gt;alert(1)&lt;/script&gt;" in h, "name is HTML-escaped in the OTP mail")
check("Hi Ann — enter" in (send_code("esc2@example.com", name="Ann", addr=ip()) and SENT[-1]["html"]), "an ordinary name renders as before")
SENT.clear()
cl.send_results_email("N", evil_name, "r@example.com", GOOD_R, GOOD_I)
h = SENT[0]["html"]
check("<script>alert(1)</script>" not in h and "<img src=x" not in h and "<h1>Your true CPL for &lt;script&gt;" in h, "company is HTML-escaped in the results mail body")
SENT.clear(); cl.send_results_email("N", "Line1\r\nBcc: x@y.z\nSubject: y", "r@example.com", GOOD_R, GOOD_I)
subj = SENT[0]["subject"]
check("\r" not in subj and "\n" not in subj, "CR/LF in a stored company cannot reach the subject")
SENT.clear(); cl.send_results_email("N", "C" * 5000, "r@example.com", GOOD_R, GOOD_I)
check(len(SENT[0]["subject"]) == 200, "subject capped at 200 characters")
SENT.clear(); cl.send_results_email("N", "Acme Roofing", "r@example.com", GOOD_R, GOOD_I)
check("<h1>Your true CPL for Acme Roofing</h1>" in SENT[0]["html"] and "Acme Roofing (" in SENT[0]["subject"], "an ordinary company renders unchanged")
check("Every two weeks" not in SENT[0]["html"] and "never shows. Free.</p>" in SENT[0]["html"], "the newsletter cadence phrase is gone")
SENT.clear(); full_flow("api-esc@example.com", company="Tom & Jerry <b>x</b>")
patch(db("SELECT id FROM calculator_leads WHERE email='api-esc@example.com'")[0][0], addr=ip())
check(len(SENT) == 2 and "Tom &amp; Jerry &lt;b&gt;x&lt;/b&gt;" in SENT[1]["html"] and "Tom & Jerry <b>x</b>" in SENT[1]["subject"], "end to end: body escaped, plain-text subject not HTML-escaped")

print("-- 11. emails byte-identical to origin/main except the one removed phrase")
spec = importlib.util.spec_from_file_location("orig_main", os.path.join(HERE, "fixtures", "calculator_leads_original_main.py"))
orig = importlib.util.module_from_spec(spec); spec.loader.exec_module(orig)
check("random.randint" in open(os.path.join(HERE, "fixtures", "calculator_leads_original_main.py")).read(), "fixture is the original (random-based) file")
gitcopy = subprocess.run(["git", "show", "2f57426e9363a19472e097d5b1200bdc5727c6ca:calculator_leads.py"], cwd=ROOT, capture_output=True, text=True)
if gitcopy.returncode == 0:
    check(gitcopy.stdout == open(os.path.join(HERE, "fixtures", "calculator_leads_original_main.py")).read(), "fixture equals `git show 2f57426:calculator_leads.py`")
else:
    print("  (git history for the base commit not available here; fixture used as is)")
PHRASE = "Every two weeks, free."   # the only edit: becomes "Free."
samples = [("Jane Smith", "Acme Roofing", "jane@example.com", GOOD_R, GOOD_I),
           ("Bob", "Delta Solar LLC", "bob@example.org", {"reportedCpl": 42.5, "trueCpl": 31.1, "hiddenCosts": 12345.6, "cmPct": -7.25, "cplGapPct": -26.8}, {"adSpend": 5000, "leads": 90})]
same = True; once = True
for nm, co, em, r_, i_ in samples:
    SENT.clear(); orig.send_results_email(nm, co, em, r_, i_); o = SENT[-1]
    SENT.clear(); cl.send_results_email(nm, co, em, r_, i_); new = SENT[-1]
    once &= o["html"].count(PHRASE) == 1
    same &= new == {**o, "html": o["html"].replace(PHRASE, "Free.")}
    SENT.clear(); orig.send_otp_email(nm, em, "123456"); o = SENT[-1]
    SENT.clear(); cl.send_otp_email(nm, em, "123456"); same &= (SENT[-1] == o)
check(once, "the original results mail contains the phrase exactly once")
check(same, "results mail == original with only that phrase removed; OTP mail == original, byte for byte, for ordinary samples")

print("-- 12. no e-mail address, name or company in any log line")
lines = []
class Cap(logging.Handler):
    def emit(self, rec): lines.append(rec.getMessage())
cap = Cap(); lg = logging.getLogger(); lg.addHandler(cap); lg.setLevel(logging.DEBUG)
reset()
E_, N_, C_ = "zqsecretperson@example.com", "Zq Secretname", "Zq SecretCo"
L = full_flow(E_, name=N_, company=C_); patch(L, addr=ip())
send_code(E_, name=N_, company=C_, addr=ip()); send_code("zqbad@@example.com", name=N_, company=C_, addr=ip())
def boom(params): raise RuntimeError(f"gateway rejected {params['to']} {E_}")
resend.Emails.send = boom
send_code("zqother@example.com", name=N_, company=C_, addr=ip())
L2 = new_lead("zqthird@example.com", N_, C_); patch(L2, addr=ip())
resend.Emails.send = _record
import requests
real_post = requests.post; posts = []
def bpost(url, **kw): posts.append(kw); raise ConnectionError(f"cannot reach beehiiv for {kw['json']['email']}")
requests.post = bpost; cl.BEEHIIV_API_KEY, cl.BEEHIIV_PUB_ID = "k", "p"
send_code("zqbee@example.com", name=N_, company=C_, addr=ip()); r = verify("zqbee@example.com", last_code("zqbee@example.com"), addr=ip(), name=N_, company=C_)
requests.post = lambda url, **kw: posts.append(kw)      # Beehiiv success path
send_code("zqbee2@example.com", name=N_, company=C_, addr=ip()); r2 = verify("zqbee2@example.com", last_code("zqbee2@example.com"), addr=ip(), name=N_, company=C_)
cl.BEEHIIV_API_KEY = cl.BEEHIIV_PUB_ID = None; requests.post = real_post
check(r2.status_code == 200 and len(posts) == 2, "Beehiiv success path also exercised under log capture")
check(r.status_code == 200 and len(posts) >= 1 and posts[0]["json"]["email"] == "zqbee@example.com", "Beehiiv failure is swallowed, still fire-and-forget (lead created, subscribe attempted once)")
lg.removeHandler(cap)
blob = "\n".join(lines).lower()
check(len(lines) >= 8, f"log lines were captured ({len(lines)})")
check("@" not in blob and "zq" not in blob and "secretco" not in blob and "example.com" not in blob, "no address, name or company in any captured log line")
print("   sample:", lines[:3])

print("-- 13. fails closed when the limiter itself breaks")
reset(); real_hit = rl._limiter.hit
def broken(*a, **k): raise RuntimeError("storage down")
rl._limiter.hit = broken
o0, l0, v0 = n("calculator_otps"), n("calculator_leads"), n("calculator_views"); SENT.clear()
check(send_code("fc@example.com", addr=ip()).status_code == 429, "send-code refused")
check(verify("fc@example.com", "123456", addr=ip()).status_code == 429, "verify refused")
lead = new_lead("fc2@example.com"); SENT.clear()
check(patch(lead, addr=ip()).status_code == 429 and not SENT, "PATCH refused, no mail")
http.post("/calculator-view", json={"session_id": "s"}, headers=H())
check((n("calculator_otps"), n("calculator_views")) == (o0, v0) and not SENT, "no OTP row, no view row, no mail")
rl._limiter.hit = real_hit

print("-- 14. limiter memory is bounded (limits 5.8.0 never drops a key by itself)")
reset(); from limits import parse
short = parse("5/second")
for i in range(10000): rl.allow(short, f"k{i}", "mem")
check(len(rl._store.events) >= 10000, "10,000 distinct keys are tracked while live")
time.sleep(1.3); old_iv = rl.SWEEP_INTERVAL_S; rl.SWEEP_INTERVAL_S = 0
rl.allow(short, "trigger", "mem"); rl.SWEEP_INTERVAL_S = old_iv
check(len(rl._store.events) <= 2 and len(rl._store.locks) <= 2, f"expired keys are removed after the window (left {len(rl._store.events)} events, {len(rl._store.locks)} locks)")
reset(); lim = parse("2/hour"); rl.allow(lim, "live", "mem"); rl.allow(lim, "live", "mem")
rl.SWEEP_INTERVAL_S = 0; rl.allow(lim, "other", "mem"); rl.SWEEP_INTERVAL_S = old_iv
check(rl.allow(lim, "live", "mem") is False, "a key still inside its window survives the sweep and still blocks")
reset()
for i in range(3): rl.allow(rl.SEND_CODE_PER_IP, "1.2.3.4", "calc_code_ip")
check(rl.sweep() == 0 and rl.allow(rl.SEND_CODE_PER_IP, "1.2.3.4", "calc_code_ip") is True and rl.allow(rl.SEND_CODE_PER_IP, "1.2.3.4", "calc_code_ip") is True and rl.allow(rl.SEND_CODE_PER_IP, "1.2.3.4", "calc_code_ip") is False, "sweep() on live keys drops nothing and does not disturb counts")

print("-- 15. client_ip")
class HDR(dict):
    def getlist(self, k): return [self[k]] if k in self else []
class R:
    def __init__(self, xff=None, host="10.0.0.1"):
        self.headers = HDR({"x-forwarded-for": xff} if xff else {})
        self.client = type("C", (), {"host": host})() if host else None
check(client_ip.real_client_ip(R("1.1.1.1, 2.2.2.2, 3.3.3.3")) == "3.3.3.3", "last X-Forwarded-For hop is the client")
check(client_ip.real_client_ip(R(None)) == "10.0.0.1" and client_ip.real_client_ip(R(None, None)) == "unknown", "falls back to the socket address, then 'unknown'")
check(client_ip.real_client_ip(R(" , ,")) == "10.0.0.1", "an empty header falls back")
check(client_ip.ip_bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == "2001:db8:1:2::/64" and client_ip.ip_bucket("1.2.3.4") == "1.2.3.4" and client_ip.ip_bucket("junk") == "junk", "IPv6 -> /64, IPv4 and junk unchanged")

print("-- 15b. review round 1 fixes: mapped IPv6, apostrophe and new-TLD addresses, one mailbox = one per-address bucket")
check(client_ip.ip_bucket("::ffff:1.2.3.4") == "1.2.3.4" and client_ip.ip_bucket("::ffff:5.6.7.8") != client_ip.ip_bucket("::ffff:1.2.3.4"), "an IPv4-mapped IPv6 address is bucketed as its IPv4 address (not one shared ::/64 bucket)")
check(client_ip.ip_bucket("2001:db8::1") == "2001:db8::/64", "a real IPv6 address still reduces to its /64")
ok_addrs = ["o'brien@example.com", "first.last+tag@example.co.uk", "u@example.xn--p1ai", "a_b-c@sub.example.org"]
bad_addrs = ["a b@example.com", "a@example", "a@example.1", "<a>@example.com", "a@@example.com", "a@exa mple.com", 'a"b@example.com', "a@example.c", "a,b@example.com", ("x" * 250) + "@example.com"]
check(all(bool(cl._EMAIL_RE.fullmatch(x)) for x in ok_addrs), "accepted: apostrophe, plus tag, long and punycode TLD, underscore/hyphen")
check(not any(cl._EMAIL_RE.fullmatch(x) and len(x) <= cl.EMAIL_MAX for x in bad_addrs), "still refused: spaces, no dot, numeric TLD, angle brackets, double @, quotes, commas, over 254")
check(send_code("o'brien@example.com", addr=ip()).status_code == 200, "an address with an apostrophe can request a code")
check(cl._cap_key("a.b+x@gmail.com") == "ab@gmail.com" and cl._cap_key("ab@googlemail.com") == "ab@gmail.com" and cl._cap_key("a.b+x@corp.com") == "a.b@corp.com" and cl._cap_key("plain@example.com") == "plain@example.com", "cap key: +tag dropped everywhere, dots dropped and googlemail=gmail for Google only")
reset()
variants = ["bomb@gmail.com", "b.omb@gmail.com", "bomb+one@gmail.com", "b.o.m.b+two@googlemail.com"]
codes = [send_code(v, addr=ip()).status_code for v in variants]
check(codes == [200, 200, 200, 429], f"four spellings of one Gmail mailbox share ONE 3-per-hour bucket (got {codes})")
reset()
codes = [send_code(v, addr=ip()).status_code for v in ["corp@example.com", "corp+a@example.com", "corp+b@example.com", "corp+c@example.com"]]
check(codes == [200, 200, 200, 429], f"+tags share one bucket at any provider (got {codes})")
check(send_code("co.rp@example.com", addr=ip()).status_code == 200, "dots are significant outside Google: co.rp@ is a different mailbox with its own bucket")
reset(); e1, e2 = "ver.ify@gmail.com", "verify+z@gmail.com"
send_code(e1, addr=ip()); good = last_code(e1)
res = [verify(e1 if i % 2 == 0 else e2, "000000", addr=ip()).status_code for i in range(7)]
check(res[:6] == [400] * 6 and res[6] == 429, f"verify attempts across spellings of one mailbox share the 6-per-10-min bucket (got {res})")
reset(); lead_a = new_lead("mail.box@gmail.com"); lead_b = new_lead("mailbox+r@gmail.com"); lead_c = new_lead("mailbox@gmail.com"); lead_d = new_lead("m.ailbox@gmail.com")
for L in (lead_a, lead_b, lead_c): patch(L, addr=ip())
before = len(SENT); patch(lead_d, addr=ip())
check(before == 3 and len(SENT) == 3 and email_sent_flag(lead_d) is False, "results emails: four leads for one Gmail mailbox share the 3-per-day cap (the 4th is saved but not mailed)")

print("-- 16. the configured limits (amount per window)")
table = {"SEND_CODE_PER_IP": (5, 3600), "VERIFY_PER_IP": (20, 600), "VIEW_PER_IP": (60, 60), "UPDATE_PER_IP": (120, 60)}
for k, (amt, win) in table.items():
    lim = getattr(rl, k); check((lim.amount, lim.get_expiry()) == (amt, win), f"in-memory {k} = {amt} per {win} s")
check(not any(hasattr(rl, k) for k in ("SEND_CODE_PER_EMAIL", "VERIFY_PER_EMAIL", "RESULTS_EMAIL_PER_ADDRESS")), "the in-memory per-mailbox limits are gone (no dead code)")
dtable = {"SEND_CODE_PER_MAILBOX": ("code_mailbox", 3, 3600), "SEND_CODE_GLOBAL": ("global_code", 100, 3600), "VERIFY_PER_MAILBOX": ("verify_mailbox", 6, 600), "VERIFY_GLOBAL": ("global_verify", 300, 3600), "RESULTS_PER_MAILBOX": ("results_mailbox", 3, 86400), "RESULTS_CLAIM": ("results_claim", 1, 3600)}
for k, v in dtable.items(): check(tuple(getattr(dl, k)) == v, f"durable {k} = {v[1]} per {v[2]} s")
check(dl.GLOBAL_KEY == "*" and dl.PURGE_AFTER == "2 days" and dl.PURGE_EVERY_CALLS == 100, "global key '*', purge after 2 days, every 100 calls")
check((cl.NAME_MAX, cl.COMPANY_MAX, cl.EMAIL_MAX, cl.SESSION_ID_MAX, cl.JSON_MAX_CHARS, cl.SUBJECT_MAX) == (100, 200, 254, 100, 20000, 200), "size limits are 100/200/254/100/20000/200")
check(cl.TOO_MANY_DETAIL == GENERIC, "the one refusal text")

print("-- 18. client address: ALL forwarded lines, the LAST hop, never the forgeable socket address when a header is present")
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.requests import Request as SReq
def req(lines, host="10.0.0.1"):
    hdrs = [(b"x-forwarded-for", l.encode()) for l in lines]
    return SReq({"type": "http", "headers": hdrs, "client": (host, 1234) if host else None, "method": "GET", "path": "/", "query_string": b""})
rip = client_ip.real_client_ip
check(rip(req(["1.2.3.4, 5.6.7.8"], "8.8.8.8")) == "5.6.7.8", "the socket address is NOT trusted: the last forwarded hop wins even over a public host")
check(rip(req(["1.1.1.1, 9.9.9.9"], "5.5.5.5")) == "9.9.9.9", "forged first entry ignored, last hop used")
check(rip(req(["9.9.9.9"])) == "9.9.9.9" and rip(req(["4.4.4.4"], None)) == "4.4.4.4", "single hop used")
check(rip(req(["1.1.1.1, 9.9.9.9", "6.6.6.6"])) == "6.6.6.6" and rip(req(["6.6.6.6", "1.1.1.1, 9.9.9.9"])) == "9.9.9.9", "two header lines are joined: the last hop of the LAST line decides, in both orders")
check(rip(req(["1.1.1.1", "", "7.7.7.7"])) == "7.7.7.7" and rip(req(["7.7.7.7", ""])) == "7.7.7.7", "empty lines are skipped")
check(rip(req(["6.6.6.6, 1.2.3.4:5555"])) == "1.2.3.4", "IPv4:port")
check(client_ip.ip_bucket(rip(req(["6.6.6.6, [2001:db8::1]:443"]))) == "2001:db8::/64", "[IPv6]:port")
check(rip(req(["6.6.6.6, [2001:db8::1]"])) == "2001:db8::1" and rip(req(["6.6.6.6, 2001:db8::1"])) == "2001:db8::1", "[IPv6] and bare IPv6")
check(rip(req(["6.6.6.6, fe80::1%eth0"])) == "fe80::1" and rip(req(["6.6.6.6, [fe80::1%25eth0]"])) == "fe80::1", "IPv6 zone id is stripped before parsing (also inside brackets)")
check(rip(req(["x, garbage"], "6.6.6.6")) == "unknown" and rip(req(["6.6.6.6, garbage"], "1.2.3.4")) == "unknown", "an unparseable last hop is 'unknown', NOT the forgeable first entry or the rewritten socket address")
check(rip(req(["6.6.6.6, [2001:db8::1"])) == "unknown", "an unterminated bracket is 'unknown'")
check(rip(req([""], "10.0.0.1")) == "10.0.0.1" and rip(req([" , ,"], "10.0.0.1")) == "10.0.0.1" and rip(req([], "10.0.0.1")) == "10.0.0.1", "empty or absent header: the socket address")
check(rip(req([], None)) == "unknown" and rip(req([""], None)) == "unknown", "no header and no socket address: 'unknown'")
check(client_ip.ip_bucket("fe80::1%eth0") == "fe80::/64" and client_ip.ip_bucket("::ffff:1.2.3.4") == "1.2.3.4", "ip_bucket handles a zone id and mapped addresses")
for label, wrapped, cli in (("trusted_hosts='*'", ProxyHeadersMiddleware(app, trusted_hosts="*"), ("203.0.113.9", 4000)),
                            ("trusted_hosts='127.0.0.1'", ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1"), ("127.0.0.1", 4000))):
    c1 = TestClient(wrapped, client=cli)
    reset()
    codes = [c1.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"ph{i}@example.com"}, headers={"X-Forwarded-For": f"1.1.1.{i}, 9.9.9.9"}).status_code for i in range(6)]
    check(codes == [200] * 5 + [429], f"{label}: rotating the forged FIRST entry cannot defeat the per-IP cap (got {codes})")
    check(c1.post("/calculator-send-code", json={"name": "A", "company": "B", "email": "ph99@example.com"}, headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429, f"{label}: the cap is counting 9.9.9.9")
    check(c1.post("/calculator-send-code", json={"name": "A", "company": "B", "email": "ph98@example.com"}, headers={"X-Forwarded-For": "1.1.1.1, 7.7.7.7"}).status_code == 200, f"{label}: 1.1.1.1 was never the counted address")
    reset()
    codes = [c1.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"pl{i}@example.com"}, headers=[("X-Forwarded-For", f"5.5.5.{i}"), ("X-Forwarded-For", "8.8.8.8")]).status_code for i in range(6)]
    check(codes == [200] * 5 + [429], f"{label}: a second forged header line cannot defeat the cap (got {codes})")
    reset(); codes = [c1.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"pm{i}@example.com"}, headers=[("X-Forwarded-For", "8.8.4.4"), ("X-Forwarded-For", f"5.5.5.{i}")]).status_code for i in range(6)]
    check(codes == [200] * 6, f"{label}: with two lines the LAST line decides, in the other order too (got {codes})")
    reset(); v0 = n("calculator_views")
    for i in range(61): c1.post("/calculator-view", json={"session_id": "s"}, headers={"X-Forwarded-For": f"2.2.2.{i}, 6.6.6.6"})
    check(n("calculator_views") - v0 == 60, f"{label}: view cap counts the last hop, not the rotating first entry")
    reset(); codes = [c1.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"pg{i}@example.com"}, headers={"X-Forwarded-For": f"1.1.1.{i}, garbage"}).status_code for i in range(6)]
    check(codes == [200] * 5 + [429], f"{label}: unparseable last hops all share the one 'unknown' bucket, whatever the forged first entry (got {codes})")
seen_client = []
async def spy(scope, receive, send): seen_client.append(scope["client"][0]); await app(scope, receive, send)
TestClient(ProxyHeadersMiddleware(spy, trusted_hosts="*"), client=("203.0.113.9", 4000)).get("/health", headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
check(seen_client == ["1.1.1.1"], "premise confirmed: uvicorn's middleware rewrites scope client to the FIRST (forgeable) entry, so client.host cannot be trusted")
reset()
class Cap2(logging.Handler):
    def __init__(self): super().__init__(); self.t = []
    def emit(self, rec): self.t.append(rec.getMessage())
c2 = Cap2(); logging.getLogger("cdai.calculator_leads").addHandler(c2)
client_ip._warned = False
rip(req(["8.8.8.8"])); n_pub = len(c2.t)
rip(req(["10.1.1.1"])); rip(req(["10.2.2.2"])); rip(req([], None)); rip(req(["garbage"]))
warn = [t for t in c2.t if "rate-limit bucket" in t]
check(n_pub == 0 and len(warn) == 1, f"a public caller logs nothing; a non-public/unknown one logs ONE warning per process (got {len(warn)})")
check(warn and not re.search(r"\d+\.\d+\.\d+\.\d+", warn[0]), "the warning names no address")
client_ip._warned = False; c2.t.clear(); rip(req(["x, garbage"])); check(len([t for t in c2.t if "rate-limit bucket" in t]) == 1 and "'unknown'" in c2.t[-1], "'unknown' is reported as such")
logging.getLogger("cdai.calculator_leads").removeHandler(c2)

print("-- 19. durable store: contract")
reset()
h1 = dl.allow("t_b", "k1", 2, 60); h2 = dl.allow("t_b", "k1", 2, 60); h3 = dl.allow("t_b", "k1", 2, 60)
check(isinstance(h1, int) and isinstance(h2, int) and h1 != h2 and h3 is None, "2 allowed with row ids, the 3rd returns None")
check(dl.allow("t_b", "k2", 2, 60) is not None and dl.allow("t_c", "k1", 2, 60) is not None, "other keys and other buckets are independent")
check(events("t_b") == 3, "a refused hit is not recorded")
check(dl.refund(h1) is True and events("t_b") == 2 and dl.allow("t_b", "k1", 2, 60) is not None, "refund deletes that hit and frees the slot")
check(dl.refund(None) is False, "refund(None) is a no-op")
check(not any("@" in (x[0] or "") for x in db("SELECT key FROM calculator_rate_events")) , "keys are stored hashed, never as an address")
reset(); full_flow("hashchk@example.com")
check(events() >= 2 and not any("hashchk" in x[0] for x in db("SELECT key FROM calculator_rate_events")), "real traffic stores no address in calculator_rate_events")

print("-- 20. durable store fails closed")
reset(); real_connect = dl.connect
def down(): raise psycopg2.OperationalError("db down for hashchk@example.com")
dl.connect = down
check(dl.allow("x", "y", 5, 60) is None, "database error: allow() returns None (over cap)")
o0 = n("calculator_otps"); SENT.clear()
r = send_code("fail1@example.com", addr=ip())
check(r.status_code == 429 and n("calculator_otps") == o0 and not SENT, "send-code: 429, no OTP row, no mail")
check(verify("fail1@example.com", "123456", addr=ip()).status_code == 429, "verify: 429 (the guess is not even looked up)")
dl.connect = real_connect
lead = new_lead("fail2@example.com"); SENT.clear(); dl.connect = down
check(patch(lead, addr=ip()).status_code == 200 and not SENT and email_sent_flag(lead) is False, "results mail: no mail, lead saved, claim released")
dl.connect = real_connect
patch(lead, addr=ip()); check(len(SENT) == 1 and email_sent_flag(lead) is True, "after the database is back the same lead is mailed")
dl._ready = False; dl.connect = down; check(dl.hit(dl.SEND_CODE_GLOBAL, "*") is None, "table creation failure is also closed"); dl.connect = real_connect
reset()

print("-- 21. a failed send gives the allowance back")
reset(); o0 = n("calculator_otps")
def failing_send(params): raise RuntimeError("resend down")
resend.Emails.send = failing_send
r = send_code("refund@example.com", addr=ip())
check(r.status_code == 500 and n("calculator_otps") == o0, "failed OTP mail: 500 and the OTP row is deleted")
check(events("code_mailbox") == 0 and events("global_code") == 0, "both the mailbox and the global hit are refunded")
resend.Emails.send = _record
check([send_code("refund@example.com", addr=ip()).status_code for _ in range(3)] == [200, 200, 200], "the visitor still has all 3 attempts afterwards")
reset(); lead = new_lead("refund2@example.com"); attempts = []
def accept_then_raise(params): attempts.append(params); raise TimeoutError("response lost after Resend accepted the mail")
resend.Emails.send = accept_then_raise
for _ in range(30): patch(lead, addr=ip())
check(len(attempts) == 1, f"a send call that raised is AMBIGUOUS: exactly ONE attempt across 30 rapid saves (got {len(attempts)})")
check(events("results_mailbox") == 1 and events("results_claim") == 1 and email_sent_flag(lead) is False, "the mailbox hit and the claim are kept, email_sent stays FALSE")
backdate("results_claim", 3601); patch(lead, addr=ip())
check(len(attempts) == 2 and events("results_mailbox") == 2, "after the claim expires the next save may try again (the mailbox cap still counts the first attempt)")
reset(); lead = new_lead("refund3@example.com"); resend.Emails.send = _record; SENT.clear()
real_key = os.environ.pop("RESEND_API_KEY"); patch(lead, addr=ip()); os.environ["RESEND_API_KEY"] = real_key
check(not SENT and events("results_mailbox") == 0 and events("results_claim") == 0 and email_sent_flag(lead) is False, "no API key configured is a DEFINITE pre-send failure: both hits refunded")
patch(lead, addr=ip()); check(len(SENT) == 1 and email_sent_flag(lead) is True, "and the next save delivers it")
reset()
fill(dl.SEND_CODE_GLOBAL, 100)
o0 = n("calculator_otps"); SENT.clear()
r = send_code("pausechk@example.com", addr=ip())
check(r.status_code == 429 and r.json() == {"detail": GENERIC} and n("calculator_otps") == o0 and not SENT, "global ceiling reached: 429, generic text, no OTP row, no mail")
check(events("code_mailbox") == 0, "the refused visitor's per-mailbox hit was refunded")
reset()

print("-- 22. time travel: N allowed, N+1 refused, allowed again after the window (in-memory caps)")
def sc(i, addr): return send_code(f"tt{i}@example.com", addr=addr).status_code
reset(); A = "21.0.0.1"
check([sc(i, A) for i in range(6)] == [200] * 5 + [429], "send-code per IP: 5 allowed, 6th refused")
clock.off += 3570; check(sc(7, A) == 429, "send-code per IP: still refused 30 s before the hour is up")   # 30 s margin: the six requests above can take seconds on a loaded machine (flaky at 1 s)
clock.off += 40; check(sc(8, A) == 200, "send-code per IP: allowed again after the hour")
reset(); A = "21.0.0.2"
check([verify(f"vt{i}@example.com", addr=A).status_code for i in range(21)] == [400] * 20 + [429], "verify per IP: 20 allowed, 21st refused")
clock.off += 570; check(verify("vt99@example.com", addr=A).status_code == 429, "verify per IP: refused 30 s inside 10 minutes")   # 30 s margin, see above
clock.off += 40; check(verify("vt99@example.com", addr=A).status_code == 400, "verify per IP: allowed again after 10 minutes")
reset(); A = "21.0.0.3"; v0 = n("calculator_views")
for i in range(61): http.post("/calculator-view", json={"session_id": "t"}, headers=H(A))
check(n("calculator_views") - v0 == 60, "view per IP: 60 stored, 61st not")
clock.off += 61; http.post("/calculator-view", json={"session_id": "t"}, headers=H(A))
check(n("calculator_views") - v0 == 61, "view per IP: stored again after a minute")
reset(); A = "21.0.0.4"; lead = new_lead("ttp@example.com")
check([patch(lead, addr=A, inputs={"adSpend": 0}).status_code for _ in range(121)][-2:] == [200, 429], "PATCH per IP: 120th allowed, 121st refused")
clock.off += 61; check(patch(lead, addr=A, inputs={"adSpend": 0}).status_code == 200, "PATCH per IP: allowed again after a minute")
reset()

print("-- 23. time travel: durable caps (rows back-dated in SQL)")
reset(); mb = "dur@example.com"
check([send_code(mb, addr=ip()).status_code for _ in range(4)] == [200, 200, 200, 429], "send-code per mailbox: 3 allowed, 4th refused")
backdate("code_mailbox", 3595); check(send_code(mb, addr=ip()).status_code == 429, "send-code per mailbox: still refused inside the hour")
backdate("code_mailbox", 10); check(send_code(mb, addr=ip()).status_code == 200, "send-code per mailbox: allowed again after the hour")
reset()
fill(dl.SEND_CODE_GLOBAL, 100)
backdate("global_code", 3595); check(send_code("g1@example.com", addr=ip()).status_code == 429, "global: still refused inside the hour")
backdate("global_code", 10); check(send_code("g1@example.com", addr=ip()).status_code == 200, "global: allowed again after the hour")
reset()
fill(dl.SEND_CODE_GLOBAL, 99)
check(send_code("g2@example.com", addr=ip()).status_code == 200 and send_code("g3@example.com", addr=ip()).status_code == 429, "global: the 100th request is allowed, the 101st refused")
reset(); send_code("vm@example.com", addr=ip())
check([verify("vm@example.com", "000000", addr=ip()).status_code for _ in range(7)][-2:] == [400, 429], "verify per mailbox: 6th reaches the lookup, 7th refused")
backdate("verify_mailbox", 595); check(verify("vm@example.com", "000000", addr=ip()).status_code == 429, "verify per mailbox: refused inside 10 minutes")
backdate("verify_mailbox", 10); check(verify("vm@example.com", "000000", addr=ip()).status_code == 400, "verify per mailbox: allowed again after 10 minutes")
reset(); SENT.clear(); ls = [new_lead("day@example.com") for _ in range(4)]
for L in ls: patch(L, addr=ip())
check(len(SENT) == 3 and email_sent_flag(ls[3]) is False, "results per mailbox: 3 mailed, 4th not")
backdate("results_mailbox", 86000); patch(ls[3], addr=ip()); check(len(SENT) == 3, "results per mailbox: still refused inside the day")
backdate("results_mailbox", 500); patch(ls[3], addr=ip()); check(len(SENT) == 4 and email_sent_flag(ls[3]) is True, "results per mailbox: the 4th lead is mailed once the day has passed")
reset()
for age, label in ((3600, "1h"), (86400, "1d"), (172000, "1.99d"), (173000, "2.002d"), (300000, "3.5d")):
    db("INSERT INTO calculator_rate_events (bucket, key, at) VALUES ('purge', %s, now() - make_interval(secs => %s))", (label, age))
check(dl.purge() == 2 and sorted(x[0] for x in db("SELECT key FROM calculator_rate_events WHERE bucket='purge'")) == ["1.99d", "1d", "1h"], "purge removes only rows older than 2 days")
db("INSERT INTO calculator_rate_events (bucket, key, at) VALUES ('purge', 'old', now() - interval '5 days')")
dl._calls = dl.PURGE_EVERY_CALLS - 1; dl.allow("p2", "k", 5, 60)
if dl._purge_thread: dl._purge_thread.join(30)
check(db("SELECT count(*) FROM calculator_rate_events WHERE key='old'")[0][0] == 0 and events("purge") == 3, "the 100th call purges opportunistically and keeps recent rows")
dl._calls = 0; db("INSERT INTO calculator_rate_events (bucket, key, at) VALUES ('purge', 'old2', now() - interval '5 days')"); dl.allow("p2", "k", 5, 60)
check(db("SELECT count(*) FROM calculator_rate_events WHERE key='old2'")[0][0] == 1, "ordinary calls do not purge")
reset()

print("-- 24. concurrency")
class SlowCursor:
    """Cursor proxy that pauses after the durable limiter's COUNT, so a missing lock would let every concurrent caller see an empty count."""
    def __init__(self, cur): self._c = cur
    def execute(self, sql, *a):
        r = self._c.execute(sql, *a)
        if sql.lstrip().startswith("SELECT count(*) FROM calculator_rate_events"): time.sleep(0.03)
        return r
    def __getattr__(self, n): return getattr(self._c, n)
class SlowConn:
    def __init__(self, c): self._conn = c
    def cursor(self, *a, **k): return SlowCursor(self._conn.cursor(*a, **k))
    def __getattr__(self, n): return getattr(self._conn, n)
_real_dl_connect = dl.connect
def slow_on(): dl.connect = lambda: SlowConn(_real_dl_connect())
def slow_off(): dl.connect = _real_dl_connect
def burst(n_threads, fn):
    """N simultaneous requests. The limiter's lock timeout is widened for the duration: these tests prove MUTUAL EXCLUSION (exact counts), not speed. With the
    production 2 s a loaded machine refuses the tail of a 330-wide queue (fail closed, by design), which would make an exact count depend on the machine."""
    barrier = threading.Barrier(n_threads); out = [None] * n_threads
    def run(i):
        c = TestClient(app); barrier.wait(); out[i] = fn(c, i)
    saved = dl.LOCK_TIMEOUT_S, dl.STATEMENT_TIMEOUT_S
    dl.LOCK_TIMEOUT_S, dl.STATEMENT_TIMEOUT_S = 120, 120
    try:
        ts = [threading.Thread(target=run, args=(i,)) for i in range(n_threads)]
        [t.start() for t in ts]; [t.join(180) for t in ts]
    finally:
        dl.LOCK_TIMEOUT_S, dl.STATEMENT_TIMEOUT_S = saved
    return out
def hdr(i): return {"X-Forwarded-For": f"31.{i // 250}.{i % 250}.7"}
reset(); o0 = n("calculator_otps"); slow_on()
res = burst(20, lambda c, i: c.post("/calculator-send-code", json={"name": "A", "company": "B", "email": "burst@example.com"}, headers=hdr(i)).status_code)
check(sorted(res).count(200) == 3 and res.count(429) == 17, f"20 concurrent send-codes for one mailbox: exactly 3 allowed (got {res.count(200)} / {res.count(429)})")
slow_off()
check(n("calculator_otps") - o0 == 3 and len(SENT) == 3 and events("code_mailbox") == 3, "exactly 3 OTP rows, 3 mails, 3 recorded hits")
reset(); send_code("vburst@example.com", addr=ip()); good = last_code("vburst@example.com"); wrong = "000000" if good != "000000" else "000001"; slow_on()
res = burst(20, lambda c, i: c.post("/calculator-lead", json={"name": "A", "company": "B", "email": "vburst@example.com", "code": wrong}, headers=hdr(i)).status_code)
slow_off()
check(res.count(400) == 6 and res.count(429) == 14, f"20 concurrent verify attempts: exactly 6 reach the code lookup (got {res.count(400)} / {res.count(429)})")
reset(); o0 = n("calculator_otps")      # no artificial pause here: lock_timeout (2 s) is a real bound, 100 serialised holds must fit in it
res = burst(120, lambda c, i: c.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"gb{i}@example.com"}, headers=hdr(i)).status_code)
check(res.count(200) == 100 and res.count(429) == 20, f"120 concurrent requests: exactly 100 allowed by the global ceiling (got {res.count(200)} / {res.count(429)})")
check(n("calculator_otps") - o0 == 100 and len(SENT) == 100 and events("global_code") == 100, "exactly 100 OTP rows, mails and recorded hits")
reset(); lead = new_lead("race@example.com")
def slow(params): time.sleep(0.3); SENT.append(params); return {"id": "x"}
resend.Emails.send = slow; slow_on()
res = burst(8, lambda c, i: c.patch(f"/calculator-lead/{lead}", json={"results": GOOD_R, "inputs": GOOD_I}, headers=hdr(i)).status_code)
resend.Emails.send = _record; slow_off()
check(res == [200] * 8 and len(SENT) == 1 and email_sent_flag(lead) is True and events("results_mailbox") == 1, "8 truly concurrent saves of one lead: exactly one mail, one recorded hit")
reset(); ls = [new_lead("rburst@example.com") for _ in range(10)]; slow_on()
res = burst(10, lambda c, i: c.patch(f"/calculator-lead/{ls[i]}", json={"results": GOOD_R, "inputs": GOOD_I}, headers=hdr(i)).status_code)
slow_off()
check(len(SENT) == 3 and sum(1 for L in ls if email_sent_flag(L)) == 3, "10 concurrent leads of one mailbox: exactly 3 mails, the rest released")
reset()

print("-- 25. PATCH with junk numbers never fails")
junk = ['"100"', "null", "true", "NaN", "Infinity", "-Infinity", str(10 ** 400), "[1]", '{"a":1}', "-5", "0", "1e999"]
lead = new_lead("junk@example.com"); SENT.clear(); bad = []
for field in ("adSpend", "leads", "trueCpl"):
    for jv in junk:
        i_ = {"adSpend": 1000, "leads": 10}; r_ = {"trueCpl": 5}
        (r_ if field == "trueCpl" else i_)[field] = "@@"
        body = json_body = '{"inputs":%s,"results":%s}' % (str(i_).replace("'", '"'), str(r_).replace("'", '"'))
        body = body.replace('"@@"', jv)
        resp = http.patch(f"/calculator-lead/{lead}", content=body, headers={**H(), "Content-Type": "application/json"})
        if resp.status_code != 200 or SENT: bad.append((field, jv, resp.status_code, len(SENT)))
check(not bad, f"36 junk-number combinations: always 200 and never a mail (failures: {bad[:3]})")
check(email_sent_flag(lead) is False, "and no claim is left behind")
check(db("SELECT inputs::text FROM calculator_leads WHERE id=%s", (lead,)) != [(None,)], "NaN/Infinity were stored as null, not rejected by jsonb")
check([cl._num(x) for x in ("1", None, True, False, float("nan"), float("inf"), 10 ** 400, [1], {}, 7, 2.5, -1e308)] == [0, 0, 0, 0, 0, 0, 0, 0, 0, 7, 2.5, -1e308], "_num: finite numbers pass, everything else is 0")
for fld in ("reportedCpl", "trueCpl", "hiddenCosts", "cmPct", "cplGapPct"):
    for junk_v in ("x", None, [1], {"a": 1}):
        SENT.clear(); good_r = dict(GOOD_R); good_r[fld] = junk_v
        got = cl.send_results_email("N", "Co", "r@example.com", good_r, GOOD_I)
        if got is not True or len(SENT) != 1: bad.append((fld, junk_v))
check(not bad, f"send_results_email: junk in any single numeric field still produces the mail (failures: {bad[:3]})")
SENT.clear()
ok = cl.send_results_email("N", "Co", "r@example.com", {"reportedCpl": "x", "trueCpl": None, "hiddenCosts": True, "cmPct": float("nan"), "cplGapPct": 10 ** 400}, {})
check(ok is True and len(SENT) == 1, "send_results_email coerces every number it formats (junk results still produce a mail)")
SENT.clear(); r = http.patch(f"/calculator-lead/{lead}", json={"inputs": {"adSpend": 1e308, "leads": 1e308}, "results": {"trueCpl": 1e308, "reportedCpl": -1e308, "hiddenCosts": 1e308}}, headers=H())
check(r.status_code == 200 and len(SENT) == 1, "huge finite numbers are meaningful data and still format without an error")

print("-- 26. request body cap before parsing")
reset(); lead = new_lead("body@example.com"); before = db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,))
ORIGIN = {"Origin": "https://alloceraintelligence.com"}
big = b'{"inputs":{"k":"' + b"v" * 70000 + b'"}}'
r = http.patch(f"/calculator-lead/{lead}", content=big, headers={**H(), **ORIGIN, "Content-Type": "application/json"})
check(r.status_code == 413 and r.json() == {"detail": "Request body too large"}, "declared Content-Length over 65536: 413")
check(r.headers.get("access-control-allow-origin") == "https://alloceraintelligence.com", "the 413 carries the CORS header")
def chunks(total, size=8192):
    sent_ = 0
    while sent_ < total:
        piece = min(size, total - sent_); sent_ += piece; yield b" " * piece
r = http.patch(f"/calculator-lead/{lead}", content=chunks(200000), headers={**H(), **ORIGIN, "Content-Type": "application/json"})
check(r.status_code == 413 and r.headers.get("access-control-allow-origin") == "https://alloceraintelligence.com", "streamed body with no Content-Length over the limit: 413 with CORS header")
check(db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,)) == before, "neither wrote anything")
def padded(total):
    core = b'{"inputs":{"adSpend":0}}'; return core + b" " * (total - len(core))
r = http.patch(f"/calculator-lead/{lead}", content=padded(65536), headers={**H(), "Content-Type": "application/json"})
check(r.status_code == 200, f"a body of exactly 65536 bytes passes (got {r.status_code})")
r = http.patch(f"/calculator-lead/{lead}", content=padded(65537), headers={**H(), "Content-Type": "application/json"})
check(r.status_code == 413, "65537 bytes is refused")
r = http.patch(f"/calculator-lead/{lead}", content=(chunks(65536)), headers={**H(), "Content-Type": "application/json"})
check(r.status_code != 413, "a streamed body of exactly 65536 bytes is not cut off")
r = http.patch(f"/calculator-lead/{lead}", content=chunks(65537), headers={**H(), "Content-Type": "application/json"})
check(r.status_code == 413, "a streamed body of 65537 bytes is cut off")
r = http.post("/calculator-send-code", content=b"x" * 70000, headers={**H(), "Content-Type": "application/json"})
check(r.status_code == 413, "the cap covers every endpoint")
check(http.get("/health").json() == {"status": "ok", "service": "cdai-calculator"}, "GET /health is untouched")
deep = '{"a":' * 9000 + "1" + "}" * 9000
before = db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,)); t0 = time.time(); quiet = TestClient(app, raise_server_exceptions=False)
r = quiet.patch(f"/calculator-lead/{lead}", content='{"inputs":' + deep + "}", headers={**H(), "Content-Type": "application/json"})
check(len(deep) < 65536 and 400 <= r.status_code < 600 and time.time() - t0 < 10 and db("SELECT inputs, results FROM calculator_leads WHERE id=%s", (lead,)) == before, f"deep nesting under the limit answers {r.status_code} quickly and writes nothing")
reset()

print("-- 27. body cap middleware, driven directly (no HTTP stack in between)")
import asyncio
from main import BodyLimitMiddleware, MAX_BODY_BYTES
check(MAX_BODY_BYTES == 65536, "limit is 65536 bytes")
def drive(app_fn, headers, chunks, scope_type="http"):
    """Run the middleware around app_fn; returns (status or None, bytes the app received, app_called)."""
    state = {"called": False, "got": 0, "status": None, "pulled": 0}
    async def inner(scope, receive, send):
        state["called"] = True
        await app_fn(scope, receive, send, state)
    mw = BodyLimitMiddleware(inner)
    queue = list(chunks)
    async def receive():
        state["pulled"] += 1
        if queue: return {"type": "http.request", "body": queue.pop(0), "more_body": bool(queue)}
        await asyncio.sleep(0.01); return {"type": "http.disconnect"}
    async def send(msg):
        if msg["type"] == "http.response.start": state["status"] = msg["status"]
    asyncio.run(mw({"type": scope_type, "headers": headers}, receive, send))
    return state
async def reader_app(scope, receive, send, st):
    while True:
        m = await receive()
        if m["type"] == "http.request":
            st["got"] += len(m["body"])
            if not m.get("more_body"): break
        else: break
    await send({"type": "http.response.start", "status": 200, "headers": []}); await send({"type": "http.response.body", "body": b"ok"})
st = drive(reader_app, [(b"content-length", b"70000")], [b"x" * 70000])
check(st["status"] == 413 and not st["called"] and st["pulled"] == 0, "declared Content-Length over the limit: 413 without running the app or reading a byte")
st = drive(reader_app, [(b"content-length", b"abc")], [b"x"])
check(st["status"] == 413 and not st["called"], "an unparseable Content-Length is refused")
st = drive(reader_app, [], [b"x" * 30000] * 3)
check(st["status"] == 413 and st["got"] <= MAX_BODY_BYTES, f"chunked body over the limit: 413 and the app never received more than the limit ({st['got']} bytes)")
st = drive(reader_app, [(b"content-length", b"10")], [b"x" * 30000] * 3)
check(st["status"] == 413 and st["got"] <= MAX_BODY_BYTES, "a lying Content-Length does not help")
st = drive(reader_app, [], [b"x" * 32768, b"y" * 32768])
check(st["status"] == 200 and st["got"] == 65536, "exactly 65536 bytes streamed passes")
st = drive(reader_app, [(b"content-length", b"65536")], [b"x" * 65536])
check(st["status"] == 200, "declared 65536 passes")
st = drive(reader_app, [], [b"x" * 32768, b"y" * 32769])
check(st["status"] == 413, "65537 bytes streamed is refused")
async def silent_app(scope, receive, send, st):
    while (await receive())["type"] == "http.request": pass
st = drive(silent_app, [], [b"x" * 30000] * 3)
check(st["status"] == 413, "an app that returns without answering after an overflow still yields a 413")
async def crashing_app(scope, receive, send, st):
    while (await receive())["type"] == "http.request": pass
    raise RuntimeError("parser blew up")
st = drive(crashing_app, [], [b"x" * 30000] * 3)
check(st["status"] == 413, "an app that crashes after an overflow still yields a 413")
async def answering_app(scope, receive, send, st):
    while (await receive())["type"] == "http.request": pass
    await send({"type": "http.response.start", "status": 422, "headers": []}); await send({"type": "http.response.body", "body": b"parse error"})
st = drive(answering_app, [], [b"x" * 30000] * 3)
check(st["status"] == 413, "the app's own error answer is replaced by the 413")
st = drive(reader_app, [], [], scope_type="lifespan")
check(st["called"], "non-HTTP scopes pass straight through")
st = drive(reader_app, [(b"content-length", b"70000")], [], scope_type="websocket")
check(st["called"] and st["status"] != 413, "a non-HTTP scope is never answered with an HTTP 413, whatever headers it carries")

print("-- 28. exactly-once results mail: durable per-lead mutex, email_sent only after a real send")
def kill_backends():
    db("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()")
reset(); lead = new_lead("mx1@example.com"); resend.Emails.send = _record; SENT.clear()
real_key = os.environ.pop("RESEND_API_KEY")
check(patch(lead, addr=ip()).status_code == 200 and email_sent_flag(lead) is False, "definite pre-send failure: 200, email_sent stays FALSE")
os.environ["RESEND_API_KEY"] = real_key
check(events("results_claim") == 0 and events("results_mailbox") == 0, "definite pre-send failure: the claim and the mailbox hit are both refunded")
patch(lead, addr=ip())
check(len(SENT) == 1 and email_sent_flag(lead) is True and events("results_claim") == 1, "a retry sends; the claim row stays as the mutex")
reset(); lead = new_lead("mx2@example.com")
def kill_then_fail(params): kill_backends(); raise RuntimeError("boom")
resend.Emails.send = kill_then_fail
r = patch(lead, addr=ip())
check(r.status_code == 200 and email_sent_flag(lead) is False and events("results_claim") == 1 and events("results_mailbox") == 1, "connection dropped during an ambiguous send failure: 200, no stuck TRUE, claim and mailbox hit kept")
resend.Emails.send = _record; SENT.clear(); patch(lead, addr=ip())
check(not SENT, "no second mail inside the hour")
backdate("results_claim", 3601); patch(lead, addr=ip())
check(len(SENT) == 1 and email_sent_flag(lead) is True, "and a later save, after the claim expired, delivers")
reset(); lead = new_lead("mx3@example.com"); SENT.clear()
def kill_then_ok(params): kill_backends(); SENT.append(params); return {"id": "x"}
resend.Emails.send = kill_then_ok
patch(lead, addr=ip()); resend.Emails.send = _record
check(len(SENT) == 1 and email_sent_flag(lead) is True, "connection dropped after a successful send: email_sent is still set (fresh connection)")
reset(); lead = new_lead("mx4@example.com"); SENT.clear(); n_conn = [0]; real_get = cl.get_conn
def flaky_conn():
    n_conn[0] += 1
    if n_conn[0] in (2, 3): raise psycopg2.OperationalError("down")
    return real_get()
cl.get_conn = flaky_conn; patch(lead, addr=ip()); cl.get_conn = real_get
check(len(SENT) == 1 and email_sent_flag(lead) is True and n_conn[0] == 4, f"marking sent is retried on fresh connections (took {n_conn[0] - 1} tries)")
reset(); lead = new_lead("mx5@example.com"); SENT.clear(); n_conn = [0]
def dead_conn():
    n_conn[0] += 1
    if n_conn[0] >= 2: raise psycopg2.OperationalError("down")
    return real_get()
cl.get_conn = dead_conn; patch(lead, addr=ip()); cl.get_conn = real_get
check(len(SENT) == 1 and email_sent_flag(lead) is False and n_conn[0] == 4, "marking sent gives up after exactly 3 tries")
patch(lead, addr=ip()); check(len(SENT) == 1, "the unexpired claim still blocks a second mail")
backdate("results_claim", 3601); patch(lead, addr=ip())
check(len(SENT) == 2 and email_sent_flag(lead) is True, "after the mutex expires the unmarked lead is mailed and marked (the one accepted duplicate: three database failures in a row right after a send)")
reset(); lead = new_lead("mx6@example.com"); SENT.clear()
dl.hit(dl.RESULTS_CLAIM, lead)                                  # a process died after claiming, before sending
patch(lead, addr=ip()); check(not SENT and email_sent_flag(lead) is False, "process-death simulation: the orphan claim blocks within the hour")
backdate("results_claim", 3595); patch(lead, addr=ip()); check(not SENT, "still blocked 5 s before the hour is up")
backdate("results_claim", 10); patch(lead, addr=ip()); check(len(SENT) == 1 and email_sent_flag(lead) is True, "allowed after the hour: mailed and marked")
reset()

print("-- 29. table security posture (RLS on, nothing for the PostgREST roles) and self-healing DDL")
reset(); dl._ready = False; dl.hit(dl.Cap("t", 1, 60), "k")
check(db("SELECT relrowsecurity FROM pg_class WHERE relname='calculator_rate_events'") == [(True,)], "row level security is enabled on calculator_rate_events")
check(db("SELECT count(*) FROM pg_policies WHERE tablename='calculator_rate_events'")[0][0] == 0, "no policies (same as the other calculator tables)")
seq = db("SELECT pg_get_serial_sequence('calculator_rate_events','id')")[0][0]
for role in ("anon", "authenticated"):
    t = db("SELECT " + ", ".join(f"has_table_privilege('{role}', 'calculator_rate_events', '{p}')" for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")))[0]
    check(not any(t) and db(f"SELECT has_sequence_privilege('{role}', '{seq}', 'USAGE'), has_sequence_privilege('{role}', '{seq}', 'SELECT'), has_sequence_privilege('{role}', '{seq}', 'UPDATE')")[0] == (False, False, False), f"{role} holds no privilege on the table or its sequence")
check(db("SELECT has_table_privilege('anon', 'calculator_leads', 'SELECT')")[0][0] is True, "control: the default grants really are present on a table that was not revoked (the assertion above is not vacuous)")
reset(); dl.hit(dl.Cap("t", 1, 60), "k")
db("DROP TABLE calculator_rate_events")
check(dl.hit(dl.Cap("t", 5, 60), "k") is None, "a dropped table: that call is refused (fail closed)")
check(dl.hit(dl.Cap("t", 5, 60), "k") is not None and db("SELECT to_regclass('calculator_rate_events')")[0][0] is not None, "the next call re-creates the table (self-heal)")
db("DROP INDEX calculator_rate_events_at"); dl._ready = False; dl.hit(dl.Cap("t", 5, 60), "k")
check(db("SELECT count(*) FROM pg_indexes WHERE tablename='calculator_rate_events'")[0][0] == 3, "a missing index is noticed and re-created while the table exists")
reset()

print("-- 30. a role without CREATE")
db("DROP ROLE IF EXISTS nocreate")
db("CREATE ROLE nocreate LOGIN BYPASSRLS"); db("GRANT USAGE ON SCHEMA public TO nocreate")      # BYPASSRLS like the production service role (owner/postgres): RLS is on
db("DROP TABLE calculator_rate_events")
real_dsn = os.environ["DATABASE_URL"]; nc_dsn = real_dsn.replace("cdai@", "nocreate@")
lines30 = []
class H30(logging.Handler):
    def emit(self, rec): lines30.append(rec.getMessage())
h30 = H30(); logging.getLogger("cdai.calculator_leads").addHandler(h30)
os.environ["DATABASE_URL"] = nc_dsn; dl._ready = False; dl._ddl_failure_logged = False; dl._fail_until = 0.0
res30 = [dl.hit(dl.Cap("t", 5, 60), "k") for _ in range(3)]
os.environ["DATABASE_URL"] = real_dsn
check(res30 == [None] * 3, "table absent and not creatable: every call is refused")
check(len([l for l in lines30 if "cannot prepare" in l]) == 1, f"exactly ONE log line for that, not one per request (got {len([l for l in lines30 if 'cannot prepare' in l])})")
dl._ready = False; dl._fail_until = 0.0; dl.hit(dl.Cap("t", 5, 60), "k")                # the owner creates it (as the service itself would)
db("GRANT SELECT, INSERT, DELETE ON calculator_rate_events TO nocreate"); db(f"GRANT USAGE ON SEQUENCE {db(chr(83)+'ELECT pg_get_serial_sequence(%s,%s)', ('calculator_rate_events','id'))[0][0]} TO nocreate")
os.environ["DATABASE_URL"] = nc_dsn; dl._ready = False; dl._ddl_failure_logged = False; dl._fail_until = 0.0
check(dl.hit(dl.Cap("t", 5, 60), "k2") is not None, "table pre-created: the role without CREATE works (no DDL is attempted)")
os.environ["DATABASE_URL"] = real_dsn; logging.getLogger("cdai.calculator_leads").removeHandler(h30)
db("REVOKE ALL ON calculator_rate_events FROM nocreate"); db("DROP OWNED BY nocreate"); db("DROP ROLE nocreate")
dl._ready = False; reset()

print("-- 31. timeouts")
import psycopg2 as _pg
seen_kw = []; real_pg_connect = _pg.connect
def spy_connect(*a, **k): seen_kw.append(k); return real_pg_connect(*a, **k)
_pg.connect = spy_connect
dl.connect().close(); cl.get_conn().close()
_pg.connect = real_pg_connect
check(len(seen_kw) == 2 and all(k.get("connect_timeout") == 5 for k in seen_kw), "durable_limits.connect and calculator_leads.get_conn both pass connect_timeout=5")
class Rec:
    def __init__(self, c, log): self._c, self.log = c, log
    def execute(self, sql, *a): self.log.append(" ".join(sql.split())); return self._c.execute(sql, *a)
    def __getattr__(self, n): return getattr(self._c, n)
class RecConn:
    def __init__(self, c, log): self._conn, self.log = c, log
    def cursor(self, *a, **k): return Rec(self._conn.cursor(*a, **k), self.log)
    def __getattr__(self, n): return getattr(self._conn, n)
stm = []
reset(); dl.hit(dl.Cap("t", 5, 60), "warm")       # table check done outside the recording
dl.connect = lambda: RecConn(_real_dl_connect(), stm)
h_ = dl.hit(dl.Cap("t", 5, 60), "k"); dl.refund(h_); dl.purge()
dl.connect = _real_dl_connect
check(stm[:2] == ["SET LOCAL lock_timeout = '2s'", "SET LOCAL statement_timeout = '5s'"], "allow() sets lock_timeout 2s and statement_timeout 5s before anything else")
check(sum(1 for q in stm if q == "SET LOCAL lock_timeout = '2s'") == 3 and sum(1 for q in stm if q == "SET LOCAL statement_timeout = '5s'") == 3, "so do refund() and purge()")
reset(); dl.hit(dl.Cap("t", 5, 60), "warm")
kk = dl._hash("stuck"); holder = psycopg2.connect(os.environ["DATABASE_URL"]); hc = holder.cursor()
hc.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("tb:" + kk,))
rel = threading.Timer(9.0, lambda: (holder.rollback(), holder.close())); rel.start()
t0 = time.time(); got = dl.allow("tb", "stuck", 5, 60); dt = time.time() - t0
check(got is None and dt < 2 * dl.LOCK_TIMEOUT_S + 3, f"a held advisory lock makes allow() refuse within about lock_timeout ({dt:.1f} s), it does not hang")
rel.join(); check(dl.allow("tb", "stuck", 5, 60) is not None, "and it works again once the lock is released")
reset()

print("-- 32. service-wide verify ceiling (300 per hour)")
reset()
fill(dl.VERIFY_GLOBAL, 300)
send_code("vg@example.com", addr=ip())
r = verify("vg@example.com", "000000", addr=ip())
check(r.status_code == 429 and r.json() == {"detail": GENERIC}, "ceiling reached: 429 with the generic text")
check(events("verify_mailbox") == 0, "the visitor's own mailbox hit was refunded")
backdate("global_verify", 3595); check(verify("vg@example.com", "000000", addr=ip()).status_code == 429, "still refused 5 s before the hour is up")
backdate("global_verify", 10); check(verify("vg@example.com", "000000", addr=ip()).status_code == 400, "allowed again after the hour")
reset()
fill(dl.VERIFY_GLOBAL, 299)
seed_otp("vg1@example.com"); seed_otp("vg2@example.com")
check(verify("vg1@example.com", "000000", addr=ip()).status_code == 400 and verify("vg2@example.com", "000000", addr=ip()).status_code == 429, "the 300th verify is allowed, the 301st refused")
reset()
for i in range(330): seed_otp(f"vgb{i}@example.com")
res = burst(330, lambda c, i: c.post("/calculator-lead", json={"name": "A", "company": "B", "email": f"vgb{i}@example.com", "code": "000000"}, headers=hdr(i)).status_code)
check(res.count(400) == 300 and res.count(429) == 30, f"330 concurrent verifies: exactly 300 reach the code lookup (got {res.count(400)} / {res.count(429)})")
check(events("global_verify") == 300 and events("verify_mailbox") == 300, "durable rows after the burst: 300 global + 300 mailbox (the 30 refused were refunded)")
reset()

print("-- 33. hostile JSON and identifiers")
reset(); lead = new_lead("hj@example.com")
def raw_patch(body, lid=None):
    return http.patch(f"/calculator-lead/{lid or lead}", content=body, headers={**H(), "Content-Type": "application/json"})
r = raw_patch('{"inputs":{"k":"x\\u0000y\\ud800z","a\\u0000b":"v","bs":"\\\\u0000","pair":"\\ud83d\\ude00","adSpend":0}}')
stored = db("SELECT inputs FROM calculator_leads WHERE id=%s", (lead,))[0][0]
check(r.status_code == 200 and stored["k"] == "xyz" and stored["ab"] == "v", "NUL and lone surrogates are removed from values and keys, the save succeeds")
check(stored["bs"] == "\\u0000" and stored["pair"] == "\U0001F600", "an escaped backslash before u0000 and a valid surrogate pair are left untouched")
r = raw_patch('{"inputs":{"k":"' + "\\u0000" * 9000 + '"},"results":{"x":1}}')
check(r.status_code == 200, f"the 20,000-character cap is measured after sanitising (got {r.status_code})")
r = raw_patch('{"inputs":{"k":"' + "v" * 20001 + '"}}'); check(r.status_code == 413, "and still enforced for real size")
send_code("hv@example.com", addr=ip()); good = last_code("hv@example.com")
r = http.post("/calculator-lead", content='{"name":"A","company":"B","email":"hv@example.com","code":"%s","inputs":{"k":"a\\u0000b\\ud800"}}' % good, headers={**H(), "Content-Type": "application/json"}); check(r.status_code == 200, "verify accepts NUL/surrogate junk in inputs too")
check(send_code("sur@example.com", name="a\ud800b", addr=ip()).status_code == 400 if False else http.post("/calculator-send-code", content='{"name":"a\\ud800b","company":"c","email":"sur@example.com"}', headers={**H(), "Content-Type": "application/json"}).status_code == 400, "a lone surrogate in a name is a 400, not a 500")
check(http.post("/calculator-send-code", content='{"name":"n","company":"c","email":"s\\ud800@example.com"}', headers={**H(), "Content-Type": "application/json"}).status_code == 400, "a lone surrogate in an address is a 400")
check(http.post("/calculator-send-code", content='{"name":"n\\u0000","company":"c","email":"nul@example.com"}', headers={**H(), "Content-Type": "application/json"}).status_code == 400, "NUL in a name is a 400")
check(http.post("/calculator-view", content='{"session_id":"s\\ud800"}', headers={**H(), "Content-Type": "application/json"}).json() == {"status": "ok"}, "a lone surrogate in a view session_id still answers ok")
base = lead
for label, lid in (("urn:uuid form", "urn:uuid:" + base), ("braced", "%7B" + base + "%7D"), ("trailing newline", base + "%0A"), ("32 hex without hyphens", base.replace("-", "")), ("leading space", "%20" + base)):
    check(raw_patch("{}", lid).status_code == 404, f"lead id as {label}: 404")
check(raw_patch("{}", base.upper()).status_code == 200, "an upper-case id is the same id")
reset(); send_code("nv@example.com", addr=ip()); good = last_code("nv@example.com")
verify("nv@example.com", "12345", addr=ip()); verify("nv@example.com", good, addr=ip(), inputs={"k": "v" * 20001}); verify("nv@example.com", good, addr=ip(), name="n" * 101); verify("not-an-email", good, addr=ip())
check(events("verify_mailbox") == 0 and events("global_verify") == 0, "requests that fail cheap validation consume no durable verify hit")
check(verify("nv@example.com", good, addr=ip()).status_code == 200 and events("verify_mailbox") == 1 and events("global_verify") == 1, "a valid attempt consumes exactly one of each")
reset()

print("-- 34. purge: bounded, single-flight, off the request path")
reset()
db("INSERT INTO calculator_rate_events (bucket, key, at) SELECT 'old', g::text, now() - interval '5 days' FROM generate_series(1, 2500) g")
db("INSERT INTO calculator_rate_events (bucket, key, at) SELECT 'new', g::text, now() - interval '1.9 days' FROM generate_series(1, 50) g")
dels = []
class DelRec:
    def __init__(self, c): self._c = c
    def execute(self, sql, *a):
        if sql.lstrip().startswith("DELETE FROM calculator_rate_events WHERE id IN"): dels.append(a)
        return self._c.execute(sql, *a)
    def __getattr__(self, n): return getattr(self._c, n)
class DelConn:
    def __init__(self, c): self._conn = c
    def cursor(self, *a, **k): return DelRec(self._conn.cursor(*a, **k))
    def __getattr__(self, n): return getattr(self._conn, n)
dl.connect = lambda: DelConn(_real_dl_connect())
deleted = dl.purge(); dl.connect = _real_dl_connect
check(deleted == 2500 and len(dels) == 3 and all(a[0][-1] == dl.PURGE_BATCH for a in dels), f"2,500 old rows are removed in {len(dels)} batches of at most {dl.PURGE_BATCH}")
check(events("new") == 50, "rows inside every window (1.9 days) are never touched")
db("INSERT INTO calculator_rate_events (bucket, key, at) SELECT 'old', g::text, now() - interval '5 days' FROM generate_series(1, 10) g")
lock_c = psycopg2.connect(os.environ["DATABASE_URL"]); lc = lock_c.cursor(); lc.execute("SELECT pg_advisory_xact_lock(%s)", (dl._PURGE_LOCK_ID,))
check(dl.purge() == 0 and events("old") == 10, "single-flight: while another purge holds the lock this one does nothing")
lock_c.rollback(); lock_c.close()
check(dl.purge() == 10, "and runs once the lock is free")
real_purge = dl.purge
dl.purge = lambda: (time.sleep(1.5), 0)[1]
dl._calls = dl.PURGE_EVERY_CALLS - 1; t0 = time.time(); dl.allow("lat", "k", 5, 60); dt = time.time() - t0
check(dt < 1.0, f"a request that triggers the purge does not wait for it ({dt:.2f} s)")
dl._purge_thread.join(10); dl.purge = real_purge
reset()

print("-- 35. random-mailbox spray cannot burn the service-wide verify budget")
reset()
codes = [verify(f"spray{i}@example.com", "000000", addr=f"41.{i // 250}.{i % 250}.9").status_code for i in range(400)]
check(set(codes) == {400} and events("global_verify") == 0, f"400 random mailboxes with no code: all answered 400 and 0 global hits consumed (global rows {events('global_verify')})")
check(verify("spray1@example.com", "000000", addr=ip()).json() == {"detail": "Invalid or expired code"}, "the answer is the same generic 400 as a wrong code")
reset(); send_code("live@example.com", addr=ip()); verify("live@example.com", "000000", addr=ip())
check(events("global_verify") == 1 and events("verify_mailbox") == 1, "a mailbox with a live code spends one global hit and one mailbox hit")
reset(); seed_otp("expired@example.com"); db("UPDATE calculator_otps SET expires_at = now() - interval '1 minute' WHERE email='expired@example.com'")
verify("expired@example.com", "123456", addr=ip()); seed_otp("used@example.com"); db("UPDATE calculator_otps SET verified = TRUE WHERE email='used@example.com'"); verify("used@example.com", "123456", addr=ip())
check(events("global_verify") == 0, "an expired or already-used code does not spend the global budget either")
reset()

print("-- 36. the OTP is consumed atomically (double verify)")
reset(); send_code("dbl@example.com", addr=ip()); good = last_code("dbl@example.com"); l0 = n("calculator_leads")
class PauseCur:
    def __init__(self, c): self._c = c
    def execute(self, sql, *a):
        r = self._c.execute(sql, *a)
        if "SELECT id FROM calculator_otps" in sql: time.sleep(0.3)        # both requests are past the lookup before either consumes the code
        return r
    def __getattr__(self, n): return getattr(self._c, n)
class PauseConn:
    def __init__(self, c): self._conn = c
    def cursor(self, *a, **k): return PauseCur(self._conn.cursor(*a, **k))
    def __getattr__(self, n): return getattr(self._conn, n)
posts4 = []; real_post4 = requests.post; requests.post = lambda url, **kw: posts4.append(kw); cl.BEEHIIV_API_KEY, cl.BEEHIIV_PUB_ID = "k", "p"
cl.get_conn = lambda: PauseConn(real_get())
res = burst(20, lambda c, i: c.post("/calculator-lead", json={"name": "A", "company": "B", "email": "dbl@example.com", "code": good}, headers=hdr(i)).status_code)
cl.get_conn = real_get; requests.post = real_post4; cl.BEEHIIV_API_KEY = cl.BEEHIIV_PUB_ID = None
check(res.count(200) == 1 and n("calculator_leads") - l0 == 1, f"20 simultaneous verifies with the same correct code: exactly ONE lead (200s: {res.count(200)}, rows: {n('calculator_leads') - l0})")
check(len(posts4) == 1, f"and exactly one Beehiiv call (got {len(posts4)})")
check(all(c in (200, 400, 429) for c in res), "the others get the generic 400/429, never a 500")
reset()

print("-- 37. a failure that never reached the code check does not cost an attempt")
reset(); send_code("fa@example.com", addr=ip()); events0 = events()
def no_conn(): raise psycopg2.OperationalError("down")
cl.get_conn = no_conn
r = verify("fa@example.com", "000000", addr=ip())
check(r.status_code == 500 and events("verify_mailbox") == 0 and events("global_verify") == 0, "get_conn failing in verify: 500 and no durable hit left consumed")
o0 = n("calculator_otps"); r = send_code("fb@example.com", addr=ip()); cl.get_conn = real_get
check(r.status_code == 500 and events("code_mailbox") == 1 and events("global_code") == 1 and n("calculator_otps") == o0, "get_conn failing in send-code: 500, no OTP row, and only the earlier successful request's hits remain")
class BoomCur:
    def __init__(self, c): self._c = c
    def execute(self, sql, *a):
        if "SELECT id FROM calculator_otps" in sql: raise psycopg2.OperationalError("lookup failed")
        return self._c.execute(sql, *a)
    def __getattr__(self, n): return getattr(self._c, n)
class BoomConn:
    def __init__(self, c): self._conn = c
    def cursor(self, *a, **k): return BoomCur(self._conn.cursor(*a, **k))
    def __getattr__(self, n): return getattr(self._conn, n)
reset(); send_code("fc@example.com", addr=ip()); cl.get_conn = lambda: BoomConn(real_get())
r = verify("fc@example.com", "000000", addr=ip()); cl.get_conn = real_get
check(r.status_code == 500 and events("verify_mailbox") == 0 and events("global_verify") == 0, "a database error before the code comparison: 500 and both hits refunded")
reset()

print("-- 38. invisible and bidirectional characters in names")
reset()
ok_names = ["Zoë", "Mina می‌خواهم", "Fam \U0001F468‍\U0001F469‍\U0001F467", "O'Brien & Sons"]
bad_chars = {"U+200B": "​", "U+200E": "‎", "U+200F": "‏", "U+202A": "‪", "U+202B": "‫", "U+202C": "‬", "U+202D": "‭", "U+202E": "‮",
             "U+2028": " ", "U+2029": " ", "U+2066": "⁦", "U+2067": "⁧", "U+2068": "⁨", "U+2069": "⁩", "U+FEFF": "﻿"}
for i, nm in enumerate(ok_names):
    check(send_code(f"okname{i}@example.com", name=nm, company=nm, addr=ip()).status_code == 200, f"accepted: {nm!r}")
bad = [k for k, ch in bad_chars.items() if send_code(f"badname@example.com", name="Ann" + ch + "Lee", addr=ip()).status_code != 400 or send_code("badname@example.com", company="C" + ch + "o", addr=ip()).status_code != 400]
check(not bad, f"refused (400) in name and in company: all 15 listed characters (not refused: {bad})")
check(events("code_mailbox") == len(ok_names) and send_code("badname@example.com", name="​", addr=ip()).json() == {"detail": "Name contains invalid characters"}, "same 'invalid characters' text, and no durable hit consumed by a refused name")
reset()

print("-- 39. table prepare failures are cached negatively and never queue requests")
reset(); dl._ready = False; calls39 = [0]
def slow_down(): calls39[0] += 1; time.sleep(0.5); raise psycopg2.OperationalError("unreachable")
dl.connect = slow_down; t0 = time.time(); outs = [None] * 50
def worker39(i): outs[i] = dl.allow("neg", f"k{i}", 5, 60)
ts = [threading.Thread(target=worker39, args=(i,)) for i in range(50)]; [t.start() for t in ts]; [t.join(120) for t in ts]
dt = time.time() - t0
check(all(o is None for o in outs) and dt < 6, f"database unreachable: 50 concurrent allow() calls all refuse within {dt:.1f} s in total, not 50 x the connect time")
check(calls39[0] == 1, f"and only ONE connect attempt was made (got {calls39[0]})")
t0 = time.time(); dl.allow("neg", "again", 5, 60); check(time.time() - t0 < 0.2 and calls39[0] == 1, "later calls inside the 10 s window refuse instantly without touching the database")
dl.connect = _real_dl_connect; check(dl.allow("neg", "again", 5, 60) is None, "still refused while the negative cache lives")
dl._fail_until = time.monotonic() - 1; check(dl.allow("neg", "again", 5, 60) is not None, "after the 10 s window the next call prepares the table and recovers")
check(dl.NEGATIVE_CACHE_S == 10, "negative cache is 10 s")
reset()

print("-- 40. drift repair: a pre-existing table without RLS or with grants is secured, not recreated")
reset(); db("DROP TABLE IF EXISTS calculator_rate_events")
db("CREATE TABLE calculator_rate_events (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, bucket TEXT NOT NULL, key TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
db("CREATE INDEX calculator_rate_events_bucket_key_at ON calculator_rate_events (bucket, key, at)"); db("CREATE INDEX calculator_rate_events_at ON calculator_rate_events (at)")
db("INSERT INTO calculator_rate_events (bucket, key) VALUES ('marker', 'm')")
check(db("SELECT has_table_privilege('anon', 'calculator_rate_events', 'SELECT')")[0][0] is True and db("SELECT relrowsecurity FROM pg_class WHERE relname='calculator_rate_events'") == [(False,)], "precondition: no RLS and the client roles hold grants")
dl._ready = False; dl._fail_until = 0.0; check(dl.hit(dl.Cap("t", 5, 60), "k") is not None, "ensure_table runs")
check(db("SELECT relrowsecurity FROM pg_class WHERE relname='calculator_rate_events'") == [(True,)], "RLS is now on")
check(not any(db("SELECT " + ", ".join(f"has_table_privilege('{r}', 'calculator_rate_events', '{p}')" for r in ("anon", "authenticated") for p in ("SELECT", "INSERT", "UPDATE", "DELETE")))[0]), "anon and authenticated hold no privilege any more")
check(events("marker") == 1, "the existing rows were kept (repair, not re-create)")
db("GRANT SELECT, INSERT ON calculator_rate_events TO anon, authenticated")           # grant drift alone, RLS stays on
check(db("SELECT relrowsecurity FROM pg_class WHERE relname='calculator_rate_events'") == [(True,)] and db("SELECT has_table_privilege('anon', 'calculator_rate_events', 'SELECT')")[0][0] is True, "precondition: RLS on, grants present")
dl._ready = False; dl.hit(dl.Cap("t", 5, 60), "k2")
check(not any(db("SELECT " + ", ".join(f"has_table_privilege('{r}', 'calculator_rate_events', '{p}')" for r in ("anon", "authenticated") for p in ("SELECT", "INSERT")))[0]), "grant drift alone is also repaired")
reset()

print("-- 41. purge is quiet when the table does not exist yet")
db("DROP TABLE calculator_rate_events"); lines41 = []
class H41(logging.Handler):
    def emit(self, rec): lines41.append(rec.getMessage())
h41 = H41(); logging.getLogger("cdai.calculator_leads").addHandler(h41)
check(dl.purge() == 0 and not lines41, f"purge with no table: returns 0 and logs nothing (logged {lines41})")
logging.getLogger("cdai.calculator_leads").removeHandler(h41); dl._ready = False; reset()

print("-- 42. CORS")
pre = http.options("/calculator-lead", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"})
check("access-control-allow-origin" not in pre.headers, "localhost origins are no longer allowed")
pre = http.options("/calculator-lead", headers={"Origin": "https://www.alloceraintelligence.com", "Access-Control-Request-Method": "POST"})
check(pre.headers.get("access-control-allow-origin") == "https://www.alloceraintelligence.com" and "access-control-allow-credentials" not in pre.headers, "the www site origin is allowed, without credentials")
pre = http.options("/calculator-lead", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
check("access-control-allow-origin" not in pre.headers, "another origin is refused")

print("-- 17. nothing else moved")
ops = sorted((p_, m) for p_, d in app.openapi()["paths"].items() for m in d)
check(ops == [("/calculator-lead", "post"), ("/calculator-lead/{lead_id}", "patch"), ("/calculator-send-code", "post"), ("/calculator-view", "post"), ("/health", "get")], "route table is exactly the original five")
pre = http.options("/calculator-lead", headers={"Origin": "https://alloceraintelligence.com", "Access-Control-Request-Method": "POST"})
check(pre.headers.get("access-control-allow-origin") == "https://alloceraintelligence.com", "CORS preflight still allows the site origin")

print(f"\n{count[0] - len(fails)}/{count[0]} checks passed")
sys.exit(1 if fails else 0)
