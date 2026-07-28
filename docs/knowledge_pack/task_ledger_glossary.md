---
id: task_ledger_glossary
display_name: Task Ledger Glossary
aliases: [task ledger terms, task ledger vocabulary]
type: documentation
status: active
owner: Alex
related: [task_ledger]
---

# Task Ledger Glossary

Signal: one typed claim extracted from communication (assignment,
status_update, blocker, blocker_resolved, completion_claim, proposal,
task_mention, deadline_signal, cancellation), carrying owner candidates
with a basis, optional deadline with raw text and precision, confidence,
and hash-anchored evidence references.

Window: a bounded slice of one conversation (source_type, source_ref,
window_start, window_end) that is the unit of extraction; one window
equals one LLM call.

Pool: the per-person append-only ledger of signals awaiting
reconciliation; events are pooled, taken, and returned, and the current
truth is the latest event per signal.

Attribution basis: how ownership was claimed; the ladder is
explicit_assignment over self_reference over inferred, and inferred
never proposes a reassignment.

Bands: the reconciler's similarity routing; auto_link at 0.90 and
above, judge in the gray zone, new below; closures and cross-owner
signals always go to the judge regardless of score.

ReviewPacket and proposal: a packet is one person's batch of proposed
board changes; each proposal names an op (create_task, complete_task,
update_status, set_deadline, add_blocker, resolve_blocker, add_subtask,
reassign) with rationale and evidence.

DecisionRecord: the recorded human verdict (accept, edit-accept,
reject-with-reason, defer) that is the only thing permitted to mutate
the board, applied through the single function apply_task_event.

Supersede: the storage-layer rule that writing a new packet retires any
open packet for that person and returns its undecided signals to the
pool, guaranteeing at most one open review each.

Provenance rail: the traceability walk from a board event through its
decision and proposal to signals and evidence hashes; gated behind the
full_trace grant, redacted for progress viewers.

Negation trap: benchmark content whose naive reading extracts the
opposite of its meaning, such as parked work read as a new task or a
dropped deadline read as a set one.

Protected APIs: Microsoft Graph endpoints (app-only Teams channel and
chat message subscriptions) that require a Microsoft approval form even
on one's own tenant; mail subscriptions and delegated chat reads are
not gated.
