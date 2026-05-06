#!/bin/sh
set -eu

PORT="${PORT:-8000}"

echo "[sthrip-op-keystore] starting on 0.0.0.0:${PORT}"

exec uvicorn server:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --access-log
