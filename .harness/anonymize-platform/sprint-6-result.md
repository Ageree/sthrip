# Sprint 6 Evaluator Result

## VERDICT: PASS

Sprint 6 (Tor `.onion` sidecar + SDK SOCKS5) meets every acceptance criterion
in `sprint-6-contract.md`. The Lead Q4 invariant — outbound Tor only when the
target hostname endswith `.onion`, never for clearnet — is correctly
implemented and explicitly tested. Default behaviour (flag off) is byte-for-byte
unchanged from Sprint 5. No DB migrations were added.

## Lead summary (≤200 words)

The implementation cleanly gates all new behaviour behind
`STHRIP_ONION_ENABLED` (default empty/false) and adds a defensive
`_should_route_via_tor(url)` predicate that returns True iff the env flag is
truthy AND the parsed hostname endswith `.onion` (case-insensitive).
`test_clearnet_target_uses_direct_when_enabled` and
`test_should_route_via_tor_matrix` both directly verify clearnet+flag-on does
NOT enter the Tor helper — confirmed by `tor_mock.assert_not_awaited()` and
`resolve_mock.assert_called_once()`. The Tor delivery helper deliberately
skips SSRF / IP-pinning, which is correct because socks5h-resolved onions are
opaque to the local DNS-rebinding defence; that path runs only after the
predicate has proved the target is `.onion`. The sidecar `torrc` is hardened
(`HiddenServiceVersion 3`, `ControlPort 0`, `ClientOnly 1`, `SafeLogging 1`,
no IPv6 client). Dockerfile drops to the unprivileged `tor` user, entrypoint
is executable, README documents volume mount + key rotation + blast radius.
40/40 new tests pass; 76/76 existing webhook tests pass; coverage on the
target modules is wellknown=100%, webhook_service=85%, total 86% (above the
80% bar). Migration count unchanged from baseline `4aecfcb` (21 .py files).
The 24 broader test failures are pre-existing on Sprint 5 baseline (verified
by stashing the diff and re-running).

## Detailed verification

### A. Test runs

```
pytest tests/test_wellknown_onion.py tests/test_sdk_use_tor.py \
       tests/test_webhook_tor_routing.py -v
=> 40 passed, 2 warnings in 0.86s
```

All 40 new tests PASS. Full breakdown:

- `test_wellknown_onion.py`: 17 passed (3 AC fixtures + truthy/falsy parametrised)
- `test_sdk_use_tor.py`: 9 passed
- `test_webhook_tor_routing.py`: 14 passed

### B. Coverage

```
pytest --cov=sthrip.services.webhook_service --cov=api.routers.wellknown \
       --cov=sdk.sthrip.client --cov-report=term \
       tests/test_wellknown_onion.py tests/test_sdk_use_tor.py \
       tests/test_webhook_tor_routing.py tests/test_webhook_service.py \
       tests/test_webhook_fanout.py

api/routers/wellknown.py             22  0   100%
sthrip/services/webhook_service.py  328  49   85%
TOTAL                               350  49   86%   (>= 80% bar PASS)
```

Note: cov path `sdk/sthrip/client` requires dotted `sdk.sthrip.client` to
register correctly. Spot-check confirmed all new Sprint-6 lines (47-53,
131-158, 296-312) are covered; the 70% missing in client.py is pre-existing
escrow/channel/etc paths not exercised by `test_sdk_use_tor.py`.

### C. Existing webhook regression check

```
pytest tests/test_webhook_url_encryption.py tests/test_webhook_service.py \
       tests/test_webhook_encryption.py tests/test_webhook_fanout.py
=> 76 passed in 0.75s
```

Zero regressions in pre-existing webhook code paths.

### D. Broad sanity sweep

```
pytest tests/ --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py \
              --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py
=> 2742 passed, 24 failed, 21 skipped, 387 warnings in 110.53s
```

The 24 failures all reproduce on the Sprint-5 baseline `4aecfcb` after
`git stash`. Spot-check of `test_close_channel_after_settlement_200` and
`test_create_session_uses_setex` confirmed both fail without any Sprint-6
diff applied. Categories: channel-api close-after-settlement (1), idempotency
e2e (4), full payment lifecycle (1), mcp tools auth (5), migration error
handling (6), session_store redis (2), production fixes (2), readiness (1),
production_fixes_round2 (1), production_fixes payment_id (1). None touch
wellknown / SDK client / webhook_service. Treating as PRE-EXISTING.

