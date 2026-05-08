# Sprint 7 Generator Report

## Status: ready for evaluator

Iteration 1. Branch `feat/anonymity-hardening`, no commit yet.

## Files written / modified

- `PRIVACY_FEATURES.md` — full rewrite (legacy "INSTANT Maximum Privacy"
  marketing copy with CoinJoin/Submarine Swaps/zk-SNARKs/Tor advertised
  as a 3-minute combo replaced with explicit Shipped / In progress /
  Roadmap structure).
- `docs/THREAT_MODEL.md` — full rewrite (legacy MPC/bridge/Solidity-era
  threat model replaced with hub-centric model; 10 threat rows in a
  single table; 8 user-criteria scenarios all covered).
- `README.md` — replaced the four-bullet "Security" section with a
  Privacy section (5 bullets, links to both new files) plus a tightened
  Security section that drops the misleading "Non-custodial" and
  "Zero-knowledge" lines.
- `PRIVACY_GUIDE.md` — added a Sprint 7 status banner at the top making
  the aspirational nature of the rest of the file unambiguous and
  pointing readers at PRIVACY_FEATURES.md / docs/THREAT_MODEL.md. The
  body of the guide is left intact per the contract decision (general
  Monero hygiene advice is still useful even though the SDK helpers it
  imports are not all shipped surface).
- `.harness/anonymize-platform/sprint-7-contract.md` — contract for this
  sprint.

No code, tests, or migrations were touched. No emoji added to any
written content (legacy emoji in README headers and PRIVACY_GUIDE
existing body were left alone — out of scope and would have introduced
noise).

## Verification output

```
=== Should find ZERO matches ===
(empty)

=== AC #4 — all 8 threats present in THREAT_MODEL.md ===
OK: blockchain analyzer
OK: marketplace scraper
OK: subpoena
OK: ADMIN_API_KEY
OK: webhook correlation
OK: network observer
OK: runtime hub compromise
OK: malicious insider

=== All 6 commits in PRIVACY_FEATURES.md ===
OK: 5a68ec8
OK: 0b03e69
OK: 9eb2eca
OK: c7ae822
OK: 4aecfcb
OK: 16126a5

=== Threat row count in THREAT_MODEL.md ===
rows: 10  (≥8 required)

=== README privacy bullets count ===
14 bold-leading bullets across the merged Privacy + Security sections
(privacy section alone has 5 bullets, all linking to the new files)

=== markdownlint ===
not installed in this environment — skipped per contract
```

## Acceptance criteria checklist

1. PRIVACY_FEATURES.md "Shipped" names 6 sprints by hash — DONE (each
   sprint section opens with `**Commit**: \`<hash>\``).
2. PRIVACY_FEATURES.md "Roadmap" marks CoinJoin / Submarine Swaps /
   zk-SNARKs / MPC mixing as "Not shipped" — DONE.
3. THREAT_MODEL.md has ≥8 threat rows in a `Threat | Current defence |
   Residual risk` table — DONE (10 rows).
4. THREAT_MODEL.md covers all 8 user-criteria scenarios — DONE
   (verification grep above).
5. Each defence references the sprint commit — DONE (every defence
   cell names the sprint and the hash).
6. Residual risks are NAMED — DONE (e.g. runtime hub compromise:
   "ANY runtime memory dump captures one in-flight request's plan";
   leaked ADMIN_API_KEY: "Until Sprint 4b drops the plaintext FK
   columns, an attacker with ADMIN_API_KEY and direct Postgres SQL
   access can still read the FK columns").
7. README.md privacy section ≥3 bullets pointing at PRIVACY_FEATURES.md
   and THREAT_MODEL.md — DONE (5 privacy bullets, both links).
8. No file claims unshipped features in present tense — DONE
   (overpromising-phrase grep returns empty).
9. PRIVACY_GUIDE.md decision documented and applied — DONE (banner
   added; rationale recorded in sprint-7-contract.md decision log).

## Note for Lead / next sprint

GitNexus surfaced two side observations during verification:

1. `sthrip/bridge/mixing/coinjoin.py` contains real research code
   (`CoinJoinTransaction`, `CoinJoinInput`, `CoinJoinOutput`,
   `start_round`). The new PRIVACY_FEATURES.md Roadmap row names this
   file explicitly and clarifies "not on the hub request path", which
   matches reality — the symbol is unreachable from any payment,
   escrow, marketplace, or webhook flow.
2. `docs/PRIVACY_INSTANT.md` (173 lines) is another overpromising
   marketing file in the same family as the legacy PRIVACY_FEATURES.md.
   It is referenced from `docs/ARCHITECTURE.md:215` but NOT from any
   of the four files in Sprint 7 scope. Out of scope for this sprint;
   recommend Lead schedules a Sprint 7-bis or a fast follow-up to
   either rewrite it or mark it deprecated. The new PRIVACY_FEATURES.md
   intentionally does not link it.

## Out of scope items confirmed

- No commit. Evaluator pass first.
- No new tests (docs-only sprint per spec lines 462-464).
- No code touched.
