"""The results email's next steps (Oct 25 2026): Option 01 stays the free audit (#intake); Option 02 names the $497/month Entry price and links straight to signup.

No database and no network: Resend is replaced by a recorder (fake key, nothing leaves the machine). Run from anywhere:  python tests/test_results_email_cta.py   (exit 1 on any FAIL)
The email gate (send-code / verify) is not touched by this change and is covered by test_calculator_abuse_caps.py.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["RESEND_API_KEY"] = "re_fake_test_key"

import resend
SENT = []
resend.Emails.send = lambda params: SENT.append(params) or {"id": "fake"}

import calculator_leads

fails = []
def check(ok, m):
    print(f"  [{'PASS' if ok else 'FAIL'}] {m}"); fails.append(m) if not ok else None

RESULTS = {"reportedCpl": 42.0, "trueCpl": 61.5, "hiddenCosts": 1950.0, "cmPct": 12.3, "cplGapPct": 46.4}
ok = calculator_leads.send_results_email("Pat", "Acme Roofing", "pat@example.com", RESULTS, {})
check(ok is True and len(SENT) == 1, "the results email is sent exactly once through Resend")
html = SENT[0]["html"] if SENT else ""
hrefs = re.findall(r'href="([^"]+)"', html)

print("Option 01: free audit")
check(hrefs.count("https://alloceraintelligence.com/#intake") == 1, "the free audit button still goes to the intake form (#intake), once")
check("Request Free Audit" in html, "the free audit button text is unchanged")

print("Option 02: retainer")
check(hrefs.count("https://cdai-portal.vercel.app/signup/") == 1, "the retainer button links to the signup page, once")
opt2 = html[html.index("Option 02"):html.index('<div class="newsletter">')]
check("https://cdai-portal.vercel.app/signup/" in opt2 and "#intake" not in opt2, "the signup link is the Option 02 button, and Option 02 no longer sends people to the intake form")
check("$497/month" in opt2 and "up to $10K/month in ad spend" in opt2, "Option 02 states the Entry price and its spend range (live Stripe price, CLAUDE.md pricing bullet)")

print("Copy rules")
low = html.lower()
check(not re.search(r"\b(7|seven)\s+(hidden\s+)?cost layers", low), "no '7 cost layers'")
check(not re.search(r"\d+(\.\d+)?\s?%\s+accura|accura\w*\s+\d", low), "no accuracy percentage")
check("100% credit" not in low and "credit 100%" not in low, "no '100% credit'")
check(SENT and SENT[0]["subject"].startswith("Your distortion audit"), "the subject line is unchanged")

print("ALL PASS" if not fails else f"FAILED {len(fails)}"); sys.exit(1 if fails else 0)
