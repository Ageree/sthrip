# Sprint 7 Contract — Remote Attestation + Cutover Docs (FINAL)

> Phase 3 Sprint 7. Last sprint of the harness. Pre-filled by Lead from product-spec.md (AD-7) and lead-decisions.md.

## What Generator will build

### A. TEE-side attestation service

1. **`gcp/payment_tee_deploy/attestation_service.py`** — collects AMD SEV-SNP attestation report at boot:
   - On TEE service startup, attempt to read `/dev/sev-guest` (or use `python-sev-snp` library if available; if neither, generate a stub report flagged `mode=stub` for development/testing).
   - Compute the running container's image hash (`sha256` of the running container's image — read `/proc/self/cgroup` or env `STHRIP_TEE_IMAGE_HASH` set at deploy).
   - Persist the attestation to memory + serve it via `GET /.well-known/attestation.json`:
     ```json
     {
       "quote_b64": "<AMD SEV-SNP attestation report base64>",
       "image_hash_sha256": "<container image hash>",
       "timestamp": "<ISO 8601>",
       "signature_b64": "<hub static key signs the above tuple>",
       "mode": "snp_real" | "snp_stub"
     }
     ```
   - Hub static key for signing: env `STHRIP_TEE_ATTESTATION_KEY` (Ed25519 base64, similar pattern to canary).

2. **Update `gcp/payment_tee_deploy/payment_service.py`** — wire the attestation endpoint (currently a Sprint 5 stub) to actually return this payload.

### B. SDK verification

1. **Modify `sdk/sthrip/client.py`** (or wherever the SDK Python client lives):
   - Add `verify_tee: bool = False` parameter to `Sthrip(...)` constructor.
   - Add `expected_image_hash: str | None = None` parameter — if not provided, read from env `STHRIP_TEE_IMAGE_HASH`.
   - When `verify_tee=True` is set:
     - Before any payment send, fetch `/.well-known/attestation.json` from the configured base URL.
     - Validate signature using a pinned hub public key (env `STHRIP_TEE_ATTESTATION_PUBKEY` or constant in code per `sthrip_sdk/attestation_anchors.py`).
     - Verify `image_hash_sha256` matches `expected_image_hash`.
     - If mismatch or missing → raise `TEEMismatchError`.
     - Cache verified attestation for 5 minutes; refetch on cache miss or before payment.
   - When `verify_tee=False` → no-op (existing behavior preserved).

2. **`sdk/sthrip/attestation_anchors.py`** — anchor file for known-good image hashes. List structure:
   ```python
   KNOWN_GOOD_HASHES = [
       # "sha256-...",  # production deploy 2026-XX-XX
   ]
   ```
   Empty for now; operator populates after first deploy.

3. **`sdk/sthrip/exceptions.py`** (extend) — `TEEMismatchError(SthripError)`, `TEEAttestationStaleError`.

### C. Sprint 6 carry-over

