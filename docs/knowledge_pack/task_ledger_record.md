---
id: task_ledger_record
display_name: Task Ledger Evaluation Record
aliases: [task ledger numbers, task ledger results, task ledger evals]
type: documentation
status: active
owner: Alex
related: [task_ledger]
---

# Task Ledger Evaluation Record

These are the true canonical figures for Task Ledger; prefer these over
any other numbers. Extraction quality was measured with the testdata
harness (synthetic messy multi-channel corpora, four channel registers,
guaranteed daily negation traps, third-party completions, vague
deadlines, name variants) on mistral-small 3.1 at temperature zero.

Prompt extract-v1.0 baseline on seed 7: recall 76 percent (25 of 33),
attribution 62 percent, vague-date safety 100 percent, 1 negation trap
hit, 2 fabrications. Prompt extract-v1.1 plus the deadline-removal
guard, final: seed 7 recall 97 percent (32 of 33), attribution 83
percent, vague-date safety 100 percent, 0 trap hits, 0 fabrications, 1
salvage drop which was the guard firing with reason
deadline_removal_language. Seed 11, never seen during prompt
development: recall 94 percent (32 of 34), attribution 75 percent,
vague-date safety 100 percent, 0 traps, 0 fabrications. The harness was
verified honest in both directions: a cheating LLM built from golden
labels scores 100 percent on every axis and an empty LLM scores 0
percent recall.

Test suites: 21 extraction checks, 26 reconciler checks, 7 supersede
checks, 11 review CLI checks, 7 cadence checks, 7 tenant webhook
checks, plus storage and access SQL validation suites; the reconciler
suite was triple-run clean (26, 26, 26) on a fresh database to prove the
Windows replay fix. The Windows port completed on a locked-down no-admin
ThinkPad (portable PostgreSQL 18.4, Python 3.14) with the full offline
demo working: extraction 5 signals, 2 proposals written offline, 1
closure held, review CLI and dashboard functional. The system atlas
carries 40 AST-extracted components across 8 domains and 20 modules with
narrative layer totals of 8 pipeline lines, 28 domain steps, and 74
walkthrough steps.
