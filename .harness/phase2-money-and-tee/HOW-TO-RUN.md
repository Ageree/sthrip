# How to run this harness

Plan written by Lead 2026-05-07. To execute via /harness-long-task:

## Option 1: Single-shot /loop autonomous (recommended)

```
/loop 30 min /harness-long-task используй план в .harness/phase2-money-and-tee/. user-criteria, lead-decisions, и product-spec уже написаны. Не задавай вопросы которые покрыты в lead-decisions.md. Working directory /Users/saveliy/Documents/Agent Payments/sthrip. Branch feat/revenue-and-tee создаётся от main или от feat/anonymity-hardening (Lead решает в Sprint 1). После каждого спринта пересоздавай команду агентов. Evaluator всегда в независимом контексте. Полная автономность как в anonymity-hardening цикле.
```

## Option 2: Manual sprint-by-sprint

```
/harness-long-task план в .harness/phase2-money-and-tee/. Сейчас выполни Sprint 1 (auto-purge + warrant canary). После завершения остановись и доложи.
```

Затем после каждого:

```
/harness-long-task продолжи с Sprint N (см .harness/phase2-money-and-tee/state.json и product-spec.md)
```

## Pre-flight checklist

Перед запуском проверь:

1. `git log --oneline -5` — feat/anonymity-hardening на месте?
2. `git branch` — какая active branch? (Lead в Sprint 1 разберётся откуда branch'eваться)
3. .venv installed and working — `cd sthrip && source .venv/bin/activate && pytest --version`
4. GitNexus index fresh — `npx gitnexus analyze --embeddings` если давно не индексировалось
5. Railway CLI logged in — `railway whoami`
6. (Phase 3 only) gcloud CLI installed — `gcloud auth list`. Если нет — install после Phase 2 готов.

## Expected duration

- Phase 1: 1 sprint × ~3-6 hours = ~1 day
- Phase 2: 3 sprints × ~3-6 hours = ~3 days
- Phase 3: 3 sprints × ~6-10 hours = ~5 days

Total wall-clock with /loop 30m intervals: 5-10 days actual harness work + test/eval iterations.

## Safety nets

- All work на `feat/revenue-and-tee` branch. main untouched.
- No Railway deploy actions — code only. Operator deploys per CUTOVER.md.
- GCP work не запускает actual VM — только artifacts ready.
- Если Generator упадёт API 500 — coherence check (см. memory `feedback_subagent_crash_recovery.md`)
- Independent Evaluator на каждом sprint (см. memory `feedback_independent_evaluator_pattern.md`)

## After all 7 sprints

1. `git log --oneline feat/revenue-and-tee` — должно быть 7 commits
2. `pytest tests/ -x` — full suite green
3. `git checkout main && git merge feat/revenue-and-tee --no-ff` — Lead approval first
4. Follow operator action items в `product-spec.md` to deploy
