# Storage: Event Log, Materialized Board, and Drill-Down Paths

Final piece of the design set (`CONTRACTS.md`, `MATCHING_POLICY.md`, `REVIEW_LOOP.md`). PostgreSQL 14+; validated against Postgres 16. `schema.sql` is the DDL, `queries.sql` the named query paths (Q1-Q9), `validate_storage.sql` the seed-and-assert suite.

## 1. Shape

```
extraction_batches -> signals -> evidence_refs          (append-only)
signal_pool_events                                       (append-only; pool = latest event)
review_packets -> proposals -> proposal_signals          (append-only)
packet_state_events                                      (append-only lifecycle)
decisions                                                (append-only; UNIQUE idempotency)
task_events  =====================================>      THE board (append-only)
   |                                              write-through in same txn
   +--> tasks_current / task_blockers / task_aliases     (mutable projections)
user_boards                                              (per-user version counter)
```

Ten tables are INSERT-only, enforced by a `forbid_mutation()` trigger on UPDATE/DELETE — not convention, a raised exception. The three mutable tables are projections of `task_events`, and `rebuild_board(user)` reconstructs them from events alone.

## 2. Invariants and how they're enforced

**Single write path.** `apply_task_event()` is the only way the board changes: it increments the per-user version in `user_boards`, appends the event, and maintains the projections in one transaction. `UNIQUE (user_ref, board_version)` on `task_events` makes version forks structurally impossible, and the validation suite asserts versions are contiguous. Manual dashboard edits use the same function with `actor_type='manual_edit'` and NULL `decision_id` — a `CHECK` requires `decision_id` whenever `actor_type='user_decision'`, so an AI-originated change can never masquerade as human-typed or arrive without its decision.

**Projection honesty is tested, not trusted.** Write-through projections are the classic place where event-sourced systems silently rot: the inline update logic and the rebuild logic drift apart. The validation suite snapshots `tasks_current`, runs `rebuild_board()` for every user, and diffs — any divergence fails the build. Run this in CI on every schema or writer change; run the rebuild in anger only for disaster recovery.

**Idempotency where retries happen.** `decisions` carries `UNIQUE (proposal_id, decided_by)` — the double-tap anchor the orchestrator relies on. `signals`/`extraction_batches` use content-hash primary keys, so re-processing a window is an upsert-shaped no-op. Schema-level `CHECK`s encode the contracts: rejects require a `reject_reason`, `edited_payload` only with `accept_edited`, conflict type and detail travel together.

**Evidence stays a pointer.** `evidence_refs` holds Graph resource ids, spans, and content hashes — no message text anywhere in this database. The drill-down UI resolves ids to deep links at render time under the viewer's own permissions. This is also the retention story: when M365 retention deletes a source message, this system holds a dangling pointer and a hash, not a leaked copy.

**The pool is events, not flags.** `signal_pool_events` (`pooled`/`taken`/`returned`) with `v_unconsumed_signals` as a latest-event-wins view. The orchestrator's supersede/defer/expire semantics ("return signals to the pool") become one INSERT, and the full custody history of every signal — which run took it, which packet returned it — is queryable forever.

**Cross-user edges are first-class.** `task_blockers.blocking_task_id` is a real FK to another user's task. This forced the one bug the validation caught: per-user rebuild deletes/re-inserts task rows that other users' blockers reference, which fails under immediate FK checking. The task-graph FKs are therefore `DEFERRABLE INITIALLY DEFERRED` — constraints check at commit, by which point the rebuilt rows are back. (Worth remembering: the fix is deferred constraints, not dropping the FK — the edge is what powers Q4.)

## 3. Query paths (queries.sql)

| # | Path | Powers |
|---|------|--------|
| Q1 | Board view: current tasks + open blocker counts + subtask counts + overdue flag | Dashboard top level |
| Q2 | Task history: events joined to decisions and proposals, in board order | Drill-down level 1 — every row says *what* changed and *who/what* caused it (`actor_type` distinguishes human-typed, human-confirmed-AI, system) |
| Q3 | Provenance walk: one event -> proposal -> signals -> evidence pointers | Drill-down level 2 — "why does the board say this," terminating in deep links |
| Q4 | Who is blocked on me: open blocker edges pointing at my tasks | Cross-user dependency view |
| Q5 | Team rollup: status counts + overdue per user | Manager tiles |
| Q6 | Open packets + undecided counts | Review-latency ops view, "items waiting" nudge |
| Q7 | Weekly acceptance rate per op vs trailing 4-week mean, with drift alarm | Matching-policy calibration (section 5) |
| Q8 | Reject-reason histogram per op | Routes fixes: `duplicate` -> thresholds, `bad_date_resolution` -> extraction, `wrong_task_match` -> auto-link |
| Q9 | Alias sets (canonical title + confirmed aliases) for non-terminal + 14-day-closed tasks | Reconciler candidate generation |

The indexes follow the queries, not the other way around: partial indexes on open blockers and on blocker edges (Q1/Q4), `(user_ref, status)` on the projection (Q1/Q5), `(user_ref, board_version)` and `(task_id, event_id)` on events (Q2, rebuild), a descending-latest index on pool events (the view). `task_aliases` has a commented `vector(3072)` column — enable pgvector there when alias-embedding matching moves into the database instead of the reconciler's memory.

## 4. Validation summary

`validate_storage.sql` seeds a realistic slice through the intended write paths (manual creates, an accepted status change, an `accept_edited` deadline, an accepted blocker with a cross-user edge, a rejected create, alias capture, pool take/return) and asserts: append-only triggers actually block mutation, the projection matches expected state, board versions are contiguous, `rebuild_board()` reproduces the projection exactly for all users, the provenance walk reaches evidence pointers, the cross-user dependency query resolves, and the pool view tracks take/return. All assertions pass against Postgres 16.
