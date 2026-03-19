"""Middleware configuration for the Sthrip API."""

import re as _re
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from sthrip.config import get_settings
from sthrip.logging_config import generate_request_id, request_id_var
from sthrip.services.metrics import http_requests_total, http_request_duration

_UUID_RE = _re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    _re.IGNORECASE,
)
_DYNAMIC_SEGMENT_RE = _re.compile(
    r'/(?=[0-9a-f\-]*[0-9])[0-9a-f\-]{20,}(?=/|$)',
    _re.IGNORECASE,
)


def _normalize_path(path: str) -> str:
    """Replace dynamic path segments (UUIDs, long IDs) with {id}."""
    path = _UUID_RE.sub("{id}", path)
    path = _DYNAMIC_SEGMENT_RE.sub("/{id}", path)
    return path

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
        endpoint = _normalize_path(request.url.path)
        http_requests_total.labels(request.method, endpoint, response.status_code).inc()
        http_request_duration.labels(request.method, endpoint).observe(duration)
        return response

    @app.middleware("http")
    async def limit_request_body(request: Request, call_next):
        # Fast path: check Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large. Maximum size is 1 MB."},
                    )
            except ValueError:
                pass

        # For mutation requests without Content-Length (e.g. chunked transfers),
        # stream the body and reject as soon as the limit is exceeded so we never
        # buffer more than MAX_REQUEST_BODY_BYTES + 1 chunk worth of data.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not content_length:
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large. Maximum size is 1 MB."},
                    )
                chunks.append(chunk)

            # Re-inject the fully-read body so downstream handlers can read it
            body = b"".join(chunks)

            async def _receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = _receive  # noqa: SLF001

        return await call_next(request)

    app.add_middleware(GZipMiddleware, minimum_size=1000)

    # Trust proxy headers when running behind reverse proxy
    if get_settings().environment != "dev":
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
        trusted_hosts = get_settings().trusted_proxy_hosts.split(",")
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_hosts)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    _STRICT_CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    _DOCS_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.redoc.ly; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.redoc.ly https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.redoc.ly; "
        "img-src 'self' data: https://cdn.jsdelivr.net https://cdn.redoc.ly; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["X-XSS-Protection"] = "0"  # Modern: disable buggy filter, CSP protects
        if request.url.path.startswith("/docs"):
            response.headers["Content-Security-Policy"] = _DOCS_CSP
        else:
            response.headers["Content-Security-Policy"] = _STRICT_CSP
        return response


def _get_cors_origins() -> list:
    """Build CORS origins list. Rejects all by default unless configured."""
    env = get_settings().environment
    configured = get_settings().cors_origins
    origins = [o.strip() for o in configured.split(",") if o.strip()]
    if env == "dev":
        origins.extend([
            "http://localhost:3000", "http://localhost:8000",
            "http://127.0.0.1:3000", "http://127.0.0.1:8000",
        ])
    return origins
