"""
CDAI Distortion Calculator — Standalone FastAPI service.
Deployed separately from the main CDAI engine.
Shares DATABASE_URL and RESEND_API_KEY with the engine — no other dependencies.

Endpoints:
  GET  /health
  POST /calculator-view
  POST /calculator-send-code
  POST /calculator-lead
  PATCH /calculator-lead/{lead_id}
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from calculator_leads import router as calculator_router

MAX_BODY_BYTES = 65536


class BodyLimitMiddleware:
    """Pure-ASGI request body cap, applied BEFORE FastAPI/pydantic read the body.

    * A declared Content-Length above the limit is answered 413 without running the app.
    * A body with no (or a false) Content-Length, e.g. chunked, is counted as it streams; the first byte over the limit stops delivery to the app
      (the app sees a disconnect, so nothing is parsed) and the response becomes 413, whatever the app tried to answer.
    Sits INSIDE CORS (added first), so the 413 still carries the CORS headers the browser needs to read it."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    @staticmethod
    async def _reject(send):
        body = b'{"detail":"Request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    declared = self.max_bytes + 1       # unparseable length: refuse
                if declared > self.max_bytes:
                    return await self._reject(send)
        received = 0
        too_large = False
        started = False
        rejected = False

        async def limited_receive():
            nonlocal received, too_large
            if too_large:
                return {"type": "http.disconnect"}
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    return {"type": "http.disconnect"}
            return msg

        async def guarded_send(msg):
            nonlocal started, rejected
            if too_large and not rejected:
                if not started:
                    rejected = started = True
                    await self._reject(send)
                return
            if rejected:
                return
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if too_large and not started:
                started = rejected = True
                await self._reject(send)
                return
            if too_large:
                return
            raise
        if too_large and not started:
            await self._reject(send)


app = FastAPI(title="CDAI Distortion Calculator", version="1.0")

# Added first = innermost. CORS is added after, so it wraps the body cap and its headers reach the 413.
app.add_middleware(BodyLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://alloceraintelligence.com",
        "https://www.alloceraintelligence.com",
    ],
    allow_credentials=False,           # no cookies or HTTP auth are used by the calculator
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(calculator_router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "cdai-calculator"}
