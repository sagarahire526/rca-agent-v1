"""
FastAPI application entry point for the RCA Agent system.

Run:
    uvicorn app.main:app --host 0.0.0.0 --port 8000

Swagger UI:  http://localhost:8000/docs   (development only — see ENV below)

Security posture
----------------
* Every /api/v1 route requires HTTP Basic credentials (see api/deps.py).
* Interactive docs and the OpenAPI schema are served only when ENV is not a
  deployed environment — the penetration test used /docs to enumerate the API.
* CORS is restricted to ALLOWED_ORIGINS; the previous "*" allowed any website
  to issue cross-origin requests from a user's browser.
* Unhandled exceptions return a generic body with a correlation id. Raw
  exception text used to reach clients and carried database hostnames, schema
  names and column names.
"""
from __future__ import annotations

import logging
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

# Ensure app/ is on sys.path so internal imports (api, services, agents, …)
# resolve correctly whether we run  `uvicorn app.main:app`  from the project
# root or  `uvicorn main:app`  from inside app/.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders

import config
from api.v1.router import router as v1_router
import services.db_service as db_svc

# ── Logging setup ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ── Keep the SSE/chart ?token= credential out of access logs ───────────────

_TOKEN_QS = re.compile(r"([?&]token=)[^&\s]+")


class _RedactTokenFilter(logging.Filter):
    """
    Replace the value of any `token=` query parameter with `<redacted>` in
    uvicorn's access log line. Browser-loaded routes pass credentials in the
    URL because EventSource and iframes cannot set headers; without this they
    would be written to disk in cleartext on every request.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(
                _TOKEN_QS.sub(r"\1<redacted>", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_QS.sub(r"\1<redacted>", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactTokenFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.API_USERNAME or not config.API_PASSWORD:
        # Loud, but non-fatal: the auth dependency already fails closed, so the
        # API rejects every request. This makes the misconfiguration obvious in
        # the startup logs instead of only in 401 responses.
        logger.error(
            "API_USERNAME / API_PASSWORD are not set — the API will reject ALL "
            "requests with 401 until they are configured."
        )
    if config.IS_PRODUCTION and not config.ALLOWED_ORIGINS:
        logger.warning(
            "ALLOWED_ORIGINS is empty — browser clients will be blocked by CORS."
        )
    db_svc.init_pool()
    db_svc.ensure_tables()
    yield
    db_svc.close_pool()


# Docs are an enumeration aid: the pentest report's repro steps for three
# findings begin with "go to the /docs". Off in deployed environments.
_docs_enabled = not config.IS_PRODUCTION

app = FastAPI(
    lifespan=lifespan,
    title="RCA Agent API",
    description="LangGraph multi-agent Root Cause Analysis system backed by Neo4j and PostgreSQL.",
    version="1.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Security response headers ──────────────────────────────────────────────

_BASE_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-XSS-Protection": "0",  # deprecated and unsafe when enabled; CSP replaces it
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}

# JSON API responses never need to load or embed anything.
_API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _chart_csp() -> str:
    """
    CSP for the chart preview page, which loads Highcharts from its CDN,
    applies inline styles, and runs one inline script block.

    `frame-ancestors` follows ALLOWED_ORIGINS so the dashboard can embed the
    page while every other origin is refused. With no origins configured it
    falls back to 'self' rather than opening up.
    """
    ancestors = " ".join(config.ALLOWED_ORIGINS) if config.ALLOWED_ORIGINS else "'self'"
    return (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline' https://code.highcharts.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        f"frame-ancestors {ancestors}"
    )


class SecurityHeadersMiddleware:
    """
    Attach security headers at `http.response.start`.

    Written as raw ASGI rather than BaseHTTPMiddleware on purpose: the latter
    wraps the response body in an anyio stream, which is a known source of
    buffering and back-pressure problems for Server-Sent Events. This version
    touches only the header frame and leaves /analyze/stream's body untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for header, value in _BASE_SECURITY_HEADERS.items():
                    if header not in headers:
                        headers[header] = value

                # Uvicorn advertises its version; that is free reconnaissance.
                headers["Server"] = "rca-agent"

                if "Content-Security-Policy" not in headers:
                    is_html = headers.get("content-type", "").startswith("text/html")
                    headers["Content-Security-Policy"] = _chart_csp() if is_html else _API_CSP
                    if not is_html and "X-Frame-Options" not in headers:
                        headers["X-Frame-Options"] = "DENY"

            await send(message)

        await self.app(scope, receive, send_with_headers)


# Added after CORSMiddleware so it sits outermost and stamps every response,
# including CORS preflights.
app.add_middleware(SecurityHeadersMiddleware)


# ── Generic error handling ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    Return a generic body and log the detail server-side. The reference id
    lets support correlate a user report with the stack trace without the
    stack trace ever crossing the network.
    """
    reference = uuid.uuid4().hex[:12]
    logger.exception(
        "Unhandled error ref=%s method=%s path=%s", reference, request.method, request.url.path
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "reference": reference},
    )


@app.get("/", tags=["Root"])
async def root():
    payload = {"service": "RCA Agent", "version": "1.0.0"}
    if _docs_enabled:
        payload["docs"] = "/docs"
    return payload


app.include_router(v1_router, prefix="/api")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        timeout_keep_alive=300,
        server_header=False,
    )
