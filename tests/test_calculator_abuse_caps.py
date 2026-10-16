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
def reset():
    rl.reset(); SENT.clear()
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
class R:
    def __init__(self, xff=None, host="10.0.0.1"):
        self.headers = {"x-forwarded-for": xff} if xff else {}
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
dtable = {"SEND_CODE_PER_MAILBOX": ("code_mailbox", 3, 3600), "SEND_CODE_GLOBAL": ("global_code", 100, 3600), "VERIFY_PER_MAILBOX": ("verify_mailbox", 6, 600), "RESULTS_PER_MAILBOX": ("results_mailbox", 3, 86400)}
for k, v in dtable.items(): check(tuple(getattr(dl, k)) == v, f"durable {k} = {v[1]} per {v[2]} s")
check(dl.GLOBAL_KEY == "*" and dl.PURGE_AFTER == "2 days" and dl.PURGE_EVERY_CALLS == 100, "global key '*', purge after 2 days, every 100 calls")
check((cl.NAME_MAX, cl.COMPANY_MAX, cl.EMAIL_MAX, cl.SESSION_ID_MAX, cl.JSON_MAX_CHARS, cl.SUBJECT_MAX) == (100, 200, 254, 100, 20000, 200), "size limits are 100/200/254/100/20000/200")
check(cl.TOO_MANY_DETAIL == GENERIC, "the one refusal text")

print("-- 18. client address: a public socket address wins and X-Forwarded-For is ignored")
class R2:
    def __init__(self, host, xff=None):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = type("C", (), {"host": host})() if host else None
rip = client_ip.real_client_ip
check(rip(R2("8.8.8.8", "1.2.3.4, 5.6.7.8")) == "8.8.8.8", "public host: forged X-Forwarded-For changes nothing")
check(rip(R2("8.8.8.8", "9.9.9.9")) == "8.8.8.8" and rip(R2("8.8.8.8")) == "8.8.8.8", "public host with or without the header")
check(rip(R2("::ffff:8.8.8.8", "1.1.1.1")) == "8.8.8.8", "IPv4-mapped public host is the IPv4 address")
check(rip(R2("2606:4700::1111", "1.1.1.1")) == "2606:4700::1111", "public IPv6 host used as is")
for h in ("10.1.2.3", "172.16.0.9", "192.168.1.1", "127.0.0.1", "169.254.1.1", "0.0.0.0", "100.64.0.5", "::1", "fe80::1", "testclient"):
    check(rip(R2(h, "7.7.7.7, 3.3.3.3")) == "3.3.3.3", f"non-public host {h}: last X-Forwarded-For hop is used")
