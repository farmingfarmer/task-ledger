# Reconciliation Matching Policy

Companion to `CONTRACTS.md`. Defines how a board-blind signal becomes (or fails to become) a proposal: candidate generation, similarity bands, the LLM judge, per-signal-type op gates, and the calibration loop that keeps thresholds honest. All constants live in `matching_policy.yaml`; the judge prompts live in `judge_prompts.py`.

## 1. Matching pipeline

```
signal ──> BLOCKING ──> EMBED + RANK ──> band?
                                          ├── sim >= auto_link ────────────> linked
                                          ├── judge_floor <= sim < auto ──> LLM JUDGE ──> linked / subtask / new
                                          └── all sims < judge_floor ─────> new-task path (+ match_note)
linked signal ──> PER-TYPE OP GATES ──> proposal (or annotation / hold / discard)
```

**Blocking** cuts the candidate set before any embedding math: tasks whose owner/watchers intersect the signal's `owner_candidates` (plus unowned tasks), excluding terminal tasks except those closed within the last 14 days. That exception matters — a `status_update` matching a just-completed task should surface as a conflict-flagged reopen proposal, not a duplicate create. Blocking is also the privacy boundary: a signal never gets compared against tasks its speaker has no relationship to.

**Ranking** embeds `title + description` of the signal and compares against each candidate's **alias set** — the canonical title, description, and up to 12 previously *accepted* signal titles. Matching against aliases rather than the canonical title alone is the single highest-leverage trick in this design: people call the same work "the Fargate migration," "the readmission deploy," and "getting us off EC2" in the same week, and each accepted match teaches the task its next name for free. Task similarity = max over its aliases.

## 2. Similarity bands and why the numbers sit where they do

Starting values for `text-embedding-3-large` cosine similarity (recalibrate for any other model — absolute cosine values are not portable across embedding models):

- **auto_link ≥ 0.90.** At this level against a human-confirmed alias with owner agreement, judge calls add latency and cost without changing outcomes. Two carve-outs never auto-link regardless of similarity: `completion_claim`/`cancellation` (they close things — always judged), and any cross-owner match (owner disagreement is exactly the ambiguity a judge exists for).
- **judge band 0.62–0.90.** Wide on purpose. Embedding similarity is good at recall and mediocre at precision in exactly this range — paraphrases, subtask/parent confusion, and sequential phases ("design X" vs "implement X") all land here. Top-3 candidates go to the judge in one call.
- **below 0.62 → new task.** The best rejected candidate and its score are recorded in `match_note` so a reviewer rejecting the create as `duplicate` is simultaneously labeling a threshold failure.
- **short-title penalty.** Signals with ≤4-token titles ("fix the pipeline") subtract 0.07 from raw similarity — generic titles over-match everything, and the penalty pushes them into the judge band where context can save them.

The asymmetry running through all of this: **a false merge corrupts confirmed history; a false new-task is one annoying card.** Every tie in the policy breaks toward not-matching, and both judge prompts encode "if unsure, choose related."

## 3. The judge

One output schema for both judges: `{verdict, matched_task_id, confidence, deciding_factor}` with verdict ∈ `same_task | subtask_of | related | distinct`. Three deliberate choices:

1. **Relationship, not boolean.** `subtask_of` maps straight to the `add_subtask` op — the drill-down hierarchy builds itself from matching, no separate classifier. `related` maps to an annotation, never an op.
2. **`deciding_factor` is mandatory.** One sentence naming the field or phrase that decided it. It flows into `match_note`/`judge_rationale` and is the main thing a human reads before trusting a merge. It also makes judge regressions reviewable: when acceptance drops, you read deciding factors, not raw transcripts.
3. **Identity = identity of deliverable.** The prompts define same-task as "the same artifact or outcome changes when the work is done," and explicitly disqualify shared project names, shared people, shared meetings, shared stack, and timing as evidence. Sequential phases and recurring work ("July retrain" vs "August retrain") are named non-duplicates. These negative examples do more work than the positive definition.

The merge judge (periodic task↔task dedup scan, pair floor 0.80, propose only at judge confidence ≥ 0.85) adds one rule: `matched_task_id` must name the *surviving* task — richer confirmed history wins, ties to the older task. A malformed judge response fails toward `distinct`, and the raw text is logged for prompt regression review.

## 4. Per-signal-type op gates

Full table in `matching_policy.yaml → signal_rules`. The shape of the policy:

**Trust is proportional to blast radius.** `task_mention` proposes nothing ever — its only job is feeding alias sets. `status_update` needs conf 0.60 and a forward/lateral transition; regressions (completed→in_progress) are allowed but always conflict-flagged. `complete_task` and `cancel_task` sit at the top: extraction conf ≥ 0.75, the strictest match bar of any type (auto-link or judge ≥ 0.85), and — the rule you called out — speaker attribution:

- **Owner says it's done** (`self_reference` or explicit statement by owner): a single signal suffices.
- **Anyone else says it's done**: held for corroboration — a second *independent* signal (different evidence resource AND different speaker) within 7 days. No corroboration → no proposal; the task gets a quiet annotation ("completion reported by X on DATE, unconfirmed") and the claim expires. Third-party "I think Bob finished that" is the #1 source of wrongly-closed tasks in any system like this, and closing someone's task out from under them is the fastest way to lose the room.

**Ownership follows the same ladder.** `explicit_assignment` may propose reassign on an owned task. `self_reference` may claim an *unowned* task only; claiming an owned one yields a `competing_owner` conflict, not a reassign. `inferred` attribution never proposes anything — annotation only.

**Deadlines:** `precision: vague` never proposes (annotate `raw_text` only); a non-owner moving someone's deadline gets a conflict flag unless they're the explicit assigner.

**Aggregation:** multiple same-op signals in a window combine via noisy-OR capped at 0.97 (agreement raises confidence, never certainty). Conflicting ops on one task in one window emit the highest-confidence proposal with the others attached as competing evidence.

Every gate failure has a defined **downgrade** — annotate, hold, pool, or discard — and downgrades never escalate. The system's failure mode is silence plus a footnote, not a wrong card.

## 5. Calibration loop

The starting thresholds are educated guesses; DecisionRecords make them empirical. Weekly job:

1. Join decisions back to proposals → match path (auto / judge / new) → raw similarity and judge confidence.
2. Sweep `auto_link`, `judge_floor`, `judge_accept_min` against accept/reject labels; pick values hitting the per-op **precision targets** in the yaml (create 0.70 … complete/cancel/merge 0.90). Precision is the binding constraint, recall secondary: a missed signal resurfaces in a later window, but a bad proposal burns trust immediately and trains users to bulk-reject.
3. `reject_reason` histograms route the fix: `duplicate` on creates → judge_floor too high or alias sets too thin; `wrong_task_match` → auto_link too low or blocking too loose; `bad_date_resolution` → extraction problem, not matching — leave the thresholds alone.
4. Drift alarm: any op's weekly acceptance rate dropping >15 points off its trailing 4-week mean pages a human before users notice. Acceptance shifts when communication patterns shift (new team, new meeting formats) — thresholds calibrated in March quietly rot by June.

`accept_edited` deltas get special handling: an edit to the *payload* (right task, wrong date) is an extraction miss; a rejection with `wrong_task_match` is a matching miss. Keeping those ledgers separate is what lets you tune one stage without thrashing the other.
