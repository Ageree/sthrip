# Sprint 7 Contract: honest docs rewrite + THREAT_MODEL.md

## What I will build

- Rewrite `PRIVACY_FEATURES.md`: split "Shipped" (six sprints, each with a real
  commit hash) vs "In progress" (Sprint 4b) vs "Roadmap" (CoinJoin, Submarine
  Swaps, zk-SNARKs, MPC mixing — explicitly NOT shipped).
- Rewrite `docs/THREAT_MODEL.md`: 8+ threats in a single table with columns
  `Threat | Current defence | Residual risk`. Each defence row references the
  sprint commit that introduced it.
- Update `README.md` privacy/security section to point at the new files and
  drop the legacy "Zero-knowledge" line.
- Reframe `PRIVACY_GUIDE.md` with a status banner: it currently advertises
  techniques (churn, decoy txs, fingerprint randomization) that are NOT
  shipped in the request path of the hub. The banner makes that explicit and
  redirects readers to `PRIVACY_FEATURES.md` + `docs/THREAT_MODEL.md`.

## Specific testable acceptance criteria

1. `PRIVACY_FEATURES.md` "Shipped" section names exactly the 6 sprints by
   commit hash: `5a68ec8`, `0b03e69`, `9eb2eca`, `c7ae822`, `4aecfcb`,
   `16126a5`.
2. `PRIVACY_FEATURES.md` "Roadmap" section explicitly marks CoinJoin,
   Submarine Swaps, zk-SNARKs, and MPC mixing as "Not shipped".
3. `docs/THREAT_MODEL.md` has at least 8 threat rows in a table with columns
   `Threat | Current defence | Residual risk`.
4. `docs/THREAT_MODEL.md` covers all 8 scenarios from user-criteria AC #6:
   external blockchain analyzer, marketplace scraper, Railway subpoena,
   leaked `ADMIN_API_KEY`, webhook correlation, on-path network observer,
   runtime hub compromise, malicious insider operator.
5. Each defence references the sprint commit (e.g.
   "Sprint 1 audit IP scrubbing — 5a68ec8").
6. Residual risks are NAMED, not hidden — e.g. for runtime hub compromise:
   "hub sees plaintext during routing window".
7. `README.md` privacy section has at least 3 bullets and points at
   `PRIVACY_FEATURES.md` and `docs/THREAT_MODEL.md`.
8. No file claims a feature that hasn't shipped (no emoji-decorated
   "CoinJoin" or "zk-SNARKs" in present tense).
9. `PRIVACY_GUIDE.md` carries an explicit status banner that makes its
   aspirational status unambiguous.

## How verified

- markdownlint clean (or skipped if not installed in this env).
- Reviewer reads files for completeness, factual accuracy, no overpromising.
- `grep` checks for known overpromising phrases — should find ZERO in
  present tense in the four target files.
- All 8 threats from user-criteria present in `docs/THREAT_MODEL.md`
  (`grep` per keyword).

## Out of scope

- Code changes.
- New features or migrations.
- The Russian text in the legacy `PRIVACY_FEATURES.md` is removed (rewrite
  is in English only); kept as a note in `docs/THREAT_MODEL.md` history that
  the prior file mixed Russian/English marketing copy.

## Decision log

- `PRIVACY_GUIDE.md` is NOT deleted. It documents privacy hygiene (churn,
  fingerprint randomization, "don't reuse addresses") that is generally
  applicable to Monero usage even when the helper imports do not all map to
  shipped Sthrip code. The fix is a status banner that says exactly that;
  rewriting the whole guide is out of Sprint 7 scope.
- The legacy `PRIVACY_FEATURES.md` claimed "Combined ≤ 3 min, fully
  unlinkable" with CoinJoin + Submarine Swaps + Tor + zk Proofs. Only the
  Tor leg of that claim shipped (Sprint 6, `16126a5`). The new file
  explicitly disclaims the prior copy.
