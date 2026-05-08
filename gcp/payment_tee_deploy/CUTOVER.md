# TEE Cutover Runbook (Phase 3 Sprint 7)

Sequential operator runbook to deploy the Sthrip payment-TEE service on a
GCP Confidential VM, soak it, and flip the production feature flag with
a tested rollback path.

> **Audience**: Sthrip operator with Railway admin + GCP Owner role.
> **Length**: 12 steps + post-cutover monitoring + rollback table.
> **Total wall-clock**: 24-72 hours including soak windows.

Conventions used below:

| Token | Meaning |
|-------|---------|
| `<project>` | GCP project ID hosting the Confidential VM (e.g. `sthrip-tee-prod`). |
| `<vX.Y.Z>` | Image version — must match the Dockerfile label and the SDK `KNOWN_GOOD_HASHES` entry you ship in step 7. |
| `<region>` / `<zone>` | GCP region + zone. Default: `us-central1` / `us-central1-a`. |
| `$RAILWAY_*` | Railway env vars set via `railway variables --service sthrip-api`. |
| `$TEE_VM_IP` | External IP of the Confidential VM after step 5. |

Every step ends with a **Verify** block — if you do not see the expected
output, **stop and investigate** before continuing.

---

## Step 1 — Pre-flight checks

```bash
# Confirm the gcloud CLI is logged into the right account.
gcloud auth list
gcloud projects list | grep <project>

# Confirm billing is enabled on the project.
gcloud beta billing projects describe <project>

# Confirm the operator has the required IAM roles
# (compute.admin + iam.serviceAccountUser at minimum).
gcloud projects get-iam-policy <project> \
    --flatten="bindings[].members" \
    --filter="bindings.members:user:$(gcloud config get-value account)" \
    --format="value(bindings.role)"

# Confirm Confidential VM is enabled in the region.
gcloud compute zones describe <zone> --project=<project>
```

**Verify**: `gcloud auth list` shows the operator account as `*` active;
`projects describe` shows `billingEnabled: true`; `get-iam-policy` lists
`roles/compute.admin` (or higher); `zones describe` shows
`status: UP`.

---

## Step 2 — Generate mTLS certificates

The TEE service requires client mTLS. Generate certs per
`gcp/payment_tee_deploy/mtls/README.md`:

```bash
cd gcp/payment_tee_deploy/mtls
bash generate-certs.sh    # produces ca.pem, server.pem, server.key, client.pem, client.key
```

Store all five files in your secure-environment vault (e.g. 1Password,
Bitwarden) AND in Railway as base64-encoded env vars:

```bash
railway variables --service sthrip-api \
    --set "STHRIP_TEE_CA_B64=$(base64 -w0 ca.pem)" \
    --set "STHRIP_TEE_CLIENT_CERT_B64=$(base64 -w0 client.pem)" \
    --set "STHRIP_TEE_CLIENT_KEY_B64=$(base64 -w0 client.key)"
```

**Verify**: each cert is non-empty, `openssl x509 -in client.pem -noout
-issuer -subject` shows the operator's CA chain.

---

## Step 3 — Generate TEE attestation key

```bash
python -c "
import base64, secrets
from nacl.signing import SigningKey
sk = SigningKey.generate()
priv = base64.b64encode(sk.encode()).decode()
pub  = base64.b64encode(sk.verify_key.encode()).decode()
print('STHRIP_TEE_ATTESTATION_KEY=' + priv)
print('STHRIP_TEE_ATTESTATION_PUBKEY=' + pub)
"
```

* The **private** half (`STHRIP_TEE_ATTESTATION_KEY`) is loaded into the
  TEE VM via GCP Secret Manager (set in step 5). It NEVER leaves the
  TEE — Railway must not see it.
* The **public** half (`STHRIP_TEE_ATTESTATION_PUBKEY`) is set on
  Railway AND shipped to SDK callers (see step 7).

