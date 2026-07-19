# Review Loop: Cadence Orchestrator and Adaptive Card

Companion to `CONTRACTS.md` and `MATCHING_POLICY.md`. Reference implementation in `orchestrator.py` (lifecycle state machine) and `review_card.py` (Adaptive Card 1.5 renderer); `simulate.py` exercises the full lifecycle.

## 1. Scheduling model

Cadence is **per-user, timezone-local, working-days-aware** — a single global tick at 9am UTC is wrong for any distributed team. The natural Azure shape: a timer-triggered function fans out one Durable Functions orchestration per user, each of which runs the tick pipeline:

```
supersede any open packet -> take unconsumed signals -> reconcile against
current board -> empty? SILENT SKIP : deliver card -> await decisions
(event-driven, not polled) -> apply -> terminal state
```

Two scheduling rules that matter more than the cron expression:

- **Silence is a valid output.** If reconciliation produces zero proposals, no card is sent. Consumed signals that matched already-confirmed state stay consumed. A daily "nothing to review!" card trains people to ignore the bot within a week; absence of a card must mean absence of work.
- **Expiry runs on its own clock.** A separate sweep (hourly is fine) expires packets past `expires_at` independent of cadence, so a user on PTO doesn't accumulate a wall of stale cards — they return to at most one live packet, built fresh against the board as it is now.

Delivery is a Bot Framework **proactive message** using a stored `conversationReference` per user (captured at install). The card uses Universal Actions (`Action.Execute`), so decisions arrive as invoke activities to the bot — not as chat messages — and the same card payload works in Teams and Outlook.

## 2. Packet lifecycle

```
draft -> delivered -> partially_decided -> closed
             |               |
             +-> expired     +-> expired
             +-> superseded  +-> superseded
```

Transitions are enforced (illegal ones raise), and three rules carry the correctness weight:

**Supersede returns signals, never copies proposals.** When a new cadence fires while a packet is still open, undecided proposals are *not* carried forward. Their source signals go back to the unconsumed pool and are re-reconciled against the current board. Replaying a stale diff is exactly the bug the `board_version` field exists to prevent; re-deriving the diff is always at least as correct and usually more so (the board moved, other signals arrived, corroboration windows matured).

**Decisions re-validate at apply time.** `board_version` gives optimistic concurrency: a user can manually complete a task on the dashboard between card delivery and card interaction. On accept, the proposal is checked against the *current* board (`proposal_still_valid`); if the board moved underneath it, the proposal is invalidated with a reason and the card refreshes to show it — never blind-applied. Decisions are idempotent on `(packet_id, proposal_id)`, which matters because Teams users double-tap.

**Defer is a signal-return, not a snooze flag.** A deferred proposal's signals rejoin the pool and get re-reconciled next tick. If the world changed by then, the user sees the *updated* proposal, not a rerun of the old one.

Every terminal transition triggers a card refresh so the message history never shows a live-looking card for a dead packet: closed packets collapse to a receipt line, superseded ones to a "superseded by the 16:00 review" stub, expired ones to a note that items will resurface.

## 3. Card layout decisions

**Ordering: conflicts first, then descending confidence.** The items genuinely needing human judgment sit above the fold; the tail is stuff the model is confident about, which is what the bulk action is for.

**Per proposal:** op chip (color-coded: closes are Good, destructive/merge are Attention) + task title, the one-sentence rationale, a compact FactSet (the concrete change, confidence, and — for deadlines — the `raw_text` phrase the date was resolved from, so bad relative-date resolution is catchable at a glance), an attention-styled conflict banner when set, then the action row.

**Actions per proposal: Accept | Edit | Reject | Later.**
- *Reject* opens a ShowCard with the enumerated reason ChoiceSet ("Why? — tunes future proposals"). Making the reason one extra tap instead of free text is what actually populates the calibration loop from §5 of the matching policy; nobody types rejection essays.
- *Edit* opens an op-specific ShowCard: title+date inputs for creates, date for deadline changes, status ChoiceSet for status changes. Ops without a meaningful single-field edit (merge, reassign) get no Edit action — complex edits belong on the dashboard, and a half-capable inline editor is worse than none.
- *Evidence* renders as `Action.OpenUrl` buttons deep-linking to the source Teams message / email (the transport layer resolves EvidenceRef → `webUrl` at render time, preserving the pointer-not-copy rule: the card contains links, never quoted content, so permission trimming and retention still apply to what the user sees when they click).

**Bulk actions: "Accept all unconflicted" and "Defer all."** Bulk accept deliberately excludes conflicted proposals — those exist precisely because they need eyes. There is intentionally no "Reject all": bulk rejection without reasons destroys the eval signal, and a user who wants everything gone has Defer all, which costs nothing (signals just re-reconcile).

**Size discipline.** Teams caps cards around 28KB. The renderer caps at 8 proposals per card, orders overflow behind a "Show N more" action that pages via `card/refresh`, and hard-fails the build if a card exceeds the limit — a delivery-time surprise is worse than a render-time exception.

## 4. Decision ingestion (bot side)

The bot's invoke handler is thin by design: parse verb → map to a DecisionRecord (stamping `decided_by` from the *authenticated Teams identity*, never from card data — card payloads are client-supplied and trivially forgeable) → `orchestrator.apply_decision` → return the re-rendered card as the invoke response. `card/refresh` re-renders current packet state for whoever opens the thread. Bulk verbs expand server-side into individual DecisionRecords so the audit log never contains a compound "accepted 6 things" entry — every board mutation stays traceable to one decision on one proposal.
