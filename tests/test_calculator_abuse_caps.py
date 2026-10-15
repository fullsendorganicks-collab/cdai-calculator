"""Abuse caps, validation, escaping and log hygiene for the calculator service (Oct 14 2026).

Throwaway local PostgreSQL with the three production tables; Resend replaced by a recorder (fake key, nothing leaves the machine); Beehiiv
unconfigured except where a test patches `requests.post`. Run from anywhere:  python tests/test_calculator_abuse_caps.py   (exit 1 on any FAIL)
Limits are per process, so every section starts with rate_limits.reset().
"""
import importlib.util, logging, os, re, subprocess, sys, time

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
import client_ip
from main import app

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

print("-- 16. the configured limits (amount per window)")
table = {"SEND_CODE_PER_IP": (5, 3600), "SEND_CODE_PER_EMAIL": (3, 3600), "VERIFY_PER_IP": (20, 600), "VERIFY_PER_EMAIL": (6, 600),
         "VIEW_PER_IP": (60, 60), "UPDATE_PER_IP": (120, 60), "RESULTS_EMAIL_PER_ADDRESS": (3, 86400)}
for k, (amt, win) in table.items():
    lim = getattr(rl, k); check((lim.amount, lim.get_expiry()) == (amt, win), f"{k} = {amt} per {win} s")
check((cl.NAME_MAX, cl.COMPANY_MAX, cl.EMAIL_MAX, cl.SESSION_ID_MAX, cl.JSON_MAX_CHARS, cl.SUBJECT_MAX) == (100, 200, 254, 100, 20000, 200), "size limits are 100/200/254/100/20000/200")
check(cl.TOO_MANY_DETAIL == GENERIC, "the one refusal text")

print("-- 17. nothing else moved")
ops = sorted((p_, m) for p_, d in app.openapi()["paths"].items() for m in d)
check(ops == [("/calculator-lead", "post"), ("/calculator-lead/{lead_id}", "patch"), ("/calculator-send-code", "post"), ("/calculator-view", "post"), ("/health", "get")], "route table is exactly the original five")
pre = http.options("/calculator-lead", headers={"Origin": "https://alloceraintelligence.com", "Access-Control-Request-Method": "POST"})
check(pre.headers.get("access-control-allow-origin") == "https://alloceraintelligence.com", "CORS preflight still allows the site origin")

print(f"\n{count[0] - len(fails)}/{count[0]} checks passed")
sys.exit(1 if fails else 0)
