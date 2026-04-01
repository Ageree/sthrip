# Phase 4a: Agent Financial OS -- Final Evaluation

**Date**: 2026-04-01
**Reviewer**: code-reviewer (Claude Opus 4.6)
**Verdict**: **FAIL** -- 1 CRITICAL issue, 2 HIGH issues must be resolved before merge.

---

## 1. Test Results

**Full suite**: 2481 passed, 0 failed, 20 skipped (154s)
**Phase 4a test count by file**:

| File | Test Count |
|------|-----------|
| test_phase4a_foundation.py | 31 |
| test_treasury.py | 30 |
| test_credit_lending.py | 40 |
| test_conditional_payments.py | 24 |
| test_split_payments.py | 9 |
| test_multi_party.py | 27 |
| test_sdk_phase4a.py | 87 |
| **Total** | **248** |

All 248 Phase 4a tests pass. The overall suite (2481) passes with no regressions.

**Coverage**: Could not run isolated coverage due to a Python 3.9 + cryptography
PyO3 import conflict when test files are collected in combination. This is an
environment issue, not a code defect. The test count (248) and breadth of
scenarios (happy paths, error paths, edge cases, authorization checks) are
strong. Based on code inspection, estimated coverage of Phase 4a service and
repo modules is well above 80%.

---

## 2. Issues Found

### [CRITICAL] Alembic migration is completely out of sync with ORM models

**File**: `migrations/versions/j1k2l3m4n5o6_phase4a_financial_os.py`

The migration creates tables with **different column names, different enum values,
and missing columns** compared to the SQLAlchemy ORM models in `sthrip/db/models.py`.
Deploying this migration to PostgreSQL will create tables that the application
code cannot query correctly.

**Affected tables (all 9 new tables have mismatches)**:

| Table | Migration Column | ORM Column | Severity |
|-------|-----------------|------------|----------|
| treasury_policies | `allocation` | `target_allocation` | Name mismatch |
| treasury_policies | `cooldown_minutes` | `rebalance_cooldown_secs` | Name + type mismatch |
| treasury_policies | (missing) | `min_liquid_xmr`, `min_liquid_xusd`, `auto_lend_enabled`, `max_lend_pct`, `min_borrower_trust_score`, `max_loan_duration_secs`, `last_rebalance_at` | 7 columns missing |
| treasury_rebalance_logs | (table name) `treasury_rebalance_logs` | `treasury_rebalance_log` | Table name mismatch |
| treasury_rebalance_logs | `trigger_type` | `trigger` | Name mismatch |
| treasury_rebalance_logs | `moves` | `conversions` | Name mismatch |
| treasury_rebalance_logs | `total_moved` | `total_value_xusd` | Name mismatch |
| treasury_rebalance_logs | (missing) | `pre_allocation`, `post_allocation` | 2 columns missing |
| treasury_forecasts | `forecast_date`, `bucket_name`, `projected_balance` | `forecast_type`, `source_id`, `expected_amount`, `expected_currency`, `direction`, `expected_at`, `confidence` | Entirely different schema |
| agent_credit_scores | `score`, `payment_history_factor`, `account_age_factor`, `volume_factor`, `default_factor`, `last_calculated_at` | `credit_score`, `total_loans_taken`, `total_loans_repaid`, `total_loans_defaulted`, `total_borrowed_volume`, `avg_repayment_time_secs`, `longest_default_secs`, `max_borrow_amount`, `max_concurrent_loans`, `calculated_at` | Entirely different schema |
| agent_loans | `status` | `state` | Name mismatch |
| agent_loans | `total_due` | `repayment_amount` | Name mismatch |
| agent_loans | `due_at` | `expires_at` | Name mismatch |
| agent_loans | (missing) | `loan_hash`, `collateral_currency`, `repaid_amount`, `grace_period_secs`, `requested_at`, `defaulted_at`, `platform_fee` | 7 columns missing |
| conditional_payments | `payer_id` | `from_agent_id` | Name mismatch |
| conditional_payments | `payee_id` | `to_agent_id` | Name mismatch |
| conditional_payments | (missing) | `payment_hash`, `memo`, `locked_amount` | 3 columns missing |
| multi_party_payments | `initiator_id` | `sender_id` | Name mismatch |
| multi_party_payments | `executed_at` | `completed_at` | Name mismatch |
| multi_party_payments | (missing) | `payment_hash` | 1 column missing |
| multi_party_recipients | `agent_id` | `recipient_id` | Name mismatch |
| multi_party_recipients | `accepted` + `rejected` (dual bool) | `accepted` (nullable tri-state bool) | Schema design mismatch |

