# Task Intelligence Contracts

Two contracts define the system: the **ExtractionBatch** (what the LLM reports from communications) and the **ReviewPacket / DecisionRecord** pair (what the reconciliation engine proposes and what the human decides). Everything else — ingestion, dashboard, storage — is plumbing around these.

## Pipeline position

```
Graph webhooks ──> ingestion queue ──> EXTRACTION ──> signal store
                                            │
                                     [ExtractionBatch]
                                            │
              task graph (versioned) ──> RECONCILIATION
                                            │
                                      [ReviewPacket]
                                            │
                                   Adaptive Card in Teams
                                            │
                                     [DecisionRecord]
                                            │
                                   event log ──> materialized board ──> dashboard
```

## Core invariants

1. **Extraction is board-blind.** It never sees task ids and never decides anything. It converts a communication window into typed signals with evidence. This keeps the extraction prompt simple, makes batches idempotent and re-runnable, and means a reconciliation bug never corrupts extraction history.

2. **Reconciliation never writes.** Its only output is a diff (ReviewPacket). Every board mutation requires a DecisionRecord. The board is therefore always human-confirmed state, never model-inferred state.

3. **Evidence is a pointer, never a copy.** EvidenceRef stores Graph resource ids + spans + a content hash. Evidence text is fetched at render time under the viewer's own permissions, so the provenance UI inherits Microsoft 365 permission trimming, sensitivity labels, and retention automatically. If the hash no longer matches (message edited/deleted), the UI flags it instead of silently showing different text.

4. **Deterministic ids.** batch_id and signal_id are content hashes. Re-processing a window (backfill, model upgrade, crash recovery) produces the same ids, so dedup is a set operation, and A/B-ing two extractor versions over the same window is directly diffable.

5. **Optimistic concurrency via board_version.** A packet is computed against a board version. If the user manually edits their board before reviewing, affected proposals are rebased or marked `conflict` — never blind-applied.

6. **Rejections are the eval set.** DecisionRecord.reject_reason is enumerated, not free-text-only, so you can chart *why* proposals fail (bad date resolution vs. duplicate detection vs. not-a-task noise) and know which stage to tune. `accept_edited` deltas are equally valuable: they show near-misses.

## Why signals and proposals are separate types

A signal is *what was said*; a proposal is *what should change*. One proposal may aggregate several signals (three messages across two days all pointing at the same status change), and one signal may produce zero proposals (it matched already-confirmed board state — no diff, no noise on the review card). Collapsing the two types is the classic mistake: it forces the extractor to know the board, destroys idempotency, and makes every model upgrade a data migration.

## Notes on specific fields

- **`completion_claim` as its own signal type.** "Done" claims close things, so they get stricter reconciliation treatment (e.g. require explicit-speaker attribution, lower auto-confidence) than ordinary status chatter.
- **`deadline.raw_text` + `precision`.** Relative-date resolution ("end of next sprint") is a top failure mode. Carrying the raw phrase to the review card lets users catch it in one glance, and `bad_date_resolution` rejections quantify it.
- **`owner_candidates[].basis`.** Explicit assignment ("Alex, can you take this") vs. self-reference ("I'll handle it") vs. inference should map to different confidence policies in reconciliation. Inferred ownership should essentially never auto-propose a reassign without a conflict flag.
- **`blocking_task_id` on blockers.** When a blocker resolves to another task on the graph it becomes an edge, not a string. That's what makes the cross-user dependency view ("who is blocked on me") possible later — it costs nothing now and is very expensive to retrofit.
- **`match_note` on create_task.** Duplicate creation is the top rejection reason for creates. Showing "closest existing match and why it wasn't merged" on drill-down builds trust and speeds review.
- **`expires_at` on packets.** Unreviewed packets must expire, or a user returning from PTO faces 40 stale cards. Expired signals simply re-reconcile against the then-current board next run.
- **`defer` decision.** Distinct from reject: "true, but I'll deal with it later." Deferred proposals resurface next cadence without counting as model errors.

## Storage sketch

Append-only tables: `extraction_batches`, `signals`, `review_packets`, `proposals`, `decisions`. One materialized view: `tasks` (current state, rebuilt from decisions). Drill-down is then a pure walk: task → decision → proposal → signals → evidence refs → live Graph fetch. Nothing on the dashboard is ever unexplainable, and nothing sensitive is duplicated outside the tenant boundary.
