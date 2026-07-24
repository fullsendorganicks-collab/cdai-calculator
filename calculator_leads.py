"""
Calculator Leads — Distortion Calculator lead capture endpoints.

POST  /calculator-view              Anonymous funnel-entry ping. No PII.
POST  /calculator-send-code        Send 6-digit OTP to email. First gate step.
POST  /calculator-lead             Verify OTP, create row, subscribe Beehiiv. Returns {id}.
PATCH /calculator-lead/{lead_id}   Live save. Sends results email once when data is real.

Completely isolated — reads/writes only calculator_leads, calculator_otps,
and calculator_views tables. No access to any engine tables (orgs,
lead_events, cost_events, etc).
"""

import os
import json
import uuid
import random
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("cdai.calculator_leads")

DB_URL          = os.getenv("DATABASE_URL")
BEEHIIV_API_KEY = os.getenv("BEEHIIV_API_KEY")
BEEHIIV_PUB_ID  = os.getenv("BEEHIIV_PUBLICATION_ID")

router = APIRouter()


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)


_VIEWS_TABLE_READY = False


def _ensure_views_table(cur) -> None:
    """Idempotent — creates calculator_views on first call if missing."""
    global _VIEWS_TABLE_READY
    if _VIEWS_TABLE_READY:
        return
    cur.execute("""
        CREATE TABLE IF NOT EXISTS calculator_views (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id TEXT,
            viewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    _VIEWS_TABLE_READY = True


# ── Pydantic models ──────────────────────────────────────────────────────────

class ViewPing(BaseModel):
    session_id: Optional[str] = None


class SendCodeRequest(BaseModel):
    name: str
    company: str
    email: str


class CalcLeadCreate(BaseModel):
    name: str
    company: str
    email: str
    code: str
    inputs: Optional[dict] = None
    results: Optional[dict] = None


class CalcLeadUpdate(BaseModel):
    inputs: Optional[dict] = None
    results: Optional[dict] = None


# ── Beehiiv ──────────────────────────────────────────────────────────────────

def _subscribe_beehiiv(email: str) -> None:
    """Auto-subscribe to Beehiiv. Silent — never blocks the response."""
    try:
        if not BEEHIIV_API_KEY or not BEEHIIV_PUB_ID:
            return
        import requests as req
        req.post(
            f"https://api.beehiiv.com/v2/publications/{BEEHIIV_PUB_ID}/subscriptions",
            headers={
                "Authorization": f"Bearer {BEEHIIV_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "email": email,
                "reactivate_existing": True,
                "send_welcome_email": True,
                "utm_source": "distortion-calculator",
                "utm_medium": "website",
            },
            timeout=5,
        )
        logger.info(f"[CALC] Beehiiv subscribed: {email}")
    except Exception as e:
        logger.error(f"[CALC] Beehiiv subscribe failed for {email}: {e}")


# ── Email helpers ─────────────────────────────────────────────────────────────

def _fmt(n: float) -> str:
    return f"${n:,.2f}"

def _fmt_r(n: float) -> str:
    return f"${round(n):,}"


def send_otp_email(name: str, email: str, code: str) -> bool:
    """Send the 6-digit verification code."""
    try:
        import resend
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            return False
        resend.api_key = api_key

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body{{margin:0;padding:0;background:#010914;color:#EBF0FF;
       font-family:Inter,Arial,sans-serif}}
  .wrap{{max-width:480px;margin:0 auto;padding:48px 24px;text-align:center}}
  .eyebrow{{font-family:'Courier New',monospace;font-size:10px;letter-spacing:.2em;
            text-transform:uppercase;color:#C4A040;margin-bottom:20px;display:block}}
  h1{{font-size:22px;font-weight:700;color:#EBF0FF;margin:0 0 8px}}
  .sub{{font-size:14px;color:#9AAECE;margin:0 0 36px;line-height:1.6}}
  .code-box{{background:#071530;border:1px solid rgba(64,128,240,.3);
             border-top:2px solid #C4A040;border-radius:2px;
             padding:28px;margin:0 auto 32px;max-width:280px}}
  .code{{font-family:'Courier New',monospace;font-size:42px;font-weight:700;
         letter-spacing:.2em;color:#EBF0FF;display:block;margin-bottom:10px}}
  .expires{{font-family:'Courier New',monospace;font-size:10px;letter-spacing:.1em;
            text-transform:uppercase;color:#4A6488}}
  .note{{font-size:12px;color:#4A6488;line-height:1.6}}
  .footer{{margin-top:36px;font-family:'Courier New',monospace;font-size:9px;
           color:#2a3d5a;letter-spacing:.06em}}
</style>
</head>
<body>
<div class="wrap">
  <span class="eyebrow">Allocera Intelligence — Distortion Calculator</span>
  <h1>Your verification code</h1>
  <p class="sub">Hi {name} — enter this code to unlock your calculator and see your true CPL.</p>
  <div class="code-box">
    <span class="code">{code}</span>
    <span class="expires">Expires in 10 minutes</span>
  </div>
  <p class="note">
    If you didn't request this, you can ignore this email.<br>
    No one else can see this code.
  </p>
  <div class="footer">Allocera Intelligence &middot; alloceraintelligence.com</div>
</div>
</body>
</html>"""

        resend.Emails.send({
            "from": "directives@alloceraintelligence.com",
            "reply_to": "nick@alloceraintelligence.com",
            "to": email,
            "subject": f"Your Distortion Calculator code: {code}",
            "html": html,
        })
        logger.info(f"[CALC] OTP sent to {email}")
        return True
    except Exception as e:
        logger.error(f"[CALC] OTP email failed for {email}: {e}")
        return False


def send_results_email(name: str, company: str, email: str,
                       results: dict, inputs: dict) -> bool:
    """Send distortion results email via Resend."""
    try:
        import resend
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            return False
        resend.api_key = api_key

        reported_cpl = results.get("reportedCpl", 0)
        true_cpl     = results.get("trueCpl", 0)
        hidden_costs = results.get("hiddenCosts", 0)
        cm_pct       = results.get("cmPct", 0)
        cpl_gap_pct  = results.get("cplGapPct", 0)

        cm_color = "#0BAF92" if cm_pct >= 0 else "#D63050"
        cm_sign  = "+" if cm_pct >= 0 else ""
        gap_dir  = "higher" if cpl_gap_pct > 0 else "lower"
        gap_abs  = abs(cpl_gap_pct)
        cpl_diff = abs(true_cpl - reported_cpl)

        subject = (
            f"Your distortion audit — {company} "
            f"({_fmt(true_cpl)} true CPL vs {_fmt(reported_cpl)} reported)"
        )

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{margin:0;padding:0;background:#010914;color:#EBF0FF;
       font-family:Inter,Arial,sans-serif;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:600px;margin:0 auto;padding:40px 24px}}
  .eyebrow{{font-family:'Courier New',monospace;font-size:10px;
            letter-spacing:.2em;text-transform:uppercase;color:#C4A040;
            margin-bottom:16px;display:block}}
  h1{{font-size:26px;font-weight:700;color:#EBF0FF;margin:0 0 8px;line-height:1.2}}
  .sub{{font-size:14px;color:#9AAECE;margin:0 0 32px;line-height:1.65}}
  table.cpl{{width:100%;border-collapse:separate;border-spacing:10px 0;margin-bottom:24px}}
  .cpl-rep{{background:#071530;border:1px solid #1A3060;padding:20px;border-radius:2px;width:50%;vertical-align:top}}
  .cpl-tru{{background:rgba(214,48,80,.06);border:1px solid rgba(214,48,80,.35);padding:20px;border-radius:2px;width:50%;vertical-align:top}}
  .cpl-lbl{{font-family:'Courier New',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;display:block;margin-bottom:8px}}
  .cpl-rep .cpl-lbl{{color:#4A6488}}
  .cpl-tru .cpl-lbl{{color:#D63050}}
  .cpl-val{{font-family:'Courier New',monospace;font-size:34px;font-weight:700;display:block;line-height:1}}
  .cpl-rep .cpl-val{{color:#9AAECE}}
  .cpl-tru .cpl-val{{color:#D63050}}
  table.stats{{width:100%;border-collapse:separate;border-spacing:10px 0;margin-bottom:24px}}
  .stat-cell{{background:#071530;border:1px solid #102040;padding:16px;border-radius:2px;width:50%;vertical-align:top}}
  .stat-lbl{{font-family:'Courier New',monospace;font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:#4A6488;display:block;margin-bottom:6px}}
  .stat-val{{font-family:'Courier New',monospace;font-size:22px;font-weight:700}}
  .gap-box{{background:rgba(22,85,216,.08);border:1px solid rgba(22,85,216,.25);border-left:3px solid #1655D8;padding:16px 20px;margin-bottom:32px;font-size:14px;color:#B8C9E0;line-height:1.7;border-radius:2px}}
  .gap-box b{{color:#4080F0;font-family:'Courier New',monospace}}
  hr{{border:none;border-top:1px solid #102040;margin:32px 0}}
  .sec-title{{font-family:'Courier New',monospace;font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:#C4A040;margin-bottom:16px;display:block}}
  .option{{background:#071530;border:1px solid #1A3060;padding:20px 24px;margin-bottom:12px;border-radius:2px}}
  .opt-lbl{{font-family:'Courier New',monospace;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#4080F0;display:block;margin-bottom:8px}}
  .opt-title{{font-size:15px;font-weight:600;color:#EBF0FF;display:block;margin-bottom:6px}}
  .opt-desc{{font-size:13px;color:#7A95B8;line-height:1.6;margin-bottom:14px}}
  .btn{{display:inline-block;font-family:'Courier New',monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;background:#1655D8;color:#EBF0FF;padding:12px 24px;border-radius:2px;text-decoration:none;font-weight:600}}
  .btn-gold{{display:inline-block;font-family:'Courier New',monospace;font-size:11px;letter-spacing:.1em;text-transform:uppercase;background:transparent;color:#C4A040;border:1px solid rgba(196,160,64,.5);padding:12px 24px;border-radius:2px;text-decoration:none}}
  .newsletter{{background:rgba(196,160,64,.06);border:1px solid rgba(196,160,64,.2);border-top:2px solid #C4A040;padding:20px 24px;margin-top:28px;border-radius:2px}}
  .nl-title{{font-size:15px;color:#EBF0FF;margin:0 0 6px;font-weight:600}}
  .nl-desc{{font-size:13px;color:#7A95B8;line-height:1.6;margin:0 0 14px}}
  .footer{{margin-top:40px;font-family:'Courier New',monospace;font-size:9px;letter-spacing:.07em;color:#4A6488;line-height:1.9}}
  .footer a{{color:#4A6488}}
</style>
</head>
<body>
<div class="wrap">
  <span class="eyebrow">Allocera Intelligence — Distortion Calculator Results</span>
  <h1>Your true CPL for {company}</h1>
  <p class="sub">Here's what the calculator found — the gap between what your ad platform reports and what you're actually paying after every hidden cost layer.</p>
  <table class="cpl"><tr>
    <td class="cpl-rep"><span class="cpl-lbl">What your dashboard reports</span><span class="cpl-val">{_fmt(reported_cpl)}</span></td>
    <td class="cpl-tru"><span class="cpl-lbl">What you're actually paying</span><span class="cpl-val">{_fmt(true_cpl)}</span></td>
  </tr></table>
  <table class="stats"><tr>
    <td class="stat-cell"><span class="stat-lbl">True contribution margin</span><span class="stat-val" style="color:{cm_color}">{cm_sign}{cm_pct:.1f}%</span></td>
    <td class="stat-cell"><span class="stat-lbl">Hidden costs / month</span><span class="stat-val" style="color:#EBF0FF">{_fmt_r(hidden_costs)}</span></td>
  </tr></table>
  <div class="gap-box">
    Your true cost per lead is <b>{gap_abs:.0f}% {gap_dir}</b> than your dashboard reports — a gap of <b>{_fmt(cpl_diff)} per lead</b>, or <b>{_fmt_r(hidden_costs)}/month</b> on this campaign alone.<br><br>
    This was one campaign, entered manually. CDAI tracks this automatically across every campaign, every morning — connected live to your ad accounts and CRM, with no manual entry.
  </div>
  <hr>
  <span class="sec-title">What to do next</span>
  <div class="option">
    <span class="opt-lbl">Option 01 — Free</span>
    <span class="opt-title">Free Distortion Audit</span>
    <p class="opt-desc">One campaign. 30-day lookback. We run your real platform data through the full CDAI cost stack and show you the gap — not a manual estimate. No commitment required.</p>
    <a class="btn" href="https://alloceraintelligence.com/#intake">Request Free Audit &rarr;</a>
  </div>
  <div class="option">
    <span class="opt-lbl">Option 02 — Full Access</span>
    <span class="opt-title">CDAI Retainer</span>
    <p class="opt-desc">CDAI connects to your ad accounts, CRM, and lead distribution stack. One directive per campaign, every morning — Scale, Hold, Cut, Pause, Quarantine, Renegotiate, Investigate, or Flag — based on your true margin after every cost layer. Automated, continuous, no spreadsheets.</p>
    <a class="btn-gold" href="https://alloceraintelligence.com/#intake">Learn More &rarr;</a>
  </div>
  <div class="newsletter">
    <p class="nl-title">The Margin Gap — Free Newsletter</p>
    <p class="nl-desc">Marketing margin intelligence for paid acquisition operators. The cost layers your dashboard never shows. Every two weeks, free.</p>
    <a class="btn-gold" href="https://allocera-intelligence.beehiiv.com/subscribe" target="_blank">Subscribe Free &rarr;</a>
  </div>
  <div class="footer">
    Allocera Intelligence &middot; alloceraintelligence.com<br>
    You're receiving this because you used the Distortion Calculator.<br>
    Reply to this email or call 919-610-9288 to speak with Nick directly.<br>
    <a href="https://allocera-intelligence.beehiiv.com/unsubscribe">Unsubscribe</a>
  </div>
</div>
</body>
</html>"""

        resend.Emails.send({
            "from": "directives@alloceraintelligence.com",
            "reply_to": "nick@alloceraintelligence.com",
            "to": email,
            "subject": subject,
            "html": html,
        })
        logger.info(f"[CALC] Results email sent to {email} ({company})")
        return True
    except Exception as e:
        logger.error(f"[CALC] Results email failed for {email}: {e}")
        return False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/calculator-view")
def log_view(payload: ViewPing):
    """
    Anonymous funnel-entry ping — fired once when the calculator panel
    scrolls into view, before any name/company/email is entered. No PII.
    Lets us measure viewed -> started (OTP requested) -> verified (lead)
    instead of only ever seeing the last stage.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        _ensure_views_table(cur)
        cur.execute(
            "INSERT INTO calculator_views (session_id) VALUES (%s)",
            (payload.session_id,)
        )
        conn.commit()
        return {"status": "ok"}
    except Exception as e:
        conn.rollback()
        logger.error(f"[CALC] Failed to log view: {e}")
        # Never let tracking failure surface to the visitor.
        return {"status": "ok"}
    finally:
        conn.close()


@router.post("/calculator-send-code")
def send_code(payload: SendCodeRequest):
    """
    Step 1 — gate. Generate a 6-digit OTP, store it, email it.
    Nothing is saved to calculator_leads until the code is verified.
    """
    email   = payload.email.strip().lower()
    name    = payload.name.strip()
    company = payload.company.strip()

    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")

    code = str(random.randint(0, 999999)).zfill(6)

    conn = get_conn()
    cur  = conn.cursor()
    try:
        # Note: previously this deleted prior unverified codes for this email
        # before inserting the new one. That silently destroyed the only
        # record that a funnel-abandon (requested code, never verified) ever
        # happened. Old unverified rows are now kept — they're cheap, and
        # expires_at already lets us tell "abandoned" from "still active".
        cur.execute("""
            INSERT INTO calculator_otps (email, name, company, code, expires_at)
            VALUES (%s, %s, %s, %s, NOW() + INTERVAL '10 minutes')
        """, (email, name, company, code))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[CALC] Failed to store OTP for {email}: {e}")
        raise HTTPException(status_code=500, detail="Failed to send code")
    finally:
        conn.close()

    sent = send_otp_email(name, email, code)
    if not sent:
        raise HTTPException(status_code=500, detail="Failed to send code email")

    logger.info(f"[CALC] OTP sent: {email}")
    return {"status": "sent"}


@router.post("/calculator-lead")
def create_calculator_lead(payload: CalcLeadCreate):
    """
    Step 2 — verify OTP, create the lead row, subscribe Beehiiv, return {id}.
    Code must match and not be expired.
    """
    email   = payload.email.strip().lower()
    code    = payload.code.strip()

    conn = get_conn()
    cur  = conn.cursor()
    try:
        # Verify the OTP
        cur.execute("""
            SELECT id FROM calculator_otps
            WHERE email = %s
              AND code = %s
              AND verified = FALSE
              AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT 1
        """, (email, code))
        otp = cur.fetchone()

        if not otp:
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid or expired code")

        # Mark OTP as verified
        cur.execute(
            "UPDATE calculator_otps SET verified = TRUE WHERE id = %s",
            (otp["id"],)
        )

        # Create the lead row
        lead_id = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO calculator_leads
                (id, name, company, email, inputs, results,
                 email_sent, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
        """, (
            lead_id,
            payload.name.strip(),
            payload.company.strip(),
            email,
            json.dumps(payload.inputs or {}),
            json.dumps(payload.results or {}),
        ))
        conn.commit()
        logger.info(f"[CALC] Verified + lead created: {email} / {payload.company}")

        # Subscribe to Beehiiv (fire and forget)
        _subscribe_beehiiv(email)

        return {"id": lead_id}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[CALC] Failed to verify/create lead for {email}: {e}")
        raise HTTPException(status_code=500, detail="Verification failed")
    finally:
        conn.close()


@router.patch("/calculator-lead/{lead_id}")
def update_calculator_lead(lead_id: str, payload: CalcLeadUpdate):
    """
    Live save — called ~900ms after the user stops typing.
    Sends the results email once when there is meaningful data.
    Email is already verified at this point so no numeric threshold needed.
    """
    conn = get_conn()
    cur  = conn.cursor()
    try:
        cur.execute("""
            UPDATE calculator_leads
            SET inputs = %s, results = %s, updated_at = NOW()
            WHERE id = %s
            RETURNING name, company, email, email_sent
        """, (
            json.dumps(payload.inputs or {}),
            json.dumps(payload.results or {}),
            lead_id,
        ))
        row = cur.fetchone()
        conn.commit()

        if not row:
            raise HTTPException(status_code=404, detail="Lead not found")

        r = payload.results or {}
        i = payload.inputs or {}

        # Send results email once when there's real data.
        # Email is already verified so no spend/lead minimums needed.
        should_email = (
            not row["email_sent"]
            and i.get("adSpend", 0) > 0
            and i.get("leads", 0) > 0
            and r.get("trueCpl", 0) > 0
        )

        if should_email:
            sent = send_results_email(
                row["name"], row["company"], row["email"], r, i
            )
            if sent:
                cur2 = conn.cursor()
                cur2.execute(
                    "UPDATE calculator_leads SET email_sent = TRUE WHERE id = %s",
                    (lead_id,)
                )
                conn.commit()

        return {"status": "ok"}

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logger.error(f"[CALC] Failed to update lead {lead_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update lead")
    finally:
        conn.close()