**LoanStatus enum mismatch**: Migration creates `('pending', 'active', 'repaid',
'defaulted', 'liquidated')` but the code defines `('requested', 'active', 'repaid',
'defaulted', 'liquidated', 'cancelled')`. The value `'requested'` is missing and
`'cancelled'` is missing from the migration enum.

**MultiPartyPaymentState enum mismatch**: Migration creates
`('pending_acceptance', 'partially_accepted', 'all_accepted', 'executed',
'expired', 'rejected')` but the code defines `('pending', 'accepted',
'completed', 'rejected', 'expired')`. None of the migration values match the
code values.

**Why tests pass**: Tests use `Base.metadata.create_all()` which creates tables
from the ORM model definitions, bypassing Alembic entirely. The migration has
never been tested against a real database.

**Fix**: Regenerate the entire migration from the current ORM models. Use
`alembic revision --autogenerate` or manually rewrite to match models.py exactly.

---

### [HIGH] ORM object mutation in credit_service.py repay_loan

**File**: `sthrip/services/credit_service.py:446-448`

```python
loan_obj = loan_repo.get_by_id(loan_id)
loan_obj.platform_fee = platform_fee  # Direct ORM mutation
db.flush()
```

This directly mutates an ORM object outside the repository layer. While ORM
mutation is accepted within repositories (per the project's documented exception),
the service layer should delegate mutations to the repo. The loan_repo should
have a `set_platform_fee(loan_id, fee)` method or the fee should be set as part
of the `repay()` transition.

**Fix**: Add a `set_platform_fee(loan_id, fee)` method to `LoanRepository` or
include `platform_fee` as a parameter to `loan_repo.repay()`.

---

### [HIGH] Private function imported from service into router

**File**: `api/routers/conditional_payments.py:84, 107`

```python
from sthrip.services.conditional_payment_service import _payment_to_dict
```

The underscore-prefixed `_payment_to_dict` is a private implementation detail
of the service module. The router imports it directly inside function bodies
(lazy import). This creates a tight coupling between the router and service
internals.

**Fix**: Either (a) make the function public by removing the underscore prefix,
or (b) expose a `list_conditional_payments()` method on the service class that
returns dicts, or (c) move `_payment_to_dict` to a shared serialization module.

---

### [MEDIUM] Deprecated `regex` parameter in FastAPI Query

**File**: `api/routers/conditional_payments.py:69`

```python
role: Optional[str] = Query(default=None, regex="^(sender|recipient)$"),
```

The `regex` parameter is deprecated in recent FastAPI/Pydantic versions. Use
`pattern` instead (as correctly done in `lending.py:150` and `multi_party.py:60`).

**Fix**: Change `regex=` to `pattern=`.

---

### [MEDIUM] models.py exceeds 800-line limit

**File**: `sthrip/db/models.py` (1213 lines)

The project guidelines specify 800 lines max per file. This file now contains
38 model classes. Consider splitting into domain-specific model modules (e.g.,
`models_treasury.py`, `models_lending.py`, `models_payments.py`) with a
`models/__init__.py` that re-exports everything for backward compatibility.

---

### [MEDIUM] balance_threshold condition logic may be inverted

**File**: `sthrip/services/conditional_payment_service.py:221`

```python
condition_met = balance < threshold
```

The condition triggers when balance is *below* the threshold. This could be
intentional (e.g., "pay insurance fund when reserves run low"), but it is
counter-intuitive for a "threshold" condition. Most threshold-based triggers
activate when a value *reaches or exceeds* a threshold. If this is intentional,
the field name should be `min_threshold` or the docstring should clarify the
semantics. Currently, tests confirm this is the implemented behavior, so it
may just need documentation.

---

### [LOW] Legacy SQLAlchemy API usage in tests

**File**: `tests/test_conditional_payments.py` (lines 430, 471, 485, 591, 658, 692)

```python
cp = db_session.query(ConditionalPayment).get(payment_id)
```

`Query.get()` is deprecated in SQLAlchemy 2.0. Use `Session.get()` instead:
```python
cp = db_session.get(ConditionalPayment, payment_id)
```

---

### [LOW] Broad exception catch in treasury rebalance

**File**: `sthrip/services/treasury_service.py:357`

```python
except (ValueError, Exception) as exc:
```

Catching `Exception` makes the `ValueError` catch redundant and silences all
errors. If the intent is to continue on conversion failures, catch specific
exceptions. If truly all errors should be swallowed, remove `ValueError` from
the tuple.

---

## 3. Code Quality Assessment

### Strengths

- **Immutable dict returns**: All service methods return plain dicts, not ORM
  objects. `_policy_to_dict`, `_loan_to_dict`, `_payment_to_dict`,
  `_recipient_to_dict`, `_offer_to_dict` -- all correctly convert ORM objects
  to response dicts.

- **Row-level locking**: `get_by_id_for_update()` methods exist in
  `ConditionalPaymentRepository`, `LoanRepository`, and `MultiPartyRepository`,
  with SQLite fallback (skip `with_for_update()` on SQLite).

- **Proper exception hierarchy**: Services raise `ValueError` (bad input),
  `PermissionError` (authorization), `LookupError` (not found). Routers map
  these to HTTP 400/403/404 respectively.

- **Audit logging**: State transitions in credit, conditional, split, and
  multi-party services all call `audit_log()`.

- **Status-guarded updates**: All state transitions use `.filter(state == X)`
  to prevent invalid transitions, returning rows-affected counts.

- **Bounded queries**: All list methods use `min(limit, _MAX_QUERY_LIMIT)`.

- **Well-structured repos**: Clean separation between service logic and data
  access. Repos are focused and under 250 lines each.

### conftest.py

All 9 new tables are registered in `_COMMON_TEST_TABLES`. All 5 new routers
are registered in `api/main_v2.py`. The conftest correctly imports and
includes all Phase 4a models.

---

## 4. File Size Compliance

| File | Lines | Status |
|------|-------|--------|
| treasury_service.py | 407 | OK |
| credit_service.py | 571 | OK |
| conditional_payment_service.py | 369 | OK |
| split_payment_service.py | 134 | OK |
| multi_party_service.py | 492 | OK |
| treasury_repo.py | 185 | OK |
| credit_repo.py | 87 | OK |
| loan_repo.py | 235 | OK |
| conditional_payment_repo.py | 152 | OK |
| multi_party_repo.py | 200 | OK |
| treasury router | 119 | OK |
| lending router | 174 | OK |
| conditional_payments router | 144 | OK |
| split_payments router | 44 | OK |
| multi_party router | 124 | OK |
| models.py | 1213 | OVER (800 max) |
| enums.py | 207 | OK |
| migration | 431 | OK |

---

## Review Summary

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 1     | block  |
| HIGH     | 2     | block  |
| MEDIUM   | 3     | warn   |
| LOW      | 2     | note   |

**Verdict: FAIL** -- The Alembic migration is fundamentally broken: column
names, table names, and enum values do not match the ORM models. Deploying
this to PostgreSQL production will result in immediate application errors.
The migration must be completely regenerated from the current ORM definitions
before this phase can be considered complete.

The 2 HIGH issues (ORM mutation in service layer, private function import in
router) should also be addressed but are not deployment blockers.

All service logic, repository patterns, test coverage, and SDK methods are
well-implemented and demonstrate solid engineering. The fix is mechanical
(regenerate migration) rather than architectural.
