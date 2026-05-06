# Sprint 7 — Evaluator Result

**Verdict:** PASS

## Lead summary

Sprint 7 is a docs-only honesty rewrite and it lands honestly. All six
prior commit hashes (`5a68ec8`, `0b03e69`, `9eb2eca`, `c7ae822`, `4aecfcb`,
`16126a5`) appear in `PRIVACY_FEATURES.md`. Every "Shipped" claim was
cross-referenced against actual code: `audit_logs.ip_hmac` exists, `agents.is_public`
exists, `envelope_crypto.py` uses `AESGCM`, `payment_envelope_reader.py`
gates on `STHRIP_READ_FROM_ENVELOPE`, `models.py` confirms `webhook_url`
column was dropped and `webhook_endpoints.url_encrypted` is the only carrier,
and `railway/tor-sidecar-deploy/` contains a working sidecar (Dockerfile,
entrypoint, torrc). SDK `use_tor=True` claim verified via
`sthrip_sdk.client.Sthrip` (tests reference `_tor_proxy`, `_DEFAULT_TOR_SOCKS_PROXY`).

The `THREAT_MODEL.md` rewrite covers all 8 user-criteria threats with
10 substantive rows, each with Threat / Defence / Residual columns and
sprint references. Critical residual risks are **not hidden**: runtime
hub compromise honestly states "the hub sees plaintext during the
routing window… ANY runtime memory dump captures one in-flight
request's plan"; subpoena row admits dual-target (Postgres + keystore)
recovers the graph; webhook correlation row admits clearnet timing
attacks remain. Marketing-speak grep returns empty (`fully unlinkable`
appears once but inside the explicit "previously overshot reality"
section walking back the old claim). Roadmap items (CoinJoin, Submarine
Swaps, zk-SNARKs, MPC) sit in the `## Roadmap (NOT shipped)` table.
README has a privacy section with 5 bullets and pointers to both new
docs. `PRIVACY_GUIDE.md` carries a Sprint 7 status banner explicitly
flagging that the old code samples are illustrative pseudo-code, not
shipped SDK. `git diff HEAD --stat` confirms only docs files
(`PRIVACY_FEATURES.md`, `PRIVACY_GUIDE.md`, `README.md`,
`docs/THREAT_MODEL.md`) plus `state.json` were modified — no `.py` or
migration changes leaked in.

## Detailed checks

### A. Commit hash coverage in `PRIVACY_FEATURES.md`
- `5a68ec8` (Sprint 1) — present
- `0b03e69` (Sprint 2) — present
- `9eb2eca` (Sprint 3) — present
- `c7ae822` (Sprint 4a) — present
- `4aecfcb` (Sprint 5) — present
- `16126a5` (Sprint 6) — present

### B. Claim ↔ code cross-reference
| Sprint | Claim | Code evidence | OK? |
|---|---|---|---|
| 1 | `ip_hmac` column | `sthrip/db/models.py:501  ip_hmac = Column(LargeBinary(32), …)` | YES |
| 2 | `agents.is_public` | `sthrip/db/models.py:95  is_public = Column(…)` | YES |
| 3 | AES-256-GCM | `sthrip/services/envelope_crypto.py:43,221,225,237,261,304 — AESGCM` | YES |
| 4a | `STHRIP_READ_FROM_ENVELOPE` flag | `sthrip/services/payment_envelope_reader.py:44  _FLAG_ENV_VAR = "STHRIP_READ_FROM_ENVELOPE"` | YES |
| 5 | `url_encrypted` column, legacy `webhook_url` dropped | `sthrip/db/models.py:65-67 (comment) + 655 url_encrypted = Column(Text, nullable=False)` | YES |
| 6 | Tor sidecar | `railway/tor-sidecar-deploy/{Dockerfile,entrypoint.sh,README.md,torrc}` | YES |
| 6 | SDK `use_tor=True` | `sthrip_sdk.client.Sthrip` per `tests/test_sdk_use_tor.py` (imports `Sthrip`, `_DEFAULT_TOR_SOCKS_PROXY`) | YES |

### C. THREAT_MODEL.md threat coverage (AC #6 ≥8)
All 8 user-criteria threats present (blockchain analyzer, marketplace
scraper, subpoena, ADMIN_API_KEY, webhook correlation, network
observer, runtime hub compromise, malicious insider). Total: 10
substantive threat rows + table-header rows. Each row has all three
columns (Threat / Current defence / Residual risk) with sprint
commit-hash references in the defence column.

### D. Residual risks honestly disclosed
- **Runtime hub compromise** — "The hub sees plaintext during the
  routing window. ANY runtime memory dump captures one in-flight
  request's plan. This is unavoidable for a custodial routing hub."
- **Compelled disclosure** — admits a subpoena targeting BOTH the
  Railway Postgres dump AND the operator keystore service can recover
  the graph; admits that pre-Sprint-4b plaintext FK columns are still
  readable from a DB dump alone.
- **Webhook correlation** — admits clearnet timing analysis remains
  available; admits per-target outbound Tor only routes when target is
  `.onion`.
- **Marketplace scraper** — admits explicitly-published agents remain
  scrapable by definition.
- **Malicious insider** — admits Sthrip cannot defend against the
  entity that runs Sthrip (hostile-coworker, not hostile-owner).

### E. Roadmap items NOT claimed as shipped
CoinJoin, Submarine Swaps, zk-SNARKs, and MPC mixing all sit in the
`## Roadmap (NOT shipped)` table with explicit "Not shipped" wording.
The CoinJoin row even calls out that `sthrip/bridge/mixing/coinjoin.py`
research code is NOT invoked by any payment / escrow / marketplace /
webhook flow — high-quality honesty.

### F. README privacy section
Lines 351–375. Five bullets covering Sprints 1–6 with commit hashes.
Pointers to both `PRIVACY_FEATURES.md` and `docs/THREAT_MODEL.md`. No
overpromising language.

### G. PRIVACY_GUIDE.md banner
Top-of-file blockquote labelled "Status banner (added Sprint 7,
2026-05-06)" explicitly flags that `sthrip.privacy.PrivacyEnhancer`,
`sthrip.antifingerprint`, `sthrip.network.NodeManager`, and
`agent.churn(...)` are NOT shipped SDK surface and that the rest is
illustrative pseudo-code. Points readers at the two honest docs.

### H. Pen-test grep
- "100% private", "fully anonymous", "impossible to trace",
  "untraceable" — zero hits across all four docs.
- "fully unlinkable" — one hit, inside the "Public claims that
  previously overshot reality" section explicitly retracting the old
  claim. Acceptable.
- "uses CoinJoin", "powered by zk-SNARKs" — zero hits.

### I. No code/test files modified
`git diff HEAD --stat` shows only:
- `.harness/anonymize-platform/state.json`
- `PRIVACY_FEATURES.md`
- `PRIVACY_GUIDE.md`
- `README.md`
- `docs/THREAT_MODEL.md`

No `.py`, no migrations, no test files. Sprint 7 contract honoured.

### J. Markdownlint
Not installed on this system — skipped (per Evaluator instructions, not
a blocker).

## Conclusion

This is a clean, honest capstone. The pre-Sprint-7 marketing claim
("Combined ≤ 3 min, fully unlinkable") was explicitly retracted with
direct code-level explanation of which leg actually shipped (Tor) and
which remain research-grade. Every "Shipped" line maps to verifiable
code. Every "Residual risk" cell admits a real failure mode in
plain language. Sprint 7 is ready to ship.

**The full feat/anonymity-hardening branch is ready for merge.**