`test_cli_client.py` and `test_cli_commands.py` fail to import `respx`
(missing dev-dep). Pre-existing, ignored per Generator's note.

### E. Tor sidecar artifact review

`railway/tor-sidecar-deploy/`:

- `Dockerfile` — Alpine 3.19, installs `tor` + `tini`, drops to `USER tor`,
  exposes 9050 only (Railway private network), ENTRYPOINT via tini for
  proper signal handling. **PASS**
- `torrc`:
  - `HiddenServiceVersion 3` — PASS (v3 onion, ed25519)
  - `ControlPort 0` — PASS (no remote NEWNYM, no signal control surface)
  - `SocksPort 0.0.0.0:9050` — PASS
  - `HiddenServicePort 80 sthrip-api.railway.internal:8000` — PASS
  - `ClientOnly 1` — extra hardening (no relay)
  - `SafeLogging 1` + `Log notice stdout` — no circuit-detail leakage
  - `ClientUseIPv6 0` — Railway internal v4-only
  - `RunAsDaemon 0` — foreground for clean signal handling
  - `DataDirectory /var/lib/tor` — PASS
- `entrypoint.sh` — executable bit set (`-rwxr-xr-x`), polls
  `/var/lib/tor/sthrip-hsv3/hostname` for 60 s, prints
  `[sthrip-tor] ONION_ADDRESS=<addr>` to stdout, foregrounds `tor` via
  `wait $TOR_PID` so tini can forward SIGTERM. **PASS**
- `README.md` — covers all required runbook items: persistent volume mount
  point `/var/lib/tor/sthrip-hsv3` (bullet 2), first-boot capture (bullet 4),
  key rotation procedure (section "Key rotation"), blast-radius note for key
  leakage (section "Blast radius if keys leak"). **PASS**

### F. Code: feature-flag default-off

- `api/routers/wellknown.py` — `_onion_endpoint_or_none()` reads
  `STHRIP_ONION_ENABLED`; if empty/not in `{"1","true","yes","on"}` returns
  None and the handler returns the unchanged module-level constant. The
  payload dict is built fresh (`dict(_AGENT_PAYMENTS_DISCOVERY)`) so the
  immutable constant is never mutated. **PASS**
- `sdk/sthrip/client.py` — new `use_tor: bool = False` kwarg; when False,
  `_build_session()` does not touch `session.proxies` (verified by
  `test_use_tor_default_false_no_socks` and `test_use_tor_false_ignores_env_proxy`).
  **PASS**
- `sthrip/services/webhook_service.py` — `_should_route_via_tor()` returns
  False unless flag is truthy AND hostname endswith `.onion`. The Tor branch
  is taken only if predicate True; otherwise the existing IP-pinning + SSRF
  path runs unchanged. **PASS**
- AC #2 / #3 (flag off OR env unset → no `onion_endpoint`) — both paths
  individually tested.

### G. Lead Q4 invariant — VERIFIED

The most critical check: outbound Tor ONLY for `.onion` targets. Verified at
three layers:

1. **Predicate-level**: `_should_route_via_tor` returns False for clearnet
   even when flag on. Asserted in `test_should_route_via_tor_matrix`.
2. **Dispatch-level**: in `_send_webhook`, the Tor branch is `if
   _should_route_via_tor(url)`. Otherwise we fall through to the existing
   `resolve_and_validate(url)` clearnet path (line 263).
3. **Behavioural test**: `test_clearnet_target_uses_direct_when_enabled`
   sets `STHRIP_ONION_ENABLED=true`, calls
   `_send_webhook("https://example.com/webhook", ...)`, then asserts
   `tor_mock.assert_not_awaited()` AND `resolve_mock.assert_called_once()`.
   This is the exact AC #8 from the contract.

All 4 cells of the 2×2 matrix (flag×onion-vs-clearnet) are tested + a 5th
case (flag unset). **PASS — Lead Q4 invariant rigorously enforced.**

### H. Pen-test grep results

