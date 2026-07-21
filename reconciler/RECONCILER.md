# Reconciler: Signals + Board -> ReviewPackets

The last stage. Implements `matching_policy.yaml` against the contracts: signals in, schema-valid ReviewPackets out. Files: `reconciler.py` (core), `embedders.py` (Ollama + offline token embeddings), `board_io_pg.py` (Postgres on both ends), `run_reconcile.py` (CLI), `test_reconciler.py` (26 checks including a live end-to-end that reconciles the actual mistral-extracted signals from the extraction module's database write).

## 1. Shape

```
v_unconsumed_signals + board (alias sets, blockers, deadlines, age)
  -> MATCH    embed vs alias sets (max-sim), short-title penalty,
              bands: auto_link / judge (top-k via judge_prompts) / new
              guards: closures always judged; cross-owner always judged;
              malformed judge output fails to distinct
  -> GATE     per-signal-type op rules from the policy
  -> HOLD     corroboration failures are NOT consumed -- they stay pooled
  -> AGGREGATE  same-op/same-target combine noisy-OR (capped), op floors
  -> ReviewPacket  deterministic ids, rationales, lineage, conflicts-first
                   ordering -- validated against reconciliation_diff_schema
```

The core takes plain dicts and injected callables (embed, judge LLM, clock), so it tests fully offline and runs identically against Ollama locally or Azure in production. `board_io_pg` closes the loop with the store: loading signals and the board, and persisting the packet + `taken` pool events + alias annotations in one transaction, idempotent on replay. The task↔task merge scan from the policy is a separate periodic concern, deliberately out of the per-user packet path.

## 2. The gates, as built

Trust proportional to blast radius, exactly as the policy table specifies:

- **task_mention** proposes nothing; a linked mention appends its title to the task's alias set (via a system `alias_added` event at packet write), which is the compounding trick that keeps matching working as vocabulary drifts.
- **proposal** creates only when nothing matched (with `match_note` recording the best rejected candidate); a matched proposal is just another alias.
- **assignment** walks the ladder: `explicit_assignment` reassigns cleanly; `self_reference` claims unowned tasks but raises `competing_owner` on owned ones; `inferred` never proposes, full stop. An unmatched assignment creates the task with owner and deadline attached.
- **status_update** proposes forward/lateral transitions plainly and flags regressions (`completed -> in_progress`) with `stale_evidence` — proposed, never silently applied.
- **deadline_signal**: vague precision annotates instead of proposing; resolved dates carry `raw_text` onto the card; a non-owner suggesting someone's deadline gets a conflict flag.
- **blocker** attaches with a best-effort `blocking_task_id` resolution (token overlap against the task graph), turning blockers into dependency edges where possible; **blocker_resolved** must match a specific open blocker or it holds.
- **completion_claim / cancellation** run the strictest path: never auto-linked, judge >= 0.85, then the speaker-attribution ladder — owner self-claim closes on one source; anyone else needs a second independent source (distinct primary evidence); tasks younger than a day hold even owner claims (the "done" echo gate).

**Hold semantics are the quiet backbone.** Held signals are simply not consumed — no state, no timers — so they remain in the pool and re-enter the next reconcile, where corroboration may have arrived. The build surfaced one gap the tests caught: a closure that failed to match was being consumed-and-dropped; it now holds, because a failed match on a "done" claim is far more likely a matching failure than real new work.

## 3. What the test suite proves

Offline (TokenEmbedder gives controllable lexical cosine; judges are scripted): band mechanics including the short-title penalty and no-judge-above-auto-link; regression conflicts; the full completion matrix (owner single-source passes, third-party single-source holds, corroborated third-party aggregates two signals into one proposal with merged evidence, malformed judge holds); the assignment ladder end to end; subtask verdicts becoming `add_subtask`; deadline and blocker gates including edge resolution; floors and mention aliasing; the cross-owner judge guard.

Live (skips itself without a database): loads the *actual signals mistral-small3.1 extracted* in the extraction module's test run, seeds and backdates an OCR task, reconciles — completion judged onto the task, assignment creating the alerting task, the third proposal surviving at exactly the create floor — persists the packet, proves write idempotency, and confirms the pool shrank by exactly the consumed count with held signals remaining.

## 4. Running the full local loop on the Mac

With the portable Postgres up and the extraction module's signals written (`test_extraction.py` does that when the DB is reachable), the whole system chains on one machine:

```zsh
cd reconciler
pip install pyyaml jsonschema psycopg2-binary
python3 test_reconciler.py

# offline matching (judge-band signals held):
python3 run_reconcile.py --user aad-dev \
    --dsn "host=localhost port=5433 dbname=taskintel user=postgres"

# real embeddings + local judge, and persist the packet:
ollama pull nomic-embed-text
python3 run_reconcile.py --user aad-dev \
    --dsn "host=localhost port=5433 dbname=taskintel user=postgres" \
    --embed-model nomic-embed-text --judge-model mistral-small3.1 --write
```

That is the complete pipeline locally: transcript -> `run_local.py` (extraction, Ollama) -> signal pool -> `run_reconcile.py` (matching + judge, Ollama) -> packet in Postgres -> visible in the pending-review query, decidable through the orchestrator, landing on the dashboard with provenance. One calibration note for local judging: `judge_accept_min`/`0.85` were tuned as starting points for strong models — watch mistral's `deciding_factor` outputs before trusting its merges, and remember the failure mode is graceful either way (bad judge output holds; it never guesses).

## 5. Remaining seams

Two deliberate simplifications to revisit with real data: corroboration independence approximates "different speakers" as "different primary evidence resources" (stored signals don't carry speaker identity — adding an `author_ref` column to signals would tighten it), and `blocker_resolved`/`blocking_task_id` matching uses token overlap where embeddings would generalize better. Both are contained inside single functions and neither weakens a safety gate — they only affect recall.
