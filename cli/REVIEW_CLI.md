# Review CLI: The Local Human Loop

The terminal equivalent of the Teams review card. `cli/review.py` closes the last gap in the local system: proposals were landing in Postgres with no way to answer them short of hand-written SQL. Now:

```zsh
python3 review.py --as aad-dev --dsn "host=localhost port=5432 dbname=taskintel user=$(whoami)"
```

lists what's pending and walks each proposal — op, target task and its *current* status, rationale, the concrete change, conflict warnings, duplicate-check notes, deadline raw phrases, evidence pointers with hashes — then takes `[a]ccept [r]eject [d]efer [s]kip [e]dit-accept [q]uit`. Rejects prompt the enumerated reason menu (the calibration loop's food). Edit-accept offers op-appropriate fields: title/deadline for creates, date for deadline changes, status for status changes. `--list` shows the queue without opening it; `--accept-unconflicted` is the bulk sweep, skipping conflicts and board-moved proposals exactly like the card's bulk button.

## Why it's safe to exist

It reuses the production paths rather than inventing local ones:

- **Every answer is a DecisionRecord** under the schema's constraints — rejects carry reasons, edited payloads only with `accept_edited`, and `UNIQUE(proposal_id, decided_by)` makes double-answers no-ops.
- **Accepts re-validate against the current board first.** A proposal whose target completed, cancelled, or merged away since packet generation is recorded with `invalidated_reason` and *not applied* — the orchestrator's optimistic concurrency, mirrored.
- **Board mutations go only through `apply_task_event()`**; creates mint `task-<hash>` ids, merges emit one `merged_into` per absorbed task, and the CLI never touches `tasks_current`.
- **Defers return signals to the pool** for re-reconciliation — same semantics as the card and the expiry sweep.
- **The whole session is one transaction**: quit mid-way or crash, and nothing partial lands.
- **Packet lifecycle stays honest**: opening a draft records `delivered`, the first answer records `partially_decided`, emptying it records `closed` — so Q6 and the dashboard's pending views tell the truth regardless of which surface did the reviewing.

`--as` is trusted local identity — right for a single-operator machine, and explicitly the consenting-adult sibling of the Teams host's authenticated stamping.

## Validation

`test_review.py` (11 checks) drives a scripted session over a real reconciler-produced packet: display ordering (conflicts first, then confidence), accept/edit-accept/defer each verified through to board state and event lineage, deferred signals observed back in the pool, the full lifecycle sequence asserted from `packet_state_events`, and idempotency confirmed by a re-run finding nothing.

## Where it points

This is the daily driver until Azure: launchd runs extraction + reconcile on cadence, and reviewing becomes a two-minute terminal habit — which is precisely what starts filling the reject-reason histograms the threshold calibration has been waiting for. When the Teams card ships, this stays as the power-user and offline path; both surfaces write the same records, so nothing forks.
