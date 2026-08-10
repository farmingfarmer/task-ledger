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

Prompt extract-v1.2 rewrote owner attribution around a rule the earlier
prompts never stated: leave the owner empty when the sentence identifies
nobody. Seed 7: recall 100 percent (33 of 33), attribution 74 percent, 1
negation trap hit, 0 fabrications, 1 salvage drop. Seed 11: recall 94
percent (32 of 34), attribution 81 percent, 0 traps, 0 fabrications.
Both seed 7 figures reproduced exactly across two runs, so the
attribution drop is a real disagreement rather than sampling noise: this
corpus credits the sender of an email with owning work their message
describes impersonally, while v1.2 reads that shape as naming nobody.
Against planted labels on synthetic meetings using a real participant
directory the same rule scores 92.2 percent, up from 81.2 percent on
identical claims, and 98.6 percent on a held-out set — the two corpora
encode different conventions for authored messages, and the rule does
not yet distinguish them.

Prompt extract-v1.3 gave authored sources -- email threads and documents
-- one exception to that rule: when a message does not identify who holds
the work, the sender does. Every other source type keeps prompt text
byte-identical, so the meeting figures above stand without re-running.
The first attempt at this exception measured as a no-op on both corpora,
because it fired only when a message named nobody while the sentence it
was written for names the team the work waits on; the precondition was
corrected from "no name appears" to "no owner is identified". Final:
seed 7 recall 100 percent, attribution 79 percent, 1 negation trap hit, 0
fabrications; seed 11 recall 94 percent, attribution 81 percent, 0 traps,
0 fabrications -- unchanged because its email windows had no headroom
left.

Test suites, run from one script against a scratch database: 56
extraction, 40 reconciler, 7 supersede, 18 review CLI, 12 share CLI, 31
digest, 22 ask, 27 attribution-eval, 14 synthetic-generator and 16
cadence checks, plus 49 dashboard access checks and an inline-JS check --
292 in total, with storage and access SQL validation suites alongside and
tenant webhook checks outside the runner; the reconciler
suite was triple-run clean (26, 26, 26) on a fresh database to prove the
Windows replay fix. The Windows port completed on a locked-down no-admin
ThinkPad (portable PostgreSQL 18.4, Python 3.14) with the full offline
demo working: extraction 5 signals, 2 proposals written offline, 1
closure held, review CLI and dashboard functional. The system atlas
carries 40 AST-extracted components across 8 domains and 20 modules with
narrative layer totals of 8 pipeline lines, 28 domain steps, and 74
walkthrough steps.
