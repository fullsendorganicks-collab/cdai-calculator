"""
Calculator Leads — Distortion Calculator lead capture endpoints.

POST  /calculator-lead            Gate unlock. Creates row, returns {id}.
PATCH /calculator-lead/{lead_id}  Live save as user types. Sends results
                                  email once when meaningful data is present.

Completely isolated — reads/writes only the calculator_leads table.
No access to any engine tables (orgs, lead_events, cost_events, etc).
"""

import os
import json
import uuid
import logging
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("cdai.calculator_leads")

DB_URL = os.getenv("DATABASE_URL")

router = APIRouter()


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)


# ── Pydantic models ──────────────────────────────────────────────────────────

class CalcLeadCreate(BaseModel):
    name: str
    company: str
    email: str
    inputs: Optional[dict] = None
    results: Optional[dict] = None


class CalcLeadUpdate(BaseModel):
    inputs: Optional[dict] = None
    results: Optional[dict] = None


# ── Email ────────────────────────────────────────────────────────────────────

def _fmt(n: float) -> str:
    return f"${n:,.2f}"


def _fmt_r(n: float) -> str:
    return f"${round(n):,}"


def send_results_email(name: str, company: str, email: str,
                       results: dict, inputs: dict) -> bool:
    """Send distortion results email to the lead via Resend."""
    try:
        import resend
        api_key = os.getenv("RESEND_API_KEY")
        if not api_key:
            logger.error("[CALC] RESEND_API_KEY not set — cannot send email")
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
  table.cpl{{width:100%;border-collapse:separate;border-spacing:10px 0;
             margin-bottom:24px}}
  .cpl-rep{{background:#071530;border:1px solid #1A3060;padding:20px;
            border-radius:2px;width:50%;vertical-align:top}}
  .cpl-tru{{background:rgba(214,48,80,.06);
            border:1px solid rgba(214,48,80,.35);padding:20px;
            border-radius:2px;width:50%;vertical-align:top}}
  .cpl-lbl{{font-family:'Courier New',monospace;font-size:9px;
            letter-spacing:.14em;text-transform:uppercase;
            display:block;margin-bottom:8px}}
  .cpl-rep .cpl-lbl{{color:#4A6488}}
  .cpl-tru .cpl-lbl{{color:#D63050}}
  .cpl-val{{font-family:'Courier New',monospace;font-size:34px;
            font-weight:700;display:block;line-height:1}}
  .cpl-rep .cpl-val{{color:#9AAECE}}
  .cpl-tru .cpl-val{{color:#D63050}}
  table.stats{{width:100%;border-collapse:separate;border-spacing:10px 0;
               margin-bottom:24px}}
  .stat-cell{{background:#071530;border:1px solid #102040;
              padding:16px;border-radius:2px;width:50%;vertical-align:top}}
  .stat-lbl{{font-family:'Courier New',monospace;font-size:9px;
             letter-spacing:.12em;text-transform:uppercase;
             color:#4A6488;display:block;margin-bottom:6px}}
  .stat-val{{font-family:'Courier New',monospace;font-size:22px;font-weight:700}}
  .gap-box{{background:rgba(22,85,216,.08);
            border:1px solid rgba(22,85,216,.25);
            border-left:3px solid #1655D8;padding:16px 20px;
            margin-bottom:32px;font-size:14px;color:#B8C9E0;
            line-height:1.7;border-radius:2px}}
  .gap-box b{{color:#4080F0;font-family:'Courier New',monospace}}
  hr{{border:none;border-top:1px solid #102040;margin:32px 0}}
  .sec-title{{font-family:'Courier New',monospace;font-size:10px;
              letter-spacing:.18em;text-transform:uppercase;
              color:#C4A040;margin-bottom:16px;display:block}}
  .option{{background:#071530;border:1px solid #1A3060;
           padding:20px 24px;margin-bottom:12px;border-radius:2px}}
  .opt-lbl{{font-family:'Courier New',monospace;font-size:9px;
            letter-spacing:.14em;text-transform:uppercase;
            color:#4080F0;display:block;margin-bottom:8px}}
  .opt-title{{font-size:15px;font-weight:600;color:#EBF0FF;
              display:block;margin-bottom:6px}}
  .opt-desc{{font-size:13px;color:#7A95B8;line-height:1.6;margin-bottom:14px}}
  .btn{{display:inline-block;font-family:'Courier New',monospace;
        font-size:11px;letter-spacing:.1em;text-transform:uppercase;
        background:#1655D8;color:#EBF0FF;padding:12px 24px;
        border-radius:2px;text-decoration:none;font-weight:600}}
  .btn-gold{{display:inline-block;font-family:'Courier New',monospace;
             font-size:11px;letter-spacing:.1em;text-transform:uppercase;
             background:transparent;color:#C4A040;
             border:1px solid rgba(196,160,64,.5);
             padding:12px 24px;border-radius:2px;text-decoration:none}}
  .newsletter{{background:rgba(196,160,64,.06);
               border:1px solid rgba(196,160,64,.2);
               border-top:2px solid #C4A040;
               padding:20px 24px;margin-top:28px;border-radius:2px}}
  .nl-title{{font-size:15px;color:#EBF0FF;margin:0 0 6px;font-weight:600}}
  .nl-desc{{font-size:13px;color:#7A95B8;line-height:1.6;margin:0 0 14px}}
  .footer{{margin-top:40px;font-family:'Courier New',monospace;
           font-size:9px;letter-spacing:.07em;color:#4A6488;line-height:1.9}}
  .footer a{{color:#4A6488}}
</style>
</head>
<body>
<div class="wrap">

  <span class="eyebrow">Allocera Intelligence — Distortion Calculator Results</span>
  <h1>Your true CPL for {company}</h1>
  <p class="sub">
    Here's what the calculator found — the gap between what your ad platform
    reports and what you're actually paying after every hidden cost layer.
  </p>

  <table class="cpl"><tr>
    <td class="cpl-rep">
      <span class="cpl-lbl">What your dashboard reports</span>
      <span class="cpl-val">{_fmt(reported_cpl)}</span>
    </td>
    <td class="cpl-tru">
      <span class="cpl-lbl">What you're actually paying</span>
      <span class="cpl-val">{_fmt(true_cpl)}</span>
    </td>
  </tr></table>

  <table class="stats"><tr>
    <td class="stat-cell">
      <span class="stat-lbl">True contribution margin</span>
      <span class="stat-val" style="color:{cm_color}">{cm_sign}{cm_pct:.1f}%</span>
    </td>
    <td class="stat-cell">
      <span class="stat-lbl">Hidden costs / month</span>
      <span class="stat-val" style="color:#EBF0FF">{_fmt_r(hidden_costs)}</span>
    </td>
  </tr></table>

  <div class="gap-box">
    Your true cost per lead is <b>{gap_abs:.0f}% {gap_dir}</b> than your
    dashboard reports — a gap of <b>{_fmt(cpl_diff)} per lead</b>,
    or <b>{_fmt_r(hidden_costs)}/month</b> on this campaign alone.
    <br><br>
    This was one campaign, entered manually. CDAI tracks this automatically
    across every campaign, every morning — connected live to your ad accounts
    and CRM, with no manual entry.
  </div>

  <hr>
  <span class="sec-title">What to do next</span>

  <div class="option">
    <span class="opt-lbl">Option 01 — Free</span>
    <span class="opt-title">Free Distortion Audit</span>
    <p class="opt-desc">
      One campaign. 30-day lookback. We run your real platform data through
      the full CDAI cost stack and show you the gap — not a manual estimate.
      No commitment required.
    </p>
    <a class="btn" href="https://alloceraintelligence.com/#intake">
      Request Free Audit &rarr;
    </a>
  </div>

  <div class="option">
    <span class="opt-lbl">Option 02 — Full Access</span>
    <span class="opt-title">CDAI Retainer</span>
    <p class="opt-desc">
      CDAI connects to your ad accounts, CRM, and lead distribution stack.
      One directive per campaign, every morning — Scale, Hold, Cut, or Stop —
      based on your true margin after every cost layer. Automated, continuous,
      no spreadsheets.
    </p>
    <a class="btn-gold" href="https://alloceraintelligence.com/#intake">
      Learn More &rarr;
    </a>
  </div>

  <div class="newsletter">
    <p class="nl-title">The Margin Gap — Free Newsletter</p>
    <p class="nl-desc">
      Marketing margin intelligence for paid acquisition operators.
      The cost layers your dashboard never shows. Every two weeks, free.
    </p>
    <a class="btn-gold"
       href="https://allocera-intelligence.beehiiv.com/subscribe"
       target="_blank">Subscribe Free &rarr;</a>
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
        logger.error(f"[CALC] Email send failed for {email}: {e}")
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/calculator-lead")
def create_calculator_lead(payload: CalcLeadCreate):
    """
    Gate unlock. Creates a calculator_leads row and returns the id.
    The browser stores this id and sends it on every subsequent PATCH.
    """
    lead_id = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO calculator_leads
                (id, name, company, email, inputs, results,
                 email_sent, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, FALSE, NOW(), NOW())
        """, (
            lead_id,
            payload.name.strip(),
            payload.company.strip(),
            payload.email.strip().lower(),
            json.dumps(payload.inputs or {}),
            json.dumps(payload.results or {}),
        ))
        conn.commit()
        logger.info(f"[CALC] New lead: {payload.email} / {payload.company}")
        return {"id": lead_id}
    except Exception as e:
        conn.rollback()
        logger.error(f"[CALC] Failed to create lead: {e}")
        raise HTTPException(status_code=500, detail="Failed to save lead")
    finally:
        conn.close()


@router.patch("/calculator-lead/{lead_id}")
def update_calculator_lead(lead_id: str, payload: CalcLeadUpdate):
    """
    Live save. Called ~900ms after the user stops typing.
    Updates inputs + results. Sends the results email exactly once
    when adSpend > 0, leads > 0, and trueCpl > 0.
    """
    conn = get_conn()
    cur = conn.cursor()
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
        should_email = (
            not row["email_sent"]
            and r.get("trueCpl", 0) > 0
            and i.get("adSpend", 0) > 0
            and i.get("leads", 0) > 0
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
