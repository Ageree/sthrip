FROM python:3.11-slim

WORKDIR /app

# Install runtime deps only — no compilers, no shell utils, smallest possible image.
RUN pip install --no-cache-dir \
        "fastapi==0.115.2" \
        "uvicorn[standard]==0.30.6" \
        "cryptography==43.0.1" \
        "pydantic==2.9.2"

COPY server.py ./
COPY entrypoint.sh ./
RUN chmod +x ./entrypoint.sh

# Run as unprivileged user — the keystore process never writes to disk
# beyond its log stream and never needs root.
RUN useradd --create-home --shell /usr/sbin/nologin keystore
USER keystore

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]