**Verify**: paste both lines into your secret vault. The two values are
the only Sprint 7-specific keys and must remain in sync — losing the
private half forces a re-cutover.

---

## Step 4 — Build and push the TEE image

```bash
cd gcp/payment_tee_deploy
docker build -t gcr.io/<project>/sthrip-payment-tee:<vX.Y.Z> .
docker push gcr.io/<project>/sthrip-payment-tee:<vX.Y.Z>

# Capture the digest emitted by `docker push`. We re-pull it cleanly
# so the digest is authoritative even if a local layer was cached.
docker inspect gcr.io/<project>/sthrip-payment-tee:<vX.Y.Z> \
    --format '{{index .RepoDigests 0}}' \
    | tee /tmp/sthrip-tee-image-digest
```

The digest looks like
`gcr.io/<project>/sthrip-payment-tee@sha256:<64hex>`. Set the operator's
authoritative pin:

```bash
export STHRIP_TEE_IMAGE_HASH="sha256:$(awk -F'@sha256:' '{print $2}' /tmp/sthrip-tee-image-digest)"
echo "$STHRIP_TEE_IMAGE_HASH"
```

**Verify**: `STHRIP_TEE_IMAGE_HASH` matches the regex
`^sha256:[a-f0-9]{64}$`. Save this value — you will paste it twice
(step 5 + step 7).

---

## Step 5 — Provision the Confidential VM

```bash
cd gcp/payment_tee_deploy
export GCP_PROJECT=<project>
export GCP_ZONE=<zone>
export TEE_IMAGE="gcr.io/<project>/sthrip-payment-tee:<vX.Y.Z>"
bash setup-vm.sh

# After the script returns, capture the external IP.
export TEE_VM_IP=$(gcloud compute instances describe sthrip-tee-vm \
    --project=<project> --zone=<zone> \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')
echo "$TEE_VM_IP"
```

`setup-vm.sh` boots the VM with `--confidential-compute=SEV_SNP`,
mounts the mTLS certs as Secret-Manager-backed disks, and seeds the
following env vars on the VM:

* `STHRIP_TEE_ATTESTATION_KEY` — the **private** half from step 3.
* `STHRIP_TEE_IMAGE_HASH`      — captured in step 4.
* `PROXY_AUTH_TOKEN`           — fresh secret used as defence-in-depth
  alongside mTLS.

**Verify**: `gcloud compute instances describe sthrip-tee-vm` shows
`confidentialInstanceConfig.enableConfidentialCompute: true`,
`scheduling.onHostMaintenance: TERMINATE`. `$TEE_VM_IP` resolves to a
public address.

---

## Step 6 — Verify the VM end-to-end

```bash
# Health probe with mTLS.
curl --cacert ca.pem --cert client.pem --key client.key \
    "https://${TEE_VM_IP}:8080/health"
# Expect: {"status":"ok"}

# Attestation payload — what the SDK fetches.
curl --cacert ca.pem --cert client.pem --key client.key \
    "https://${TEE_VM_IP}:8080/.well-known/attestation.json" | jq .

# Expect a JSON dict with keys:
#   quote_b64, image_hash_sha256, timestamp, signature_b64, mode
# image_hash_sha256 must match $STHRIP_TEE_IMAGE_HASH from step 4.
# mode must be "snp_real" in production (NEVER "snp_stub").
```

If `mode == "snp_stub"`, the AMD SEV-SNP guest device is missing or the
`python_sev_snp` helper failed to import — STOP. Do not pin a stub
attestation; it offers no real protection.

