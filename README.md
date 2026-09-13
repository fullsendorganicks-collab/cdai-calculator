# cdai-calculator

Standalone lead-capture microservice for [Allocera Intelligence](https://alloceraintelligence.com)'s margin distortion calculator — deployed independently of the main CDAI engine, with its own isolated database schema.

## What it does

- **OTP-gated capture** — visitor pings an anonymous view event, requests a 6-digit email code, verifies it, then gets a lead row created. No PII is stored before the email is verified.
- **Live save** — the calculator's results are saved incrementally (`PATCH /calculator-lead/{id}`) as the visitor fills it in, and a results email sends automatically once the data is complete — no separate submit step required.
- **Newsletter integration** — verified leads are subscribed to the Margin Gap newsletter via the Beehiiv API.
- **Fully isolated** — reads and writes only its own three tables (`calculator_leads`, `calculator_otps`, `calculator_views`). No access to the main engine's org/lead/cost tables, so a bug here can't touch production client data.

## Endpoints

```
GET   /health
POST  /calculator-view              anonymous funnel-entry ping, no PII
POST  /calculator-send-code         send 6-digit OTP to email
POST  /calculator-lead              verify OTP, create row, subscribe to Beehiiv
PATCH /calculator-lead/{lead_id}    live save; sends results email once complete
```

## Stack

Python · FastAPI · PostgreSQL · Beehiiv API

## Setup

```bash
pip install -r requirements.txt
# requires DATABASE_URL, BEEHIIV_API_KEY, BEEHIIV_PUBLICATION_ID
uvicorn main:app --reload
```

Shares its `DATABASE_URL` and email-sending credentials with the main CDAI engine but has no other dependency on it — it can be deployed, scaled, and taken down independently.
