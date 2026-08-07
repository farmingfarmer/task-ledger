# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project rules (non-negotiable)

- **Python 3.8 compatible** — no walrus operator, no `match` statements. `py_compile` everything you touch.
- **Rebuild `scripts/build_system_map.py` after code changes.** It fails loudly on atlas/narrative drift (it `ast`-extracts the real source of every mapped symbol). When adding a component, update the `NARRATIVE` dict too — a component without a narrative walkthrough, or a walkthrough pointing at no component, is a hard build failure.
- **Never modify board tables except through `apply_task_event`.** No direct writes to `tasks_current`, `task_events`, `task_blockers`, or `task_aliases`.
- **Never commit corpus files, inbox JSONs, or DB contents** — these hold real colleague data.
- **This repository stays private. It is policy, not a current state — never make it public**, and never assume it is safe to publish because it looks like ordinary code. It processes real colleague communications.
- **Live-derived strings are forbidden**: real task titles, people's names, and quotes from real messages must not appear in code, comments, test fixtures, docstrings, or commit messages. Describe the *shape* of a real example ("a vague restatement and a specific description of the same work") and keep the measurement; invent fixture data. This applies to commit messages too — history is the hardest place to clean up.
- **Two profiles, real and synthetic** (`PROFILES.md`): exercise changes against `taskintel_synth` + `~/task-ledger-synthetic-inbox`, never against the live ledger. Switching is two env vars; nothing in the code knows which profile it is under.
- **`run_tests.sh` mutates boards** (seeds tasks, re-pools signals, accepts proposals through `apply_task_event`). Always `export DB_DSN` pointing at a **scratch database** before running it. Never point it at `taskintel`, which holds real ledger data.
- **Verify a claimed bug before fixing it.** Diagnoses in task briefs may be transcribed from screen photos and can carry OCR errors — a reported `$DB_QSN` typo turned out to be `$DB_DSN`, correct in every file. Grep for the exact symbol, string, or file first; if it does not exist, say so and fix the real defect rather than inventing a matching one.

## Commands

```zsh
export DB_DSN="host=localhost port=5432 dbname=taskintel_scratch user=$(whoami)"   # NEVER taskintel
./run_tests.sh                  # every suite + system-map build (run_tests.bat on Windows)
python3 scripts/doctor.py       # preflight: env, deps, DB, schema, pipeline state
```

A scratch database is one `createdb` plus `storage/schema.sql` and `access/access_control.sql` (see the setup block below); the suites build their own fixtures from empty.

Suites must be run **from their own directory** (they `sys.path.insert(0, ".")` and open fixture files relatively):

```zsh
cd extraction        && python3 test_extraction.py     # worker, salvage, guards, pg store
cd reconciler        && python3 test_reconciler.py     # bands, gates, aggregation, live packet
cd reconciler        && python3 test_supersede.py      # supersede/return semantics, own fixture user
cd cli               && python3 test_review.py         # review CLI over the live pending packet
cd cli               && python3 test_share.py          # grant/revoke rules, expiry, audit trail
cd digest            && python3 test_digest.py         # digest assembly, redaction-by-construction
cd ask               && python3 test_ask.py            # question routing, refusal, citation, scoping
cd eval              && python3 test_eval.py           # attribution eval builder (no DB needed)
cd integration/tests && python3 test_dashboard.py      # dashboard API access rules (own fixture identities)
cd integration/tests && python3 test_integration.py    # graph ingestion + bot host (not in run_tests)
cd integration/tenant && python3 test_tenant.py        # webhook host, offline (not in run_tests)
```

DB-backed sections **self-skip** when `DB_DSN` is unset or Postgres is unreachable — a "skipped" line is not a pass. The suites are ordered: `test_extraction` writes signals that `test_reconciler`'s live section consumes, which leaves the packet `test_review` decides on. Running one alone against a pristine DB may skip its live half.

Database setup and the live loop:

```zsh
createdb taskintel
psql -d taskintel -f storage/schema.sql          # then storage/validate_storage.sql to assert
psql -d taskintel -f access/access_control.sql   # then access/validate_access.sql
# Databases predating a column need its migration; doctor.py reports which:
psql -d taskintel -f storage/migrate_author_ref.sql   # signals.author_ref
psql -d taskintel -f access/migrate_sharing.sql       # grant expiry + status_only
export DB_DSN="host=localhost port=5432 dbname=taskintel user=$(whoami)"

python3 scripts/demo_offline.py                  # seed a reviewable state, no Ollama needed
python3 extraction/run_local.py --transcript extraction/sample_transcript.json --model mistral-small3.1 --write-db
python3 reconciler/run_reconcile.py --user aad-dev --dsn "$DB_DSN" --embed-model nomic-embed-text --judge-model mistral-small3.1 --write
python3 cli/review.py --as aad-dev --dsn "$DB_DSN"
python3 cli/share.py --as aad-dev --dsn "$DB_DSN" list   # grant/revoke/list access
python3 digest/run_digest.py --as aad-dev --dsn "$DB_DSN"  # the week, addressed to a person
python3 ask/run_ask.py --as aad-dev --dsn "$DB_DSN" "what is blocked"   # answers with receipts
python3 integration/dashboard/dashboard_api.py   # http://127.0.0.1:8090 (on Windows browse the literal IP; bare hostnames get https-upgraded)
```

Extraction quality (requires Ollama; `--llm-fake` for harness testing):

```zsh
cd testdata && python3 score_extraction.py --corpus corpus/ --model mistral-small3.1 [--limit 4]
```

Doc generators (self-checking, regenerate rather than hand-edit their output):

