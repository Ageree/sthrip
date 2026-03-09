# Sthrip Python SDK Production Image
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Production image
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r sthrip && useradd -r -g sthrip sthrip

# Copy dependencies from builder
COPY --from=builder /root/.local /home/sthrip/.local
ENV PATH=/home/sthrip/.local/bin:$PATH

# Copy application
COPY sthrip/ ./sthrip/
COPY setup.py .
COPY README.md .

# Install package
RUN pip install --no-cache-dir -e . && \
    chown -R sthrip:sthrip /app && \
    chown -R sthrip:sthrip /home/sthrip

# Switch to non-root user
USER sthrip

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "from sthrip import Sthrip; print('OK')" || exit 1

# Labels
LABEL maintainer="dev@sthrip.io" \
      version="0.1.0" \
      description="Anonymous payments SDK for AI Agents"

CMD ["python", "-c", "print('Sthrip SDK v0.1.0 - Ready for import: from sthrip import Sthrip')"]
