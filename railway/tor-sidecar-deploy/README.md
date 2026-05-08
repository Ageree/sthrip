# Sthrip Tor sidecar — operator runbook

A small Alpine + `tor` container deployed as a sibling Railway service to
the Sthrip API. It serves two purposes:

1. **Inbound** — a Hidden Service v3 mapping `:80 → sthrip-api.railway.internal:8000`,
   so `https://<onion>.onion/v2/...` resolves to the same FastAPI app
   without exposing it to the clearnet.
2. **Outbound** — a SOCKS5 proxy on `127.0.0.1:9050` (private Railway
   network only) consumed by the API container when delivering webhooks
   to `.onion` agents. Per Lead decision Q4, only `.onion` targets get
   routed through Tor; clearnet stays clearnet.

Behind the `STHRIP_ONION_ENABLED` feature flag (default `false`) so we
can land code without flipping any user-visible behaviour.

## What it builds

- `Dockerfile` — `alpine:3.19` + `tor` + `tini`, runs as the
  unprivileged `tor` user, no `ControlPort`.
- `torrc` — v3 hidden service, `SocksPort 0.0.0.0:9050`, `ClientOnly 1`,
  `SafeLogging 1`, IPv6 client off.
- `entrypoint.sh` — starts tor, polls for the `.onion` hostname, prints
  it once on first boot, then hands control to the tor process.

## First-time deploy

1. **Create the Railway service** named e.g. `sthrip-tor` in the
   `sthrip` project with a private (internal-only) network.
2. **Mount a persistent volume** at `/var/lib/tor/sthrip-hsv3`. This is
   essential — the v3 hidden-service ed25519 keys live there. Without
   the volume, every restart yields a brand-new `.onion` and clients
   that pinned the previous one can no longer reach you.
3. Build with this directory as context (`railway/tor-sidecar-deploy/`).
4. After first deploy, watch the logs for:
   ```
   [sthrip-tor] ONION_ADDRESS=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.onion
   ```
5. **Copy that address into the API service** as `STHRIP_ONION_ENDPOINT`
   and set `STHRIP_ONION_ENABLED=true` on the API service. The
   `/.well-known/agent-payments.json` discovery JSON will start
   advertising `onion_endpoint`.
6. (Optional) Set `STHRIP_TOR_SOCKS_PROXY` on the API service if the
   sidecar is reachable at a non-default address (default
   `socks5h://127.0.0.1:9050` works when sidecar and API are colocated;
   on Railway the API service should use
   `socks5h://sthrip-tor.railway.internal:9050`).

## Subsequent deploys

The persistent volume keeps the `.onion` stable. The container will
print the same address on boot. No env-var update needed unless you
intentionally rotate the keys (see below).

## Key rotation

To rotate the `.onion` address (and invalidate every prior pinning):

1. Disable the flag: set `STHRIP_ONION_ENABLED=false` on the API service.
2. Wipe the persistent volume (Railway dashboard → service → volume →
   delete).
3. Redeploy the sidecar; new keys generate, new `.onion` is printed.
4. Update `STHRIP_ONION_ENDPOINT` and re-enable the flag.

## Blast radius if keys leak

The v3 hidden-service ed25519 keys living in
`/var/lib/tor/sthrip-hsv3/` are equivalent to the identity of the
onion site. Anyone with them can serve traffic at that `.onion`. They
are NOT a wallet key and do not by themselves leak any user data, but
treat them as you would a TLS private key:

- Restrict who can pull the volume snapshot.
- If you suspect leakage, follow "Key rotation" above immediately.

## What this sidecar deliberately does NOT do

- Does **not** open any inbound clearnet port — Railway exposes 9050
  only to the private network, not the public internet.
- Does **not** run as a Tor relay. `ClientOnly 1` makes that explicit.
- Does **not** expose `ControlPort`. A compromised process cannot
  remotely reconfigure tor.
- Does **not** use IPv6 outbound (Railway private networking is v4).

## Local smoke test

You can build the image locally to confirm the Dockerfile is well-formed:

```bash
cd railway/tor-sidecar-deploy
docker build -t sthrip-tor:local .
# do NOT run this with the real volume mount on your laptop — it will
# generate throwaway keys.  For runtime testing, use Railway staging.
```

## Files

- `Dockerfile`       — image definition
- `torrc`            — tor configuration
- `entrypoint.sh`    — startup script (must be executable)
- `README.md`        — this file
