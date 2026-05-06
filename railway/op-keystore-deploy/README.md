# sthrip-op-keystore — operator runbook

A small FastAPI + AES-GCM service deployed as a sibling Railway service
to the Sthrip API. It holds the operator key encryption key (`KEK_OP`)
and exposes only `wrap` / `unwrap` over the Railway private network.

The hub never sees `KEK_OP` plaintext. Combined with the hub's own
`STHRIP_HUB_KEK`, this enforces the Sprint 3/4b invariant that recovering
the encrypted payment graph requires compromising **both** services
independently — neither alone is enough.

Behind the `OP_KEYSTORE_MODE=remote` switch on the API service (default
`stub`) so we can land code without flipping any user-visible behaviour.

## What it builds

- `Dockerfile`     — `python:3.11-slim` + `fastapi` + `uvicorn` +
  `cryptography` + `pydantic`. Runs as the unprivileged `keystore` user.
- `server.py`      — three endpoints: `POST /wrap`, `POST /unwrap`,
  `GET /health`. Bearer auth via `AUTH_TOKEN`.
- `entrypoint.sh`  — starts uvicorn bound to `0.0.0.0:${PORT}`.

## First-time deploy

1. **Generate `KEK_OP`**:
   ```bash
   python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
   ```
   Copy the output. **This is the operator KEK — store it in a separate
   secret manager from the hub. Loss of this key means the encrypted
   payment graph is unrecoverable; leak of this key + the hub KEK means
   the graph is recoverable by the holder of both.**
2. **Generate the bearer auth token** (also 32 random bytes):
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
3. **Create the Railway service** named `sthrip-op-keystore` in the
   `sthrip` project with **a private (internal-only) network**. Do NOT
   attach the project's `Postgres` or any other database.
4. Set service env vars:
   - `KEK_OP_BASE64` — output from step 1.
   - `AUTH_TOKEN`    — output from step 2.
   - `PORT` — leave to Railway default (Railway injects it).
5. Build with this directory as context (`railway/op-keystore-deploy/`).
6. After first deploy, watch the logs for:
   ```
   [sthrip-op-keystore] starting on 0.0.0.0:8000
   ```
7. **Wire the API service**:
   - `OP_KEYSTORE_AUTH_TOKEN` — the same token from step 2.
   - `OP_KEYSTORE_URL` — leave empty to use the default
     `http://sthrip-op-keystore.railway.internal:8000`, or override.
   - Keep `OP_KEYSTORE_MODE=stub` for now. Flipping to `remote` is the
     cutover (see below).

## ACL

The Railway service is internal-only — no public domain. Only services
on the same Railway project can reach it. Combined with bearer auth,
two layers protect against accidental exposure.

The keystore container has no access to:
- `DATABASE_URL`
- `REDIS_URL`
- The Sthrip API source tree
- The Tor sidecar volume

It sees only `KEK_OP_BASE64` and `AUTH_TOKEN`. This is deliberate.

## Cutover from stub to remote

Run only after the dual-write era has been live for at least one full
backup cycle and the Sprint 4a backfill has been verified.

1. Deploy `sthrip-op-keystore` (above).
2. Set `OP_KEYSTORE_AUTH_TOKEN` on the API service.
3. Set `OP_KEYSTORE_MODE=remote` on the API service. New writes go via
   the remote keystore. (Reads still work because the legacy plaintext
   FK columns are still populated.)
4. Run `scripts/backfill_payment_envelope.py` until it reports zero
   NULL `participant_envelope` rows.
5. Set `STHRIP_READ_FROM_ENVELOPE=true` on the API service. Soak for
   24 h. Watch the audit log for `fallback_decrypt_error`.
6. Only then set `STHRIP_DROP_LEGACY_FK=true` and run
   `alembic upgrade head` (the Sprint 4b destructive migration). Once
   the columns are gone, the data is only readable through the
   envelope; this is the point of no return.

## Key rotation

To rotate `KEK_OP`:
1. Generate a new base64 key (step 1 above).
2. Run the (Sprint 7) rewrap script to re-seal every envelope's
   `dek_wrap_op` under the new key. **Do NOT** swap `KEK_OP_BASE64`
   without rewrapping first — every existing envelope becomes
   permanently undecryptable.
3. Once rewrap completes, swap `KEK_OP_BASE64` and redeploy.

To rotate the bearer auth token:
1. Set the new value on both services simultaneously.
2. Redeploy both. There is a brief window during the rolling redeploy
   where one side has the new token and the other has the old; new
   writes during that window will fail and surface as a 5xx on the API
   side. Schedule rotation during low traffic.

## Blast radius if the keystore is compromised

The attacker recovers `KEK_OP` and can issue `unwrap` calls. They can
unwrap any DEK in the database — but only if they also have the hub's
`STHRIP_HUB_KEK`. With only the keystore key, they get nothing readable.

This is the entire point of the two-key envelope. A compromised
keystore + hub KEK = full recovery; either alone = nothing.

If the keystore is compromised:
1. Immediately rotate `AUTH_TOKEN` (kicks out any session the attacker
   established).
2. Decide whether to rotate `KEK_OP` (full rewrap of every row's
   `dek_wrap_op`). Mandatory if the attacker had the key long enough to
   exfiltrate database snapshots.
3. Audit the API access logs for unusual `unwrap` traffic.

## What this service deliberately does NOT do

- **No database access.** Stateless except the in-process AES-GCM.
- **No clearnet exposure.** Internal-only Railway network.
- **No `/keys` or `/export` endpoint.** No way to ex-filtrate the KEK
  through the HTTP surface.
- **No logging of payload bytes.** Body lengths and HTTP status codes
  only. An attacker reading the keystore logs learns nothing about the
  graph.
- **No multi-key support yet.** Sprint 7 adds key versioning. For now
  exactly one `KEK_OP` per service deployment.

## Local smoke test

You can build and run the image locally to confirm the Dockerfile is
well-formed and the endpoints work:

```bash
cd railway/op-keystore-deploy
docker build -t sthrip-op-keystore:local .
KEK=$(python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")
TOKEN=$(python -c "import secrets; print(secrets.token_hex(32))")
docker run --rm -p 8000:8000 \
    -e KEK_OP_BASE64="$KEK" \
    -e AUTH_TOKEN="$TOKEN" \
    sthrip-op-keystore:local

# In another terminal:
curl -s http://localhost:8000/health
DEK=$(python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())")
WRAPPED=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"dek_b64\":\"$DEK\"}" \
    http://localhost:8000/wrap | python -c "import sys,json; print(json.load(sys.stdin)['wrapped_b64'])")
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -d "{\"wrapped_b64\":\"$WRAPPED\"}" \
    http://localhost:8000/unwrap
```

The returned `dek_b64` should equal the input `DEK`.

## Files

- `Dockerfile`    — image definition
- `server.py`     — FastAPI app
- `entrypoint.sh` — startup script (must be executable)
- `README.md`     — this file
