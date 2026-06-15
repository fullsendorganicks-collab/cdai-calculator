"""
CDAI Distortion Calculator — Standalone FastAPI service.
Deployed separately from the main CDAI engine.
Shares DATABASE_URL and RESEND_API_KEY with the engine — no other dependencies.

Endpoints:
  GET  /health
  POST /calculator-lead
  PATCH /calculator-lead/{lead_id}
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from calculator_leads import router as calculator_router

app = FastAPI(title="CDAI Distortion Calculator", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://alloceraintelligence.com",
        "https://www.alloceraintelligence.com",
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(calculator_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "cdai-calculator"}
