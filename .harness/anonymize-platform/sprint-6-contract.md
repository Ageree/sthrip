# Sprint 6 Contract: Tor `.onion` sidecar + discovery JSON + SDK SOCKS5

## Scope

Infrastructure-only sprint. **No DB migrations.** Behind `STHRIP_ONION_ENABLED` feature
flag (default `false`) so the cutka-deploy is safe.

## What I will build

### Infra (new directory `railway/tor-sidecar-deploy/`)

- `Dockerfile` — Alpine + `tor` package, drops to `tor` user, exposes `9050`,
  ENTRYPOINT `/entrypoint.sh`
- `torrc` — Hidden Service v3 mapping `:80 → sthrip-api.railway.internal:8000`,
  SocksPort `0.0.0.0:9050`, ControlPort disabled, no logging of circuit data,
  data dir `/var/lib/tor`
- `entrypoint.sh` — start `tor` (foreground), poll `/var/lib/tor/sthrip-hsv3/hostname`
  for up to 60s and dump the resolved `.onion` to stdout once available
  (operator copy-pastes into `STHRIP_ONION_ENDPOINT`)
- `README.md` — operator runbook: how to mount the persistent volume on
  `/var/lib/tor/sthrip-hsv3`, what to do on first boot, key rotation, blast
  radius if the keys leak

### Code

- `api/routers/wellknown.py`: include `onion_endpoint` in the discovery JSON
  IFF `STHRIP_ONION_ENABLED` is truthy AND `STHRIP_ONION_ENDPOINT` env is set.
  Endpoint path stays cache-friendly because we only branch on env, never read
  from the filesystem at request time. Default behaviour unchanged.
- `sdk/sthrip/client.py`: add `use_tor: bool = False` param. When True, build
  the `requests.Session` with a SOCKS5 proxy mount taken from
  `STHRIP_TOR_SOCKS_PROXY` (default `socks5h://127.0.0.1:9050`). When False,
  behaviour identical to current code.
- `sthrip/services/webhook_service.py`: when target hostname endswith `.onion`
  AND `STHRIP_ONION_ENABLED` truthy, route delivery through the Tor SOCKS5
  proxy via an `aiohttp_socks` connector AND skip SSRF / IP-pinning (the
  resolution happens in Tor, not locally — `socks5h` semantics). Per Lead Q4:
  **only `.onion` targets**, never clearnet.
- `requirements.txt`: add `httpx[socks]>=0.27` (SOCKS extra) and
  `aiohttp-socks>=0.8` (server-side `aiohttp` SOCKS connector).

### Tests (all new, all unit / no live Tor)

- `tests/test_wellknown_onion.py`
- `tests/test_sdk_use_tor.py`
- `tests/test_webhook_tor_routing.py`

## Specific testable acceptance criteria

1. `GET /.well-known/agent-payments.json` includes `onion_endpoint` when
   `STHRIP_ONION_ENABLED=true` AND `STHRIP_ONION_ENDPOINT` env var set
   (`test_onion_endpoint_published_when_enabled`).
2. `GET /.well-known/agent-payments.json` excludes `onion_endpoint` when flag
   off (`test_onion_endpoint_excluded_when_disabled`).
3. `GET /.well-known/agent-payments.json` excludes `onion_endpoint` when env
   var unset, even if flag on (`test_onion_endpoint_excluded_when_env_unset`).
4. SDK `Sthrip(use_tor=True)` configures SOCKS5 proxy on the requests Session
   (`test_use_tor_true_configures_socks5_transport`).
5. SDK `Sthrip(use_tor=False)` is identical to current (no proxy adapter)
   (`test_use_tor_default_false_no_socks`).
6. SDK `use_tor=True` honours `STHRIP_TOR_SOCKS_PROXY` env override
   (`test_use_tor_respects_env_proxy`).
7. Webhook delivery to `.onion` target with `STHRIP_ONION_ENABLED=true` routes
   through SOCKS5 (`test_onion_target_uses_socks5_when_enabled`).
8. Webhook delivery to clearnet target with `STHRIP_ONION_ENABLED=true` does
   **not** route through SOCKS5 — Lead Q4 invariant
   (`test_clearnet_target_uses_direct_when_enabled`).
9. Webhook delivery to `.onion` target with `STHRIP_ONION_ENABLED=false` does
   **not** route through SOCKS5 (won't actually connect, but won't try Tor
   either — legacy behaviour) (`test_onion_target_uses_direct_when_disabled`).
10. Webhook delivery to clearnet target with flag off is unchanged
    (`test_clearnet_target_uses_direct_when_disabled`).
11. `torrc` declares `HiddenServiceVersion 3`, no `ControlPort` (or set to 0),
    sane `SocksPort` — verified via grep.
12. `Dockerfile` exists and is well-formed; `entrypoint.sh` is executable.
13. **No new DB migration:** `migrations/versions/` count unchanged from the
    Sprint 5 baseline (22 files at `4aecfcb`).

## How verified

```bash
pytest tests/test_wellknown_onion.py tests/test_sdk_use_tor.py \
       tests/test_webhook_tor_routing.py -v

pytest --cov=sdk/sthrip/client --cov=api/routers/wellknown \
       --cov=sthrip/services/webhook_service \
       --cov-report=term --cov-fail-under=80 \
       tests/test_wellknown_onion.py tests/test_sdk_use_tor.py \
       tests/test_webhook_tor_routing.py

# Sanity broad
pytest tests/ -x --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py

# Static checks
grep -E "HiddenServiceVersion 3|SocksPort|ControlPort" railway/tor-sidecar-deploy/torrc
test -f railway/tor-sidecar-deploy/Dockerfile
test -x railway/tor-sidecar-deploy/entrypoint.sh

# Migration count guard
test "$(ls migrations/versions/ | wc -l)" = "22"
```

## GitNexus impact (paste output)

```
mcp__gitnexus__impact target=_send_webhook direction=upstream
=> risk LOW; d=1 _deliver_one (same file); d=2 process_event; d=3 _process_one.
   No external module surface affected.

mcp__gitnexus__impact target=agent_payments_discovery direction=upstream
=> risk LOW; impactedCount 0.
```

## Out of scope

- Actually deploying the Railway sidecar service (Lead does in a separate step
  after merge).
- Generating the real `.onion` address (operator action; we only consume
  `STHRIP_ONION_ENDPOINT` env).
- Per-target Tor circuit pinning / circuit isolation.
- 14-day key-rotation grace period (operational runbook, not code).
- Live integration test against a real Tor instance in CI (out of scope per
  spec lines 436-437).
- Changing existing webhook signing / retry / SSRF behaviour for clearnet —
  fully preserved.