```
$ grep -rn "WEBHOOK_FORCE_TOR\|FORCE_TOR" sthrip/ api/ sdk/ --include="*.py"
(empty)         => no all-traffic-via-tor sneak-in. PASS.

$ grep -rn "\.onion" sthrip/ api/ sdk/ --include="*.py" | grep -v endswith
(only docstrings + Sprint-3 bridge/tor module pre-existing files)
=> no hardcoded onion addresses in new code. PASS.

$ grep -rn "socks5h://" sthrip/ api/ sdk/ --include="*.py"
=> only in docstrings + the env-driven default constants. No hardcoded
   credentials embedded. PASS.

$ grep -E "httpx\[socks\]|aiohttp-socks|PySocks" requirements.txt
httpx[socks]>=0.27.0
aiohttp-socks>=0.8.4
PySocks>=1.7.1
=> deps present. PASS.
```

### I. SDK backward compat

- `use_tor: bool = False` default preserves the existing init signature.
- `Sthrip()` and `Sthrip(api_key=..., max_per_tx=...)` work unchanged
  (no proxies dict mounted, `session.proxies` untouched).
- `test_use_tor_default_false_no_socks` and
  `test_use_tor_false_ignores_env_proxy` verify behaviour even if
  `STHRIP_TOR_SOCKS_PROXY` env var is set in the environment.
- `test_use_tor_attribute_persists` confirms the `_use_tor` attribute is
  stored on the instance.

**PASS.**

### J. No DB migrations

```
$ ls migrations/versions/*.py | wc -l
21
$ git ls-tree -r --name-only 4aecfcb -- migrations/versions/ | wc -l
21
$ git diff --name-only 4aecfcb HEAD -- migrations/
(empty)
```

Migration count and contents identical to baseline `4aecfcb`. **PASS** for
AC #13.

## Acceptance-criteria final scorecard

| # | AC                                                            | Status |
|---|---------------------------------------------------------------|--------|
| 1 | onion_endpoint published when both env vars set               | PASS   |
| 2 | onion_endpoint excluded when flag off                         | PASS   |
| 3 | onion_endpoint excluded when env var unset                    | PASS   |
| 4 | SDK use_tor=True configures SOCKS5 transport                  | PASS   |
| 5 | SDK use_tor=False default identical to current                | PASS   |
| 6 | SDK honours STHRIP_TOR_SOCKS_PROXY env override               | PASS   |
| 7 | webhook .onion target + flag on routes via SOCKS5             | PASS   |
| 8 | webhook clearnet + flag on stays direct (Lead Q4 invariant)   | PASS   |
| 9 | webhook .onion target + flag off stays direct                 | PASS   |
| 10| webhook clearnet + flag off unchanged                         | PASS   |
| 11| torrc has v3 hidden service, no ControlPort, sane SocksPort   | PASS   |
| 12| Dockerfile present, entrypoint.sh executable                  | PASS   |
| 13| no new DB migration                                           | PASS   |

## Minor observations (non-blocking)

1. The Generator report says coverage 86% via the `--cov=` form using
   slash-paths; on this environment coverage requires dotted module form
   (`--cov=sthrip.services.webhook_service`). Same numbers either way; just
   a minor command-line caveat for future sprints.
2. `test_webhook_tor_routing.py:90` uses
   `asyncio.get_event_loop().run_until_complete(coro)` which raises a
   `DeprecationWarning` on Python 3.13. Tests pass; cosmetic.
3. The Tor delivery helper imports `time` inside the function body
   (`import time as _time`). Stylistic; harmless.
4. Generator report's `migrations/versions/*.py | wc -l => 21` matches the
   baseline `21` (the directory listing above showed `22` because
   `__pycache__/` is also a directory entry). No discrepancy.
5. `requirements.txt` adds three packages (`httpx[socks]`, `aiohttp-socks`,
   `PySocks`); `requirements.lock` only references httpx-related socks deps.
   The runtime image must ship `aiohttp-socks` for the actual Tor outbound
   path; the lazy import in `_send_webhook_via_tor` covers test-env
   degradation cleanly. Generator already flagged this in their notes.

## Recommendation

**MERGE.** Sprint 6 is ready to commit on `feat/anonymity-hardening`. After
merge, Lead can run the operational sequence in
`railway/tor-sidecar-deploy/README.md` to actually deploy the sidecar and
flip `STHRIP_ONION_ENABLED=true` on the API service.
