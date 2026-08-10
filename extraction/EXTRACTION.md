# Extraction Worker: Communication Window -> Signals

Implements the extraction stage specified in `CONTRACTS.md` / `extraction_schema.json`. Files: `extraction_prompts.py` (the prompt), `extraction_worker.py` (the pipeline), `signal_store_pg.py` (Postgres persistence + pooling), `run_local.py` + `sample_transcript.json` (local iteration against Ollama or Azure OpenAI), `test_extraction.py` (21 checks, including live-database idempotency).

## 1. Shape

```
job (from WindowBatcher / transcript poll)
  -> fetcher (Graph in prod; injected)         content items, never stored
  -> build_messages                            numbered items + window end date
  -> LLM (Ollama / Azure OpenAI; injected)     one call per window
  -> parse + salvage                           per-signal validation
  -> name resolution                           display names -> person refs
  -> evidence assembly                         citations -> resource ids + sha256
  -> deterministic ids                         batch_id / signal_id content hashes
  -> jsonschema.validate                       against extraction_schema.json
  -> signal_store_pg.write_batch               idempotent insert + pooling
```

Everything external is injected (fetcher, LLM, clock), so the whole pipeline tests offline with a fake model and runs identically against `mistral-small3.1` on a laptop or a GPT deployment in Azure — the batch records which (`extractor.model` + `prompt_version`), which is what makes cross-model comparison a SQL query later.

## 2. Prompt design decisions

**Citations are the contract.** The window is presented as numbered items and every signal must cite the item numbers supporting it. The worker converts citations into EvidenceRefs — Graph resource id plus a sha256 of the item text at extraction time — and a signal with no valid citations is dropped, full stop. This is what makes "Trace to source" on the dashboard real rather than decorative: the model cannot emit an unanchored claim that survives.

**Negative guidance gets equal billing.** Extractors fail by over-firing, so the prompt spends as many words on what is *not* a signal — opinions, uncommitted questions, hypotheticals, reminiscing about long-finished work, meeting meta-talk — as on the type definitions, and closes with "if you would hedge the title with maybe, do not emit it." The sample transcript's item 6 ("we should probably rebuild the docs... not urgent") exists precisely to exercise the proposal/noise boundary during prompt iteration.

**The closed sets downstream depends on are defined inline.** Attribution basis (explicit_assignment / self_reference / inferred, with examples), status values, deadline precision. `completion_claim` gets its own conservative framing ("almost done is a status_update") because the matching policy treats completions as the highest-blast-radius signal.

**Attribution has a fourth answer, and the prompt has to say so.** Three bases with no stated permission to return *none* is an instruction to always find someone, and that is what the model did: scored against planted labels, every false attribution was an impersonal sentence — "the retention sweep is stuck waiting on the platform team", "let's get it scoped" — pinned on whoever happened to say it. So an empty `owner_candidates` is described as normal and frequent, and the test for it is **structural, not a confidence threshold**: no first person and no person named means no owner. "When in doubt, inferred" was removed for the same reason — doubt was exactly the state that produced the guessing.

The same passage names the failures in the other direction, because under-attribution and over-attribution are one gap, not two: self_reference is defined by *first person* rather than by signal type (a blocker and a completion are as self-referential as a commitment, and transcripts routinely drop the leading "I"), a request addressed to someone by name belongs to the person addressed, and a report about a third party belongs to the person named rather than the one reporting.

**A recap is not a conversation, and says so.** Some tenants will not
release meeting transcripts; what Copilot hands over instead is a recap --
subjects grouped under the people who raised them, with no utterances, no
timestamps and no commitment language. That arrives as its own source type,
`meeting_recap`, rather than being dressed up as one of the others, because
two things follow from it. Its evidence is `meeting_insight`, never
`transcript_utterance`: a bullet points at a paraphrase of several things
somebody said, and routing it to an utterance type would claim a
sentence-level receipt the data cannot support. And it is deliberately not
an AUTHORED source, because a recap has no sender -- it is machine-written
about other people, so the sender-owns-the-work rule would attribute
everybody's work to nobody in particular.

The prompt tells it to emit sightings only, and a worker guard enforces
what the prompt cannot: any `completion_claim`, `blocker`,
`blocker_resolved`, `deadline_signal`, `cancellation` or `status_update`
from a recap window is deleted with reason `recap_cannot_witness_state`,
and any owner basis stronger than `inferred` is demoted, since nobody said
anything for the model to have heard. Measured on a real recap: ten bullets
in, ten `task_mention`s out, every owner `inferred`, every evidence ref
`meeting_insight`. Prefer a transcript, a chat thread or an email thread
whenever one is available -- those carry the words people actually chose,
which is what attribution and provenance both rest on.

**Dates resolve against a given anchor.** The prompt supplies the window end date; the model resolves "Friday" and "end of next sprint" against it or returns null — "never guess a date" is in the output rules, and the worker enforces it anyway (below).