Generator from Sprint 6 noted: dispatcher 4xx routing relies on `body["_status_code"]`. If TEE returns a 4xx with non-dict body (e.g., plain string), the tag is skipped → misroutes as success. Add defensive check in `payment_dispatch.py`: when `_status_code` is missing AND status code is 4xx, also raise HTTPException (don't treat as success).

### D. Documentation

1. **Update `docs/THREAT_MODEL.md`** — add a new row or update existing:
   - **Threat**: Hub runtime memory compromise (Railway host attacker reads in-memory plaintext)
   - **Pre-Phase-3**: HIGH residual (closed only via THREAT_MODEL Sprint 1-7 + 4b — operator coercion only)
   - **Post-Phase-3 (Sprint 7 enabled)**: TEE-protected via AMD SEV-SNP. Residual: AMD primitive bug (CVE class), AMD/GCP supply-chain compromise, image-pinning bypass via SDK opt-out
   - Cross-link Sprint 5 + Sprint 6 + Sprint 7 commits.

2. **Update `PRIVACY_FEATURES.md`** — add Phase 2 / Phase 3 section:
   - Auto-purge + canary (Sprint 1)
   - Commission + subscription tier (Sprints 2-4)
   - GCP TEE + attestation (Sprints 5-7 — mark "shipped pending operator deploy")
   - Honest framing: TEE benefits apply only when operator has deployed AND `STHRIP_PAYMENT_VIA_TEE=true`. Until then, runtime exposure is unchanged.

3. **`gcp/payment_tee_deploy/CUTOVER.md`** — operator runbook (12 sequential steps):
   1. Pre-flight: `gcloud auth list`, `gcloud projects list`, billing enabled, IAM granted.
   2. Generate mTLS certs (per `mtls/README.md`); store in secure-environment.
   3. Generate `STHRIP_TEE_ATTESTATION_KEY` Ed25519 keypair; private to TEE, public to Railway as `STHRIP_TEE_ATTESTATION_PUBKEY`.
   4. Build & push image: `docker build -t gcr.io/<project>/sthrip-payment-tee:vX.Y.Z .`. Capture sha256 → `STHRIP_TEE_IMAGE_HASH`.
   5. Provision Confidential VM: `bash setup-vm.sh`. Capture VM external IP → `STHRIP_TEE_ENDPOINT`.
   6. Verify VM: `curl https://<IP>:8080/health` (with mTLS). Inspect `/.well-known/attestation.json` payload.
   7. Append `STHRIP_TEE_IMAGE_HASH` to `attestation_anchors.py`, ship SDK update.
   8. Set Railway envs: `STHRIP_PAYMENT_VIA_TEE=false` initially, `STHRIP_TEE_ENDPOINT`, `STHRIP_TEE_CLIENT_CERT_PATH`, etc.
   9. Soak Railway with TEE health-check loop logs for 24-48h. Monitor `tee_unreachable_total`.
   10. Flip flag in **staging** first: `STHRIP_PAYMENT_VIA_TEE=true`. Run smoke tests + monitor `payment_dispatch` metrics.
   11. Flip flag in **production**. Watch `/admin/revenue` + payment latency dashboards 24h.
   12. If issues: `STHRIP_PAYMENT_VIA_TEE=false` → instant rollback. No code change required.
   13. After 7-day soak with flag on: announce in `PRIVACY_FEATURES.md` update + blog post.

   Include rollback table, monitoring checklist, alert thresholds.

### E. Tests

Tests in `tests/test_sdk_tee_attestation.py`:

1. **`test_attestation_endpoint_includes_required_fields`** — TestClient on `gcp/payment_tee_deploy/payment_service.py`, GET `/.well-known/attestation.json`, assert body has `quote_b64`, `image_hash_sha256`, `timestamp`, `signature_b64`, `mode`.

2. **`test_sdk_verify_tee_accepts_pinned_hash`** — mock the attestation endpoint to return a payload with image_hash matching `expected_image_hash`. SDK fetches, verifies signature with pinned pubkey, verifies hash, returns success.

3. **`test_sdk_verify_tee_rejects_mismatch_hash`** — same setup but image_hash differs. SDK raises `TEEMismatchError`.

4. **`test_sdk_verify_tee_rejects_invalid_signature`** — payload signed with wrong key. SDK raises `TEEMismatchError`.

5. **`test_sdk_verify_tee_disabled_skips_check`** — `Sthrip(verify_tee=False)` (default), no attestation fetched, no verification.

6. **`test_sdk_attestation_cache_5min_ttl`** — first call fetches, second within 5 min uses cache, after 5 min refetches.

7. **`test_sdk_attestation_stale_raises_on_fresh_fetch_failure`** — mock 503 from endpoint, no cached attestation → raises `TEEAttestationStaleError`.

8. **`test_dispatcher_4xx_non_dict_body_does_not_fallback`** (Sprint 6 carry-over) — TEE returns 422 with body=`"insufficient balance"` (plain string). Dispatcher should raise HTTPException with 422 and NOT fall back.

## How success is verified

```bash
cd "/Users/saveliy/Documents/Agent Payments/sthrip" && source .venv/bin/activate
pytest tests/test_sdk_tee_attestation.py -v --tb=short 2>&1 | tail -30
pytest tests/test_payment_dispatch.py -v --tb=short 2>&1 | tail -20  # regression on Sprint 6
timeout 600 pytest tests/ -q --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py 2>&1 | tail -30
```

Plus manual:
- `cat docs/THREAT_MODEL.md | grep -A 5 "Phase 3"` — see updated row
- `cat PRIVACY_FEATURES.md | grep -A 10 "TEE"` — see new section
- `wc -l gcp/payment_tee_deploy/CUTOVER.md` — runbook present, ~150-300 lines

## Risk callouts

- **Don't actually run `/dev/sev-guest`** in tests — use stub mode. The contract allows this.
- **Don't break SDK back-compat** — `verify_tee=False` (default) must preserve existing SDK API exactly.
- **Cache the attestation** — without caching, every payment fetches the endpoint (latency hit). 5-min TTL is reasonable.
- **Pinned pubkey must NOT be hardcoded in source** — it's a config value (env or `attestation_anchors.py`). Docs make this clear.
- **Don't ship a non-empty `KNOWN_GOOD_HASHES`** — operator populates after first real deploy.

## Out of scope

- Attestation chain validation against AMD root cert (Sprint 7+)
- Multi-region attestation (future)
- Reproducible builds (future hardening, not in Phase 2)

## Branch and commit

- Single commit: `feat(tee): remote attestation + SDK verify_tee + cutover runbook (Phase 3 Sprint 7 — FINAL)`
- After PASS, this closes Phase 2.
- No push.

## Final sprint completion checklist

After Generator + Evaluator both PASS Sprint 7:

1. `git log --oneline feat/revenue-and-tee` — verify 8 commits (one per sprint, plus Sprint 2 iter 2 = 8).
2. `pytest tests/ -x -q` — full suite green (no -x test skips).
3. Phase 2 close-out summary in state.json.
4. Lead reports to user with full Phase 2 completion stats.
