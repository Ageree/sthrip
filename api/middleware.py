"""Middleware configuration for the Sthrip API."""

import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from sthrip.logging_config import generate_request_id, request_id_var
from sthrip.services.metrics import http_requests_total, http_request_duration

MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MB


def configure_middleware(app: FastAPI) -> None:
    """Apply all middleware to the app."""

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        import re as _re
        raw_rid = request.headers.get("x-request-id", "")
        sanitized = _re.sub(r'[^a-zA-Z0-9\-_]', '', raw_rid)[:64]
        rid = sanitized or generate_request_id()
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.middleware("http")
    async def track_metrics(request: Request, call_next):
        import time as _time
        start = _time.perf_counter()
        response = await call_next(request)
        duration = _time.perf_counter() - start
        endpoint = request.url.path
        http_requests_total.labels(request.method, endpoint, response.status_code).inc()
        http_request_duration.labels(request.method, endpoint).observe(duration)
        return response

    @app.middleware("http")
    async def limit_request_body(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large. Maximum size is 1 MB."},
            )
        return await call_next(request)

    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Trust proxy headers when running behind reverse proxy
    if os.getenv("ENVIRONMENT", "production") != "dev":
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        trusted_hosts = os.getenv("TRUSTED_PROXY_HOSTS", "127.0.0.1").split(",")
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


def _get_cors_origins() -> list:
    """Build CORS origins list. Rejects all by default unless configured."""
    env = os.getenv("ENVIRONMENT", "production")
    configured = os.getenv("CORS_ORIGINS", "")
    origins = [o.strip() for o in configured.split(",") if o.strip()]
    if env == "dev":
        origins.extend([
            "http://localhost:3000", "http://localhost:8000",
            "http://127.0.0.1:3000", "http://127.0.0.1:8000",
        ])
    return origins
