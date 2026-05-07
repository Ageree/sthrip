# syntax=docker/dockerfile:1.6
#
# sthrip-payment-tee — Phase 3 Sprint 5
#
# Multi-stage build for the payment hot-path service. Runs inside a GCP
# Confidential VM (AMD SEV-SNP). The image MUST stay narrow:
#
# * No marketplace, escrow, MCP, SLA, reviews, channels, swaps, stablecoin,
#   admin UI, Tor sidecar, or test code.
# * Non-root runtime user.
# * Listens on 8080 only.
# * Image goal: < 300 MB final.

# ---------------------------------------------------------------------------
# Builder stage — install dependencies into a virtualenv we copy forward.
# ---------------------------------------------------------------------------
FROM python:3.9-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build tools needed by some wheels (cryptography, msgpack-c). The
# runtime stage will NOT carry these.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libssl-dev \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install only the payment-hot-path Python deps. We keep this list explicit
# rather than copying the full requirements.lock so that adding a marketplace
# dependency upstream cannot accidentally inflate the TEE image.
COPY gcp/payment_tee_deploy/requirements.txt /build/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /build/requirements.txt

# ---------------------------------------------------------------------------
# Runtime stage — slim image with the venv + only the payment code.
# ---------------------------------------------------------------------------
FROM python:3.9-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    LOG_LEVEL=INFO \
    TEE_ENFORCE_BOUNDARY=1

# Add a non-root user. uid:gid 10001:10001 to match GCP's recommended
# unprivileged range and avoid collisions with host UIDs.
RUN groupadd --system --gid 10001 sthrip \
    && useradd --system --uid 10001 --gid sthrip --no-create-home sthrip

WORKDIR /app

# Copy the virtualenv from the builder.
COPY --from=builder /opt/venv /opt/venv

# Copy ONLY what the TEE service needs:
#   * gcp/payment_tee_deploy/* — the service entrypoint + import_guard.
#   * sthrip/                  — backend modules referenced by the service.
#   * api/__init__.py + minimal helpers if needed (currently zero).
# Everything else (api/admin_ui, api/routers/marketplace, etc.) is excluded
# by .dockerignore.
COPY --chown=sthrip:sthrip gcp/payment_tee_deploy /app/gcp/payment_tee_deploy
COPY --chown=sthrip:sthrip sthrip                  /app/sthrip
# Stub package marker so ``import gcp`` resolves.
COPY --chown=sthrip:sthrip gcp/__init__.py         /app/gcp/__init__.py

USER sthrip:sthrip

EXPOSE 8080

# OCI labels for supply-chain provenance.
LABEL org.opencontainers.image.source="https://github.com/Ageree/sthrip" \
      org.opencontainers.image.title="sthrip-payment-tee" \
      org.opencontainers.image.description="Phase 3 TEE payment hot-path service (GCP Confidential VM)" \
      org.opencontainers.image.licenses="MIT"

# Default to gunicorn-managed uvicorn workers in production-mode for
# graceful reloads + worker recycling. The TEE VM has 2 vCPU (n2d-standard-2),
# so 2 workers gives clean parallelism without thrashing.
CMD ["uvicorn", "gcp.payment_tee_deploy.payment_service:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "2", \
     "--no-access-log"]
