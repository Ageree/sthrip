# Sprint 6 Generator Report

## Status: ready-for-review

## Files added

```
A  railway/tor-sidecar-deploy/Dockerfile
A  railway/tor-sidecar-deploy/torrc
A  railway/tor-sidecar-deploy/entrypoint.sh   (chmod +x)
A  railway/tor-sidecar-deploy/README.md
A  tests/test_wellknown_onion.py
A  tests/test_sdk_use_tor.py
A  tests/test_webhook_tor_routing.py
A  .harness/anonymize-platform/sprint-6-contract.md
```

## Files modified

```
M  api/routers/wellknown.py            (+30 / -1)  — env-gated onion_endpoint
M  sdk/sthrip/client.py                (+22 / -1)  — Sthrip(use_tor=True)
M  sthrip/services/webhook_service.py  (+~110 / -2) — Tor outbound for .onion
M  requirements.txt                    (+5 / -1)   — httpx[socks], aiohttp_socks, PySocks
```

No DB migrations. No deletions. All changes additive and behind
`STHRIP_ONION_ENABLED=false` default.

## GitNexus impact

```
mcp__gitnexus__impact target=_send_webhook direction=upstream
=> risk LOW; d=1 _deliver_one (same file); d=2 process_event; d=3 _process_one.

mcp__gitnexus__impact target=agent_payments_discovery direction=upstream
=> risk LOW; impactedCount 0.

mcp__gitnexus__detect_changes scope=unstaged
=> risk_level: low; affected_processes: []; 30 symbols touched in 5 files.
```

## Test results

```
pytest tests/test_wellknown_onion.py -v              => 17 passed
pytest tests/test_sdk_use_tor.py -v                  =>  9 passed
pytest tests/test_webhook_tor_routing.py -v          => 14 passed
                                                       ─────────
                                                       40 new passing

pytest tests/ --ignore=tests/test_channels.py        => 2742 passed
       --ignore=tests/test_mcp_auth.py                  24 failed (PRE-EXISTING
       --ignore=tests/test_cli_client.py                — verified by stashing
       --ignore=tests/test_cli_commands.py              the diff and rerunning)
       -q                                               21 skipped
```

The 24 failing tests reproduce on the Sprint-5 baseline (commit `4aecfcb`)
when the Sprint 6 diff is stashed away — they are not regressions caused
by this sprint. They span: channel-api close-after-settlement, idempotency
e2e (3), full payment lifecycle, mcp tools auth (5), migration error
handling (6), session_store redis (2), production fixes (2), and others.
None touch wellknown / SDK client / webhook_service.

## Coverage

```
pytest --cov=api.routers.wellknown
       --cov=sthrip.services.webhook_service
       tests/test_wellknown_onion.py tests/test_sdk_use_tor.py
       tests/test_webhook_tor_routing.py
       tests/test_webhook_service.py tests/test_webhook_fanout.py

api/routers/wellknown.py                22 stmts, 0 miss, 100%
sthrip/services/webhook_service.py     328 stmts, 49 miss, 85%
TOTAL                                   86%
```

The 49 missing lines in `webhook_service.py` are the live `_send_webhook_via_tor`
body (real `aiohttp_socks` connector + `aiohttp.ClientSession.post` against a
real onion target) and a few unrelated retry / metric code paths that were
already missing before Sprint 6. The ImportError-fallback branch in the
new helper IS covered by `test_tor_helper_returns_failure_when_aiohttp_socks_missing`.

## Static / structural checks

```
$ grep -E "HiddenServiceVersion 3|SocksPort|ControlPort" railway/tor-sidecar-deploy/torrc
SocksPort 0.0.0.0:9050
ControlPort 0
HiddenServiceVersion 3                       (plus the matching comment lines)

$ test -f railway/tor-sidecar-deploy/Dockerfile        => OK
$ test -x railway/tor-sidecar-deploy/entrypoint.sh     => executable

$ ls migrations/versions/*.py | wc -l
21                                            (== Sprint 5 baseline; same files)
```

## Acceptance-criteria check (sprint-6-contract.md)

| # | AC                                                           | Status |
|---|--------------------------------------------------------------|--------|
| 1 | onion_endpoint published when both env vars set              | PASS   |
| 2 | onion_endpoint excluded when flag off                        | PASS   |
| 3 | onion_endpoint excluded when env var unset                   | PASS   |
| 4 | SDK use_tor=True configures SOCKS5 transport                 | PASS   |
| 5 | SDK use_tor=False default identical to current               | PASS   |
| 6 | SDK honours STHRIP_TOR_SOCKS_PROXY env override              | PASS   |
| 7 | webhook .onion target + flag on routes via SOCKS5            | PASS   |
| 8 | webhook clearnet + flag on stays direct (Lead Q4)            | PASS   |
| 9 | webhook .onion target + flag off stays direct                | PASS   |
| 10| webhook clearnet + flag off unchanged                        | PASS   |
| 11| torrc has v3 hidden service, no ControlPort, sane SocksPort  | PASS   |
| 12| Dockerfile present, entrypoint.sh executable                 | PASS   |
| 13| no new DB migration                                          | PASS   |

## Notes for Evaluator

- All Sprint 6 behaviour is gated on `STHRIP_ONION_ENABLED`. Until an
  operator sets it to a truthy value AND populates `STHRIP_ONION_ENDPOINT`,
  the system runs unchanged from Sprint 5.
- The Tor outbound path deliberately skips SSRF / IP-pinning. That is
  correct: SSRF / DNS-rebinding defences only apply to clearnet DNS; an
  onion address is resolved by the Tor client itself via `socks5h://`.
  The flag-gated nature limits the blast radius.
- The webhook delivery helper lazily imports `aiohttp_socks` so unit tests
  and dev environments without the wheel installed still pass for the
  clearnet path. The runtime container that actually sees Tor traffic
  must have `aiohttp-socks` installed (now in `requirements.txt`).
- The new `test_tor_helper_returns_failure_when_aiohttp_socks_missing`
  test patches `builtins.__import__` to confirm graceful failure when
  the dep is absent.
- I did **not** run `railway up`. Per the contract, deployment is Lead's
  decision; this sprint only ships artifacts and code.

## What Lead must do after merge to activate

1. Create a new Railway service `sthrip-tor` in the `sthrip` project
   (private network only).
2. Build it from `railway/tor-sidecar-deploy/`.
3. Mount a persistent volume on `/var/lib/tor/sthrip-hsv3`.
4. Deploy. Watch logs for `ONION_ADDRESS=...onion`.
5. On the API service, set:
   - `STHRIP_ONION_ENABLED=true`
   - `STHRIP_ONION_ENDPOINT=<the .onion from step 4>`
   - `STHRIP_TOR_SOCKS_PROXY=socks5h://sthrip-tor.railway.internal:9050`
6. Redeploy API; verify `/.well-known/agent-payments.json` advertises
   the onion endpoint.

Detailed runbook: `railway/tor-sidecar-deploy/README.md`.