**Verify**: the `image_hash_sha256` field of the attestation matches
`$STHRIP_TEE_IMAGE_HASH` byte-for-byte; `mode == "snp_real"`; the
signature verifies under `STHRIP_TEE_ATTESTATION_PUBKEY` from step 3
(use the SDK's `verify_tee_attestation` for an offline check).

---

## Step 7 — Pin the image hash in the SDK

Append the digest to `sdk/sthrip/attestation_anchors.py`:

```python
# sdk/sthrip/attestation_anchors.py
KNOWN_GOOD_HASHES: list[str] = [
    "sha256:<64hex>",  # 2026-XX-XX vX.Y.Z — first production deploy
]
```

Then ship a new SDK release:

```bash
cd sdk
# Bump version in sthrip/__init__.py and pyproject.toml.
python -m build
python -m twine upload dist/sthrip-<new-version>*
```

**Verify**: `pip install --upgrade sthrip` from a clean venv pulls the
new version; `python -c "from sthrip.attestation_anchors import
KNOWN_GOOD_HASHES; print(KNOWN_GOOD_HASHES)"` prints the new entry.

---

## Step 8 — Configure Railway envs (flag OFF)

```bash
railway variables --service sthrip-api \
    --set "STHRIP_PAYMENT_VIA_TEE=false" \
    --set "STHRIP_TEE_ENDPOINT=https://${TEE_VM_IP}:8080" \
    --set "STHRIP_TEE_PROXY_TOKEN=$(cat /tmp/proxy-auth-token)" \
    --set "STHRIP_TEE_IMAGE_HASH=${STHRIP_TEE_IMAGE_HASH}" \
    --set "STHRIP_TEE_ATTESTATION_PUBKEY=<paste pub from step 3>" \
    --set "STHRIP_TEE_CLIENT_CERT_PATH=/etc/sthrip/tee/client.pem" \
    --set "STHRIP_TEE_CLIENT_KEY_PATH=/etc/sthrip/tee/client.key" \
    --set "STHRIP_TEE_CA_PATH=/etc/sthrip/tee/ca.pem"
```

The flag stays `false` so the existing local handler keeps serving real
traffic while the health-check loop runs against the new TEE.

**Verify**: `railway variables --service sthrip-api | grep STHRIP_TEE`
shows all eight values; `STHRIP_PAYMENT_VIA_TEE` is the literal string
`false`.

---

## Step 9 — Soak the TEE health-check loop (24-48h)

The Railway service runs an in-process health-check loop
(`payment_dispatch.health_check_loop`). Stream logs:

```bash
railway logs --service sthrip-api --filter "TEE health"
```

Monitor the Prometheus metric `tee_unreachable_total{reason="health"}`
on `/admin/metrics`. Acceptable envelope: zero alerts in any 1-hour
window. Three-strikes-style alert lines look like
`TEE /health failed 3 consecutive times`.

**Verify**: after 24h, `tee_unreachable_total{reason="health"}` is
flat or near-zero. If it rises, the VM is unstable — do NOT proceed
to step 10.

---

## Step 10 — Flip the flag in **staging** first

If you do not have a staging Railway service, create one and re-do
steps 8-9 against it. Once staging is soaked:

```bash
railway variables --service sthrip-api-staging \
    --set "STHRIP_PAYMENT_VIA_TEE=true"
railway redeploy --service sthrip-api-staging
```

Run the smoke-test pack against staging:

```bash
pytest tests/test_payment_dispatch.py -v
pytest tests/test_sdk_tee_attestation.py -v
# Plus an end-to-end Sthrip SDK pay() against the staging URL.
```

**Verify**: `tee_dispatch_total{outcome="success"}` increments on every
real payment; `outcome="fallback_*"` stays at zero; payment latency on
the staging dashboards stays within 1.5x the local-handler baseline.

---

## Step 11 — Flip the flag in **production**

```bash
railway variables --service sthrip-api \
    --set "STHRIP_PAYMENT_VIA_TEE=true"
railway redeploy --service sthrip-api
```

Watch `/admin/revenue` and the payment-latency dashboards for the next
24 hours. Alert thresholds:

| Metric | Threshold | Action |
|--------|-----------|--------|
| `tee_dispatch_total{outcome="fallback_5xx"}` | >5 events / 5min | Page operator. |
| `tee_dispatch_total{outcome="fallback_unreachable"}` | >5 events / 5min | Page operator. |
| `tee_dispatch_total{outcome="user_error"}` | >baseline-rate * 2 | Investigate (could be legit user-error spike). |
| `payment_latency_seconds{quantile="0.95"}` | >1.5x baseline | Investigate. |
| `tee_unreachable_total{reason="health"}` | any non-zero increment | Investigate. |

**Verify**: 24h in, all five metrics are within thresholds; no
correlation between TEE flag flip and revenue dashboard anomalies.

---

## Step 12 — Rollback procedure (no code change required)

If anything in step 11 trips an alert:

```bash
railway variables --service sthrip-api \
    --set "STHRIP_PAYMENT_VIA_TEE=false"
railway redeploy --service sthrip-api
```

**Within 60 seconds** the dispatcher returns to the local handler. No
DB rollback is required — the local path writes the same balance and
HubRoute rows the TEE path does (the M-1 fix in Sprint 6 ensures the
HubRoute admin row is inserted on the Railway side regardless of which
path served the request).

| Symptom | Action |
|---------|--------|
| TEE 5xx storm | Flip flag → false. Investigate VM logs. Re-soak before retry. |
| TEE network unreachable | Flip flag → false. Verify GCP firewall + Railway egress IP allow-list. |
| SDK clients seeing `TEEMismatchError` after legitimate redeploy | Re-run step 4-5, then update `KNOWN_GOOD_HASHES` (step 7) and ship a new SDK release. Do NOT roll back the VM if the new image is otherwise healthy. |
| Latency spike | Flip flag → false. Compare payment-latency before/after the flip. |

**Verify rollback worked**: `tee_dispatch_total{outcome="success"}`
stops incrementing; payments continue serving via the local path
without errors visible to clients.

---

## Step 13 — Soak window + announcement

Once the flag has been on for 7 days with no rollback events:

* Move the `<sprint-7-commit>` placeholder in `PRIVACY_FEATURES.md` to
  the actual commit hash.
* Add a row to the post-cutover dashboard in
  `docs/THREAT_MODEL.md` noting the date.
* Publish a blog post / changelog summarising the cutover. Tone:
  honest about the residual risks listed in the THREAT_MODEL row.

---

## Monitoring checklist (steady state)

After cutover, the on-call dashboard should show:

* `tee_dispatch_total{outcome="success"}` — primary success counter.
* `tee_dispatch_total{outcome="user_error"}` — 4xx from the TEE; matches
  the local user-error baseline within ±20%.
* `tee_dispatch_total{outcome="fallback_*"}` — should be near zero in
  steady state. Any non-zero rate is a paging alert.
* `tee_unreachable_total{reason="*"}` — health, network, server_error
  counters. Steady state: flat.
* Payment-latency 95p — within 1.5x of the local-handler baseline.

## Alert thresholds (steady state)

| Metric | Window | Action |
|--------|--------|--------|
| `fallback_5xx` rate ≥ 5 / 5min | 5min | Page operator, investigate VM. |
| `fallback_unreachable` rate ≥ 5 / 5min | 5min | Page operator, check GCP. |
| Payment latency 95p > 1.5x baseline | 30min sustained | Investigate. |
| `tee_unreachable_total{reason="health"}` > 0 in 1h | 1h | Slack alert, investigate. |
| `image_hash_sha256` mismatch in attestation | immediate | Stop SDK callers; possible image drift. |

## Pinned hashes lifecycle

* Append-only — each operator deploy adds one new entry.
* Old entries stay in the list until the operator is confident no
  client is still pinned to them.
* Removing an entry is a breaking change for SDK callers — bump SDK
  major version when you do it.

---

## Phase 2 close-out

Step 13 marks the end of the Phase 2 + Phase 3 plan tracked under
`.harness/phase2-money-and-tee/`. Subsequent hardening (multi-region
attestation, AMD root-cert chain validation, reproducible builds) is
out of scope and tracked separately.