## 3. Worker policies

**Salvage, don't sink.** Each signal validates independently; malformed ones drop with a recorded reason (`bad_type`, `missing_title`, `no_valid_evidence`, `missing_confidence`, `not_an_object`, `duplicate`) while good neighbors survive. A fully unparseable response yields a schema-valid *empty* batch plus a report entry — never an exception into the queue runtime. The report is the tuning channel: drop-reason frequencies tell you exactly which prompt rule the model is failing.

**Salvage works at two levels, because per-signal salvage does nothing when the array itself breaks.** `_parse_json_array` is all-or-nothing: one stray character and every signal in the response is lost. That is not hypothetical — a window carrying four contradictory claims about one item drove the model to 14,899 characters that broke at character 6,847, and the whole window extracted as nothing. `_salvage_objects` re-reads such a response for brace-balanced top-level objects, tracking string literals and escapes so a brace inside a title cannot shift the depth count, and recovered **41 usable objects** from that same response.

It is a reader, not a repairer: nothing is patched, closed, or inferred, and a truncated tail is counted as broken rather than completed, because a half-read signal is a fabricated one. `report["salvaged"]` carries the rescued count, the broken count, and the original parse error, so a window that needed salvage is visible rather than quietly thinner than it should have been.

**A window can still exceed the socket timeout.** That same window takes 480–680s against `run_local`'s 300s limit, so it never completes and the cadence tick re-queues it every run — which looks exactly like the normal degraded-mode wait for Ollama. Salvage does not fix that; it only means a response that *does* arrive is no longer discarded wholesale.

**Hallucination guards are worker-side, not prompt-side hopes.** Non-ISO dates are nulled (raw phrase kept — the review card shows the human the phrase either way). Owner names that don't resolve through the injected directory are dropped entirely, because the schema forbids display names and a guessed AAD id is worse than no attribution. Unknown basis values demote to `inferred`, never promote. Confidence clamps to [0,1].

**Determinism end to end.** `batch_id = sha(source_ref, window, prompt_version)`; `signal_id = sha(batch_id, type, primary evidence, title)`. The suite proves identical ids across re-runs even when one response arrives fenced and the other bare — which is what makes crash-recovery replays and A/B prompt comparisons safe downstream.

**Validation is a gate, not a log line.** The assembled batch must pass `extraction_schema.json` before the worker returns it; the schema is located automatically in both the repo layout (`../contracts/`) and flat layouts.

## 4. Persistence and pooling

`write_batch` inserts batch -> signals -> evidence -> pool events in one transaction, `ON CONFLICT DO NOTHING` on the content-hash keys; pool events are written only for *newly inserted* signals, so replaying a window never double-pools (asserted against the live database). Pooling policy (`pool_targets`): a signal pools to every resolved owner candidate; a signal with **no** resolvable owner pools to its `author_ref` alone — the person who said it. It used to fan out to every participant in the window, on the reasoning that the human review would sort out whose it was. The first live run priced that reasoning: one unowned claim arrived on five boards, four people rejected it as not theirs, and 22 of 27 `wrong_owner` rejections collapsed to 10 claims rejected repeatedly. Broadcast survives only behind `broadcast_unowned=False`, and a signal with no owner and no author is left `unrouted` rather than sprayed. Content never persists: items are fetched, hashed, cited, and discarded.

## 5. Running it locally

On the Mac against Ollama (worth iterating here before spending Azure tokens):

```zsh
cd extraction
python3 run_local.py --transcript sample_transcript.json --model mistral-small3.1
```

Batch to stdout, salvage report to stderr. Add `--write-db "host=localhost port=5433 dbname=taskintel user=postgres"` to persist into the portable Postgres and watch the signals appear in `v_unconsumed_signals`. Azure: set `AZURE_OPENAI_ENDPOINT/KEY/DEPLOYMENT` and pass `--backend azure`. The transcript file format (job + directory + items) is the fixture format for building an eval set: accumulate real-ish transcripts with expected signals, and the salvage report plus schema validation give pass/fail for free.

The expected read on the sample transcript, for calibrating any model you point at it: a status_update (Alex, drift monitor), a blocker (Priya, IAM role), a completion_claim (Dev, OCR fix, self-reference), an assignment (Dev, alerting, explicit_assignment, deadline "end of next week" resolving to 2026-07-31), and a proposal (feature store docs, unowned) — with item 7's meeting talk producing nothing.

## 6. What this unlocks

With extraction real, the remaining unbuilt stage is the **reconciler**: take `v_unconsumed_signals` + the board, apply `matching_policy.yaml` with real embeddings and the judge prompts, emit ReviewPackets. Once that exists, the full loop — transcript in, card out, decision in, dashboard fact with provenance — runs end to end on one machine with no cloud dependencies.
