# Sprint 1 Contract: Foundation -- Enums + Models + Repos

## What Will Be Built

### 1. New Enums (`sthrip/db/enums.py`)
- `LoanStatus`: requested, active, repaid, defaulted, liquidated, cancelled
- `ConditionalPaymentState`: pending, triggered, executed, expired, cancelled
- `MultiPartyPaymentState`: pending, accepted, completed, rejected, expired

### 2. New Models (`sthrip/db/models.py`) -- 9 tables
- `TreasuryPolicy` -- agent treasury configuration (target allocation, rebalance thresholds, reserves)
- `TreasuryForecast` -- predicted cash flows (subscription due, escrow release, loan repayment)
- `TreasuryRebalanceLog` -- rebalance execution history
- `AgentCreditScore` -- credit score + factors + derived limits (agent_id as PK)
- `AgentLoan` -- loan lifecycle (requested -> active -> repaid/defaulted/liquidated)
- `LendingOffer` -- lending marketplace offers
- `ConditionalPayment` -- conditional payments with condition_type + condition_config
- `MultiPartyPayment` -- atomic multi-party payment header
- `MultiPartyRecipient` -- individual recipients in multi-party payment

### 3. New Repos (`sthrip/db/`) -- 5 files
- `treasury_repo.py` -- TreasuryRepository (policy CRUD, forecast CRUD, rebalance log)
- `credit_repo.py` -- CreditRepository (get/create credit score, update factors)
- `loan_repo.py` -- LoanRepository (create, state transitions, list, get_for_update)
- `conditional_payment_repo.py` -- ConditionalPaymentRepository (create, trigger, execute, expire, cancel, list)
- `multi_party_repo.py` -- MultiPartyRepository (create payment + recipients, accept/reject, complete, list)

### 4. Updated Files
- `sthrip/db/repository.py` -- re-export all new repos
- `tests/conftest.py` -- add new tables to `_COMMON_TEST_TABLES`

## Testable Acceptance Criteria

### Test file: `tests/test_phase4a_foundation.py`

1. **test_enum_loan_status** -- LoanStatus has all 6 values, subclasses str
2. **test_enum_conditional_payment_state** -- ConditionalPaymentState has all 5 values
3. **test_enum_multi_party_payment_state** -- MultiPartyPaymentState has all 5 values
4. **test_treasury_policy_create** -- create policy, verify fields persisted
5. **test_treasury_policy_unique_agent** -- second policy for same agent raises IntegrityError
6. **test_treasury_forecast_create** -- create forecast, verify fields
7. **test_treasury_rebalance_log_create** -- create log entry, verify fields
8. **test_treasury_repo_set_get_policy** -- TreasuryRepository.set_policy + get_policy
9. **test_treasury_repo_add_forecast** -- add_forecast + list_forecasts
10. **test_treasury_repo_add_rebalance_log** -- add_rebalance_log + list_rebalance_history
11. **test_credit_score_create** -- create AgentCreditScore, verify defaults
12. **test_credit_repo_get_or_create** -- CreditRepository.get_or_create returns/creates
13. **test_credit_repo_update_score** -- update score + factors
14. **test_loan_create** -- create AgentLoan with all fields
15. **test_loan_state_transitions** -- requested -> active -> repaid (via repo methods)
16. **test_loan_default_transition** -- active -> defaulted -> liquidated
17. **test_loan_cancel** -- requested -> cancelled
18. **test_loan_repo_list_by_agent** -- list loans as lender and borrower
19. **test_lending_offer_create** -- create LendingOffer, verify fields
20. **test_lending_offer_deactivate** -- deactivate offer via repo
21. **test_conditional_payment_create** -- create with all condition types
22. **test_conditional_payment_trigger** -- pending -> triggered
23. **test_conditional_payment_execute** -- triggered -> executed
24. **test_conditional_payment_expire** -- pending -> expired
25. **test_conditional_payment_cancel** -- pending -> cancelled
26. **test_conditional_payment_repo_list** -- list by agent (sender/recipient)
27. **test_multi_party_create** -- create payment + recipients
28. **test_multi_party_accept_all** -- all recipients accept -> completed
29. **test_multi_party_reject** -- one rejects -> rejected (require_all_accept=True)
30. **test_multi_party_expire** -- pending -> expired
31. **test_multi_party_repo_list** -- list by agent

## How Success Is Verified

```bash
# Run Sprint 1 tests
python3 -m pytest tests/test_phase4a_foundation.py -v

# Verify no regressions
python3 -m pytest --tb=short -q 2>&1 | tail -5
```

All 31 new tests pass. All 2240 existing tests pass (no regressions).
1 pre-existing failure in test_readiness_nonblocking.py (unrelated to Phase 4a).

## Results

- 31/31 new tests PASSED
- 2240 existing tests PASSED (baseline verified by running with --ignore=tests/test_phase4a_foundation.py)
- 1 pre-existing FAILED (TestReadinessWalletNonBlocking -- unrelated)
- Sprint 1 COMPLETE