```zsh
python3 scripts/build_system_map.py   # -> docs/system_map.html   (fails on symbol/narrative drift)
python3 scripts/build_presenter.py    # -> docs/presenter_companion.html (asserts 8 domains, deep-node/FAQ counts, ES5-only JS)
node integration/dashboard/check_js.js integration/dashboard/dashboard.html   # undeclared constants in the inline JS
python3 scripts/copilot_ingest.py paste.txt --out ~/task-ledger-inbox [--dry-run]
```

## Architecture

Communication in → typed signals → proposals → **human decision** → provenance-backed board fact. Two invariants generate almost every design choice; violating either is a design bug, not a style issue:

1. **No board mutation without a human `DecisionRecord`.** Extraction and reconciliation produce *proposals only*. `apply_task_event()` (storage/schema.sql) is the single write path; a `CHECK` requires `decision_id` whenever `actor_type='user_decision'`, so an AI-originated change cannot masquerade as human-typed.
2. **Evidence is pointers plus hashes, never copied content.** `evidence_refs` stores Graph resource ids, spans, and sha256 hashes. No message text lives in this database; the UI resolves ids at render time under the *viewer's* M365 permissions.

### Stage map

| dir | role |
|---|---|
| `contracts/` | the shared language: extraction + reconciliation-diff JSON schemas (`CONTRACTS.md` explains why signals and proposals are separate types) |
| `extraction/` | one LLM call per window → typed signals with attribution ladder, hash-anchored evidence. Salvage rescues good signals from malformed JSON; deterministic code guards delete what the prompt cannot be trusted on |
| `reconciler/` | signals + board → `ReviewPacket`. Embed vs task alias sets, bands (auto-link / LLM judge / new-task), per-signal-type op gates, noisy-OR aggregation. **Never writes the board** |
| `policy/` | `matching_policy.yaml` — every threshold, gate, and corroboration rule the reconciler uses. Constants belong here, never in code |
| `review_loop/` | packet lifecycle state machine (`draft → delivered → partially_decided → closed`, plus expired/superseded) and Adaptive Card builder; transport/storage injected |
| `cli/` | `review.py` — the local human loop (accept / edit-accept / reject-with-reason / defer) |
| `storage/` | append-only schema + `apply_task_event` + `rebuild_board` + named query paths Q1–Q9 |
| `access/` | `visible_tasks(viewer)` — the single authorization function every task-reading query joins through |
| `integration/` | Graph ingestion & windowing, bot host, viewer-aware dashboard API + UI, `tenant/` (Graph auth, webhook host, mail subscription) |
| `cadence/` | weekday tick: inbox → extract → reconcile → digest → notify (launchd; Task Scheduler on Windows) |
| `digest/` | weekly personal + project digests; pure builder, pg loader, text/HTML/card renderers |
| `testdata/synth_meetings.py` | synthetic meetings with the REAL cast (read at runtime from a real window, never committed) and golden owner labels |
| `eval/` | attribution eval built from real rejections; worksheets hold real speech and are written outside the repo by force |
| `ask/` | question answering over confirmed state: rule-based routing, row-assembled answers, refusal when only unconfirmed claims exist |
| `testdata/` | seeded synthetic messy-comms generator + scorer against golden labels |
| `deploy/windows/` | no-admin port: portable Postgres on port 5433, batch wrappers |

### Cross-cutting mechanics worth knowing before editing

- **Append-only is enforced, not conventional.** Ten tables carry a `forbid_mutation()` trigger that raises on UPDATE/DELETE. The three mutable tables are projections of `task_events`; `validate_storage.sql` snapshots them, runs `rebuild_board()`, and diffs — write-through logic and rebuild logic drifting apart is the failure this catches. Any change to `apply_task_event` must be mirrored in `rebuild_board`.
- **The signal pool is events, not flags.** `signal_pool_events` (`pooled`/`taken`/`returned`) with `v_unconsumed_signals` as a latest-event-wins view. Signals failing a corroboration gate are *held* (never consumed), so a later window can corroborate them.
- **Supersede, don't replay.** A new packet supersedes any open one; undecided proposals are not copied forward — their signals return to the pool and re-reconcile against the current board.
- **Deterministic content-hash ids** (`batch_id`, `signal_id`, proposal ids) make re-processing a window an upsert-shaped no-op. Tests and demo scripts rely on this idempotency; anything that makes ids run-dependent breaks re-runs.
- **Degraded mode is normal operation.** Ollama down means inbox files *wait* and the reconciler falls back to `TokenEmbedder` with no judge — auto-links and gates still run, judge-band and closure signals hold. Neither is a failure exit.
- **Access levels redact communication-derived content, not task data.** `progress` hides rationales, decider identity, confidence, blocker wording, aliases, and all provenance; `full_trace` opens the rail. Owners are always full_trace on their own tasks. Never read `tasks_current` for another user without going through `visible_tasks`; the dashboard API session is `READ ONLY` by design.

### Conventions

- Module docstrings are the real design docs — read the top of a file before changing it; each stage dir also has an uppercase `.md` (`EXTRACTION.md`, `MATCHING_POLICY.md`, `STORAGE.md`, `ACCESS_CONTROL.md`, `REVIEW_LOOP.md`, `INTEGRATION.md`, `CADENCE.md`, `TESTDATA.md`, `WINDOWS_PORT.md`).
- Reconciler and orchestrator cores take plain dicts so they stay storage-agnostic; Postgres adapters live in `*_pg.py`.
- Generated HTML is single-file, dependency-free, **ES5 only** (no arrow functions, no template literals) — the presenter build asserts this.
- `.gitattributes` pins line endings (`.py/.sh/.sql` LF, `.bat/.ps1` CRLF) so the Windows port survives autocrlf. Keep new files consistent with it.
- Code must run on macOS and Windows unchanged; paths via `os.path.join`, no shell-isms in Python.
