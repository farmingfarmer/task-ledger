---
id: task_ledger_architecture
display_name: Task Ledger Architecture
aliases: [task ledger pipeline, task ledger design]
type: documentation
status: active
owner: Alex
related: [task_ledger]
---

# Task Ledger Architecture

The pipeline has six stages and two cross-cutting rails. Stage 01
Capture: Microsoft Graph subscriptions (or a local drop-folder inbox)
deliver message pointers; the WindowBatcher groups bursts per
conversation, closing windows after 60-180 seconds of quiet with a
30-minute age cap. Stage 02 Extract: one LLM call per window returns
typed signals in strict JSON; a salvage layer keeps good signals out of
malformed responses; hallucination guards delete claims that cite
nonexistent messages, name unknown people, or use deadline-removal
language. Stage 03 Pool: signals are written idempotently under
content-hash identity and pooled per person in an append-only event
ledger (pooled, taken, returned); held work is simply unconsumed rows.
Stage 04 Reconcile: embedding similarity sorts signals into bands
(auto-link at 0.90 and above, LLM judge in the gray zone, new-task
below); closures and cross-owner claims always face the judge; a
malformed judge answer parses as no-match, which means held. Per-type
gates encode trust proportional to blast radius: completion claims need
owner self-reference or two independent sources, and sub-day-old tasks
hold even owner claims. Aggregation combines same-op proposals under
noisy-OR with run-salted proposal ids. Stage 05 Review: packets deliver
as Teams Adaptive Cards (paginated under the 28KB cap) or a local CLI;
both write identical DecisionRecords; accepts re-validate against the
current board and stale accepts are recorded as invalidated, not forced.
Stage 06 Board: a read-only Flask dashboard with field-level redaction
by viewer grant (progress vs full_trace); every confirmed fact offers a
provenance walk: board event to decision to proposal to signals to
evidence hashes.

Rail one is the Proving Ground: a seeded synthetic messy-comms generator
with guaranteed daily hard cases and a scorer verified honest in both
directions. Rail two is Cadence: a weekday 16:45 tick that ingests,
reconciles, and notifies only when proposals await; a down Ollama
degrades the tick (inbox waits, offline TokenEmbedder, judge-band holds)
and never fails it. Storage-layer supersede guarantees at most one open
packet per person regardless of how many runs stack up.