check(rip(R2("10.0.0.1", "3.3.3.3, not-an-ip")) == "10.0.0.1", "non-public host and a garbage last hop: falls back to the host")
check(rip(R2(None, "garbage")) == "unknown" and rip(R2(None, "4.4.4.4")) == "4.4.4.4", "no host: 'unknown' for garbage, the hop when it parses")
check(rip(R2("10.0.0.1")) == "10.0.0.1", "non-public host and no header: the host")
pub = TestClient(app, client=("8.8.4.4", 5000)); pub2 = TestClient(app, client=("9.9.9.9", 5000))
reset()
codes = [pub.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"pc{i}@example.com"}, headers={"X-Forwarded-For": f"1.1.1.{i}"}).status_code for i in range(6)]
check(codes == [200] * 5 + [429], f"end to end: six forged-header requests from one public socket address share one bucket (got {codes})")
check(pub2.post("/calculator-send-code", json={"name": "A", "company": "B", "email": "pc9@example.com"}, headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200, "a different socket address is a different bucket whatever the header claims")
reset(); v0 = n("calculator_views")
for i in range(61): pub.post("/calculator-view", json={"session_id": "s"}, headers={"X-Forwarded-For": f"2.2.2.{i}"})
check(n("calculator_views") - v0 == 60, "view cap also keyed by the socket address, not the header")

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
reset(); lead = new_lead("refund2@example.com"); resend.Emails.send = failing_send
patch(lead, addr=ip())
check(events("results_mailbox") == 0 and email_sent_flag(lead) is False, "failed results mail: hit refunded and email_sent back to FALSE")
resend.Emails.send = _record; SENT.clear(); patch(lead, addr=ip())
check(len(SENT) == 1 and email_sent_flag(lead) is True, "the next save delivers it")
reset()
for _ in range(100): dl.hit(dl.SEND_CODE_GLOBAL, dl.GLOBAL_KEY)
o0 = n("calculator_otps"); SENT.clear()
r = send_code("pausechk@example.com", addr=ip())
check(r.status_code == 429 and r.json() == {"detail": GENERIC} and n("calculator_otps") == o0 and not SENT, "global ceiling reached: 429, generic text, no OTP row, no mail")
check(events("code_mailbox") == 0, "the refused visitor's per-mailbox hit was refunded")
reset()

print("-- 22. time travel: N allowed, N+1 refused, allowed again after the window (in-memory caps)")
def sc(i, addr): return send_code(f"tt{i}@example.com", addr=addr).status_code
reset(); A = "21.0.0.1"
check([sc(i, A) for i in range(6)] == [200] * 5 + [429], "send-code per IP: 5 allowed, 6th refused")
clock.off += 3599; check(sc(7, A) == 429, "send-code per IP: still refused 1 s before the hour is up")
clock.off += 2; check(sc(8, A) == 200, "send-code per IP: allowed again after the hour")
reset(); A = "21.0.0.2"
check([verify(f"vt{i}@example.com", addr=A).status_code for i in range(21)] == [400] * 20 + [429], "verify per IP: 20 allowed, 21st refused")
clock.off += 599; check(verify("vt99@example.com", addr=A).status_code == 429, "verify per IP: refused just inside 10 minutes")
clock.off += 2; check(verify("vt99@example.com", addr=A).status_code == 400, "verify per IP: allowed again after 10 minutes")
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
for _ in range(100): dl.hit(dl.SEND_CODE_GLOBAL, dl.GLOBAL_KEY)
backdate("global_code", 3595); check(send_code("g1@example.com", addr=ip()).status_code == 429, "global: still refused inside the hour")
backdate("global_code", 10); check(send_code("g1@example.com", addr=ip()).status_code == 200, "global: allowed again after the hour")
reset()
for _ in range(99): dl.hit(dl.SEND_CODE_GLOBAL, dl.GLOBAL_KEY)
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
    barrier = threading.Barrier(n_threads); out = [None] * n_threads
    def run(i):
        c = TestClient(app); barrier.wait(); out[i] = fn(c, i)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n_threads)]
    [t.start() for t in ts]; [t.join(120) for t in ts]
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
reset(); o0 = n("calculator_otps"); slow_on()
res = burst(120, lambda c, i: c.post("/calculator-send-code", json={"name": "A", "company": "B", "email": f"gb{i}@example.com"}, headers=hdr(i)).status_code)
slow_off()
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

print("-- 17. nothing else moved")
ops = sorted((p_, m) for p_, d in app.openapi()["paths"].items() for m in d)
check(ops == [("/calculator-lead", "post"), ("/calculator-lead/{lead_id}", "patch"), ("/calculator-send-code", "post"), ("/calculator-view", "post"), ("/health", "get")], "route table is exactly the original five")
pre = http.options("/calculator-lead", headers={"Origin": "https://alloceraintelligence.com", "Access-Control-Request-Method": "POST"})
check(pre.headers.get("access-control-allow-origin") == "https://alloceraintelligence.com", "CORS preflight still allows the site origin")

print(f"\n{count[0] - len(fails)}/{count[0]} checks passed")
sys.exit(1 if fails else 0)
