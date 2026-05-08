# Sprint 3 Evaluation Result

## Verdict: PASS (ship-it, with caveats noted under Sprint 4 obligations)

Crypto correctness is sound, dual-write is wired into all four targets, reads
remain on plaintext FKs, migration is idempotent and reversible, baseline
regression suite shows zero new failures (24 pre-existing failures on parent
`0b03e69` reproduce 1:1 with Sprint 3 applied).

The Generator's report under-counted pre-existing failures (claimed 12, actual
24) — annoying but the substance ("zero new regressions") is verified.

## Crypto review (priority #1)

| Check | Result | Evidence |
| --- | --- | --- |
| AES-GCM nonce strategy: fresh `secrets.token_bytes(12)` per encrypt | OK | `envelope_crypto.py:224, 260`. `_aes_seal` and the payload-seal both call `secrets.token_bytes(_NONCE_LEN)` independently. **No nonce reuse.** |
| Each AES-GCM call uses a *distinct* nonce per (key, plaintext) | OK | One nonce for payload-under-DEK, one nonce for DEK-under-hub_kek, one nonce for DEK-under-op_kek. Three independent `token_bytes(12)` calls. Even if two happened to collide, they'd be under different keys, so still safe. |
| DEK is freshly random per envelope | OK | `dek = secrets.token_bytes(_KEK_LEN)` at line 256; new on every `encrypt_envelope` call. Test `test_transaction_envelope_uses_fresh_dek` proves identical inputs yield distinct envelopes. |
| Decrypt with wrong KEK fails (no silent fallback) | OK | Verified by my own ad-hoc run: wrong hub → `InvalidTag`; wrong op → `InvalidTag`; both wrong → `InvalidTag`. The `dek_via_hub != dek_via_op` check runs *after* both unwraps, so it can only trigger when both unwraps succeed but yielded different DEKs (active tampering). |
| Tamper detection wires through | OK | `test_envelope_tampering_detection` constructs a wrap of a different DEK under the legitimate hub_kek, expects `ValueError("wrappers disagree")`. Passes. |
| Payload serialization safe under hostile description content | OK | `_encode_payload` uses `json.dumps` with `separators` and `utf-8` — special chars (`"`, `\`, newline, `{`) round-trip unchanged. Verified by hand: `'hax"yo\\\\nbye{abc:1}'` → identical out. |
| Schema version field present | OK | `PaymentEnvelope.schema_version=1` (line 81), packed as `"v"` in msgpack blob, checked at decrypt (line 286). `with_schema_version` returns a new frozen instance (immutability respected). Sprint 7 rotation can bump cleanly. |
| Key length validation | OK | `_aes_seal`/`_aes_open` and `encrypt_envelope`/`decrypt_envelope` all enforce `_KEK_LEN == 32`. Short blobs in `_aes_open` raise before any AES call (`len < _NONCE_LEN+16`). |
| msgpack vs JSON choice | OK | msgpack for the wire envelope (compact, binary-safe for nonces/CT), JSON for the inner payload (human-debuggable should we ever want to). Both are explicitly defended (`use_bin_type=True`, `raw=False`). |

**No nonce-reuse, no IV-derivation-from-key, no fallback decryption path that
could mask tampering.** This is a clean envelope construction.

## Tests run with results

```
tests/test_envelope_crypto_service.py  29 passed (boundaries x14, roundtrip,
                                       both-keys, schema, tampering, KEK loader)
tests/test_operator_keystore.py         9 passed
tests/test_payment_envelope.py          8 passed (tx, escrow, milestone, relay,
                                       fresh-DEK, idempotent, reads-unchanged,
                                       migration round-trip)
TOTAL                                  46 passed in 0.53s
```

Coverage on new modules (`pytest --cov`):
- `sthrip/services/envelope_crypto.py` — **89 %** (15 missing lines, defensive)
- `sthrip/services/operator_keystore.py` — **98 %**
- `sthrip/services/payment_envelope_writer.py` — **97 %**
- Aggregate: **92.34 %** — over the 80 % gate.

## User criteria AC#2 partial check

| Sub-criterion | Met | Evidence |
| --- | --- | --- |
| `transactions`, `escrow_deals`, `escrow_milestones` carry encrypted-at-rest envelope | YES (forward-only, dual-write) | `participant_envelope BYTEA NULL` columns exist; repos populate on every insert. Old rows still plaintext (Sprint 4 backfills/cuts over). |
| ADMIN_API_KEY alone cannot read the graph (second factor required) | PARTIAL — by design | The wire format requires both KEKs at decrypt. **In Sprint 3 the `op_kek` is a hard-coded constant** in `operator_keystore.py:_STUB_OP_KEK` — anyone with the source code has it. This is the documented Sprint 3 → Sprint 4 boundary (Lead Q1: "stub then real"). The cryptographic *plumbing* is correct; the *separation-of-trust* property only lands when Sprint 4 deploys `sthrip-op-keystore.railway.internal` and flips `OP_KEYSTORE_MODE=remote`. The Generator report calls this out explicitly. |
| `MessageRelay` envelope (Lead Q5) | YES | New `participant_envelope` column on `message_relays`; `MessagingService.relay_message` calls `apply_envelope(relay, …)` before `db.add`. Verified by `test_message_relay_envelope_written`. |

## Migration safety

`migrations/versions/s0t1u2v3w4x5_payment_envelope.py`:

- All 7 ADD COLUMNs are nullable=True. ✓ Won't block legacy inserts.
- Each ADD COLUMN guarded by `inspector.get_columns` membership check (cheap for
  an Alembic env that runs once). Re-running upgrade is a no-op. ✓
- No UPDATE/INSERT in the migration body. ✓ (no backfill, per spec line 346)
- `down_revision = "r9s0t1u2v3w4"` correctly chains from the Sprint 2 head. ✓
- `downgrade()` drops only the columns this migration added, with a presence
  guard. ✓

The migration is also explicitly verified by `test_migration_round_trip`:
1. Build a "pre-Sprint-3" engine with bare tables.
2. Run `upgrade()` → assert columns exist.
3. Run `upgrade()` again → no-op (idempotent).
4. Run `downgrade()` → assert columns absent.

The test's monkey-patching of `alembic.op` is unconventional but works against
SQLite. It correctly avoids alembic's full env.py wiring.

## Dual-write coverage (all 4 tables)

| Table | Writer | Verified |
| --- | --- | --- |
| `transactions` | `transaction_repo.py:50-60` lazy-imports `apply_envelope`, calls after model construction, before `db.add`. | `test_transaction_envelope_written`, `test_transaction_envelope_uses_fresh_dek`, `test_transaction_envelope_idempotent_on_replay`. |
| `escrow_deals` | `escrow_repo.py:55-63` ditto. | `test_escrow_envelope_written` decrypts the envelope and confirms buyer/seller mapping. |
| `escrow_milestones` | `milestone_repo.py:46-72` looks up parent deal's buyer/seller (one extra query per milestone batch — bounded), then per-milestone `apply_envelope`. | `test_milestone_envelope_written`. |
| `message_relays` | `messaging_service.py:147-156` (top-level import — services layer, no db cycle). | `test_message_relay_envelope_written`. |

Dual-write coverage is complete.

## Read paths NOT touched

```
$ grep -rn 'decrypt_envelope|envelope_to_dict|PaymentEnvelope\.from_bytes|participant_envelope' sthrip/db/ api/
sthrip/db/models.py:189: participant_envelope = Column(LargeBinary, nullable=True)
sthrip/db/models.py:252: participant_envelope = Column(LargeBinary, nullable=True)
sthrip/db/models.py:306: participant_envelope = Column(LargeBinary, nullable=True)
sthrip/db/models.py:689: participant_envelope = Column(LargeBinary, nullable=True)
```

Only column declarations on the four models. **No router, no repo `get_*`/`list_*`
method, no admin view, no MCP tool reads from `participant_envelope`.** Sprint 4
gets a clean cutover surface.

## Stub keystore boundary

| Property | Verified |
| --- | --- |
| Default `OP_KEYSTORE_MODE` resolves to `"stub"` | `test_get_keystore_default_is_stub` |
| `StubKeystore.wrap_dek` is a real AES-GCM seal (not identity) | `test_keystore_stub_mode_round_trip` (asserts `wrapped != dek`) |
| `RemoteKeystore.{wrap_dek,unwrap_dek,get_kek_for_envelope}` all `raise NotImplementedError` | `test_remote_keystore_raises_until_sprint_4` |
| Switching `OP_KEYSTORE_MODE=remote` returns the placeholder, calling raises | `test_get_keystore_remote_mode` |
| Invalid mode rejected | `test_get_keystore_invalid_mode` |

The stub KEK (`b"sthrip-stub-op-kek-32-byte-len!!"`) is an in-source 32-byte
literal. Documented in module docstring as **not a secret**; the security
boundary is explicitly Sprint 4. This is the contract Lead approved.

## amount_bucket coarsening verification

`amount_to_bucket(Decimal("123.45"))` → `"100-1k XMR"`.

- bucket label does NOT contain `"123"` — verified by `test_amount_bucket_coarsened`.
- bucket label does NOT contain `"123.45"` — same test.
- 14 boundary cases parameterized in `test_amount_to_bucket_boundaries` (0, 0.5,
  1, 9.99, 10, 99.999, 100, 123.45, 999.999, 1000, 9999, 10000, 1e6, -1).
  Boundaries are `<` upper, `<=` lower → no overlap, no gap.

Buckets are linear-decade boundaries (0/1/10/100/1k/10k) which is "log-scale"
in the colloquial sense the contract means. Precision loss at the bottom edge
(every amount in `[100, 1000)` collapses to one bucket) is the privacy goal.

## Code review findings

### CRITICAL: none

### HIGH: none

### MEDIUM

1. **Generator misreported pre-existing failure count.** Generator claimed 12
   pre-existing failures; the actual baseline `0b03e69` has 24 failures (verified
   by `git stash && pytest && git stash pop`). The substance of the claim
   ("Sprint 3 introduces zero new failures") is true — the same 24 fail with or
   without Sprint 3 applied. But this miscount is the kind of self-reporting
   error a fresh evaluator should flag for the Lead.

2. **`ResourceWarning: unclosed database`** in three of the new tests
   (`test_transaction_envelope_idempotent_on_replay`, `test_reads_unchanged_in_dual_write`,
   `test_migration_round_trip`). All originate from SQLAlchemy holding the
   sqlite connection past the `db.close()` because the tests construct an
   engine in `_baseline_engine()` without an explicit `engine.dispose()`. Not a
   correctness issue and not a Sprint 3 regression (the project has many similar
   warnings) — recommend a follow-up cleanup but not a blocker.

### LOW

3. **`_serializable` in `payment_envelope_writer.py`** is a no-op — it returns
   the input unchanged for both `Decimal` and "everything else" branches. The
   actual normalization happens inside `envelope_crypto._normalize_payload`.
   The local helper is dead code; recommend removing it for clarity.

4. **`_BUCKET_NEGATIVE = "negative XMR"`** still contains the substring
   `"negative"`, which is a coarse leak for any amount ≤ 0. In practice a
   negative-amount transaction shouldn't exist (DB constraints elsewhere), but
   if it does, the bucket label literally telegraphs the sign. Consider
   `"<0 XMR"` or treating negatives as `"unknown"`. **Cosmetic, not blocking.**

5. **`amount_to_bucket(None)` returns `"10k+ XMR"`**. Defensive comment says
   "never leak 'unknown' as exact" but the chosen sentinel is misleading — a
   None amount becoming "10k+" overstates the bucket. Recommend
   `amount_to_bucket(None) → None` and let callers skip writing the column
   (the writer already does `if amount is not None` for the bucket assignment).
   **Cosmetic, not blocking.**

## Coverage

Aggregate over the three new modules: **92.34 %**, comfortably over the 80 %
contract gate. Missing lines are defensive branches (envelope corruption, key
length validation paths, defensive `except` in writer).

## Recommendation

**ship-it** — this is iter 1 and the implementation matches the contract on
every testable acceptance criterion (1–10). No CRITICAL or HIGH findings.
The MEDIUM and LOW findings are post-merge polish that don't affect the
crypto property under audit.

Sprint 4 obligations the Lead must track:
- Replace `_STUB_OP_KEK` with a network call to `sthrip-op-keystore.railway.internal`.
- Cut reads over to envelope-decrypt, drop plaintext FK columns.
- Backfill old rows with envelope (or accept they remain unencrypted historic data).
- Make `OP_KEYSTORE_MODE=remote` the production default.

Sprint 3 lays the foundation cleanly. The dual-write surface is correct and
complete; the cryptographic construction is sound.
