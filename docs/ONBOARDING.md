# Task-Ledger — Intern Onboarding

Welcome. You're being handed a working system with an honest history and
an explicit end goal, and you're being asked for the thing insiders can't
produce: an outside judgment of what's valuable, what isn't, and what's
missing. This document is everything you need to form one.

**Contents:** [What this is](#what-this-is) ·
[The rules it lives by](#the-rules-it-lives-by) ·
[How it works](#how-the-pipeline-works) ·
[Day one](#day-one-getting-it-running) ·
[Current state vs. end goal](#current-state-vs-end-goal) ·
[Questions we want your eyes on](#questions-we-want-your-eyes-on) ·
[Ground rules](#ground-rules) · [Glossary](#glossary) ·
[Where to look things up](#where-to-look-things-up)

---

## What this is

People say what they're working on, out loud, in meetings — and then it
evaporates. Task-ledger listens to meeting transcripts and turns spoken
statements into a tracked board of work: it decides which sentences
represent actual work, whether that work is already tracked (and should
receive a progress update, a completion, a deadline change) or is novel
(and should be proposed as a new task), and it attributes everything —
who said it, who owns it, when, in which meeting.

The end goal, in the project owner's words: **an automated system that
intakes raw transcripts from Copilot / Microsoft Graph, comprehends
which verbal statements represent actual work, matches them to existing
tasks or creates new ones, with correct attribution of owners,
requesters, and deadlines.**

The one-sentence pitch that survives every demo: *it hears everything,
forgets nothing, asks little, and never writes a fact nobody vouched
for.*

## The rules it lives by

These are architectural commitments, not aspirations. Breaking any of
them is a redesign, not a patch. They're also shown to every user in the
dashboard header ("the deal").

1. **Everything runs locally.** Transcripts, models (Ollama), database
   (Postgres) — nothing touches a cloud. This is the privacy argument
   that makes the system approvable at all.
2. **Nothing reaches a board without a named human saying yes.** The
   system proposes; people decide. There is no other write path.
3. **Every fact keeps its receipt** — the sentence, speaker, meeting,
   date, and content hash that produced it. Append-only tables enforce
   that history doesn't change (see `forbid_mutation` in
   `storage/schema.sql`).
4. **It counts its own mistakes.** Every rejection is recorded with a
   reason and feeds calibration. Misses are scored against the system.
5. **There is no scorecard of people, and there never will be.** It
   tracks work, not workers. A said-vs-done leaderboard would poison
   the candid speech the whole system feeds on.

## How the pipeline works

Nine stages, each with a home in the code. The through-line: raw speech
becomes typed *signals*, signals become *proposals* or *candidates*,
humans confirm, and only then does a board change.

1. **Ingest** — transcripts become window files in an inbox.
   `scripts/transcript_ingest.py` parses Teams/Word exports;
   `scripts/copilot_ingest.py` parses Copilot pastes. Windows are
   chunked at **8 utterances** (`extraction/windowing.py`) — measured:
   larger windows make the model summarise away project names.
2. **Extract** — one LLM call per window turns speech into typed
   signals (task_mention, status_update, completion_claim, assignment,
   blocker, deadline_signal…). `extraction/extraction_worker.py`.
   Authorship comes from the transcript, never from the model.
3. **Pool** — signals route to the people they concern (owners, else
   the speaker). `extraction/signal_store_pg.py::pool_targets`.
   Everything is idempotent by content hash: replays insert nothing.
4. **Match** — each pooled signal is compared to the person's tasks by
   embedding similarity over title+alias sets, in bands
   (`policy/matching_policy.yaml`): ≥0.90 auto-links; 0.62–0.90 asks an
   LLM judge; ≥0.80 with a failed judge becomes a **human question**
   (the "ask band"); below floor waits. `reconciler/reconciler.py`.
5. **Gate** — per signal type: does this become a proposal? Closures
   need the speaker to be the owner or corroboration. Untracked work is
   held (retried each tick, expiring after 7 days) AND registered as a
   candidate.
6. **The candidate register** — mentions of work on nobody's board
   accumulate per person. Promotion to a proposal needs corroboration:
   2 sightings or 2 distinct speakers within 14 days. One-off noise
   ages out. `storage/migrate_task_candidates.sql` has the founding
   argument.
7. **Review** — proposals arrive in a per-person packet. `tl review`
   (terminal) or the dashboard popup. Accepting teaches: the wording
   just confirmed becomes an alias, so the same paraphrase auto-links
   next time and questions get rarer. `cli/review.py`.
8. **The board** — event-sourced, projections rebuilt from the log,
   every change tied to a decision. `apply_task_event` in the schema is
   the single write path.
9. **Readers** — `tl brief` (what this recurring meeting said last time
   and what became of it), `tl recall` (who said that, quoted, with
   receipts), `tl handover` (everything one person carries), the
   dashboard (`tl dashboard`), weekly digests, `tl ask`.

The cadence: `cadence/cadence_tick.py` runs the whole loop — inbox →
extract → reconcile every user → notify. It takes an advisory lock (one
tick at a time), checks its database and models before spending
anything, and explains every zero: held signals print what they almost
matched and how close.

## Day one: getting it running

You need: **Python 3.10+, PostgreSQL, and (only for extraction) Ollama**
with `nomic-embed-text` plus a chat model. Start without Ollama — the
demo path doesn't need it.

```
git clone <repo> && cd task-ledger
./tl                      # doctor: tells you everything missing, in order
./tl demo                 # seeds a reviewable synthetic state, no Ollama
./tl review               # answer cards in the terminal
TL_VIEWER_PICKER=1 ./tl dashboard    # http://127.0.0.1:8090
```

(Windows: `tl.bat` everywhere; see `deploy/windows/WINDOWS_PORT.md`.)

**Habits that will save you days** (each learned expensively):

- **Run `./tl` (doctor) first, always.** It checks services, schema,
  migrations, models, and pipeline state, and names the fix for
  anything missing. If doctor is red, nothing else is worth running.
- **The tick explains itself.** `models:` and `promotion:` lines show
  the configuration actually in force; `held:` lines show what didn't
  match and why. Read the log before theorizing.
- **`./tl test` before any push.** The full suite forces itself onto a
  scratch database and covers every subsystem.
- **Synthetic data for everything.** `./tl demo-set` generates meeting
  transcripts with planted answers; `./tl score` measures extraction
  against them. You never need real meeting content to develop.

## Current state vs. end goal

**Working today, proven end-to-end on real meetings:** transcript in →
work comprehended → matched or created → progress attached → completion
proposed → human ruled → board updated with receipts. The full loop has
run on a real corpus; the review/teaching cycle works; briefs, recall,
and handover read the results.

**Manual today:** transcripts are collected by hand (Copilot export or
Teams/Word export pasted into a file), ingested with
`tl transcripts <file> --skip-processed`, and the tick runs hourly via a
scheduled task (or by hand).

**The gap to the end goal, in order:**

1. **Live Graph intake.** `integration/tenant/graph_ingestion.py`
   already implements change-notification subscriptions — it has never
   been wired live. The blockers are auth setup and, critically,
   **tenant/IT approval for processing real meeting content** — an
   organizational gate, not a code gate. Until then, manual collection
   (or a watched drop-folder) is the bridge.
2. **A per-person delivery surface.** Today, reviewing someone's queue
   means switching `TL_USER` — fine for one operator, wrong for a team.
   The intended surface is a Teams card that stamps identity from the
   authenticated user (the code comments have always assumed it). This
   is the largest unbuilt piece.
3. **Deadlines and requesters.** Deadline plumbing exists end-to-end
   but is essentially unexercised; "who asked for this" is not modeled
   at all.

## Questions we want your eyes on

You're the outside perspective. These are genuinely open — argue with
any of them:

- **Is the extraction good enough?** `./tl score` measures it against
  golden labels, and it has *not* been re-run since window size changed
  to 8. Quantify it. Would a better local model (or an approved hosted
  one) change the product?
- **Part vs. whole.** "The capability is done" gets matched to the
  whole project. Subtask structure exists in the schema
  (`parent_task_id`, `add_subtask`) but the extractor rarely emits it.
  Worth fixing? How?
- **Is the candidate bar right?** 2 sightings / 2 speakers / 14 days —
  chosen from measured corpora, but small ones. The rejection reasons
  in `decisions` are the data to judge it with.
- **What would make a team actually adopt this?** The skeptic's
  objections and answers are in the dashboard ("the deal") and the git
  history (`face the skeptic`). What's still missing?
- **What should be deleted?** Some features may not earn their keep.
  An outsider can say so; say so.
- **No CI.** The suite is scratch-DB-clean and would port to a GitHub
  Action with a Postgres service. Probably the best first contribution.

## Ground rules

- **Real meeting data never enters the repo, a cloud, or your machine.**
  Develop against synthetic corpora only (`testdata/`, `./tl demo-set`).
  The real ledger lives on the owner's hardware and stays there.
- **Append-only means append-only.** If a table refuses your UPDATE,
  that's the design working — read the trigger's comment before
  fighting it.
- **Config must be read by code.** A policy key nothing reads has
  caused multi-day incidents twice. If you add a knob, add the code
  that reads it in the same commit.
- **When a run produces zero, find the line that says why** — every
  stage prints its reasons. The project's whole history is silent zeros
  becoming printed sentences; keep it that way.

## Glossary

| Term | Meaning |
|---|---|
| **window** | 8 utterances of transcript; the unit of one extraction call |
| **signal** | one typed claim extracted from speech (mention, status, completion…) |
| **pool** | per-person queue of unconsumed signals awaiting reconcile |
| **candidate** | work heard about but on nobody's board, accumulating sightings |
| **sighting** | one recorded mention of a candidate (one per signal, ever) |
| **promotion** | a corroborated candidate becoming a create proposal |
| **packet** | one person's batch of proposals awaiting review |
| **judge** | the LLM asked "are these the same task?" in the 0.62–0.90 band |
| **ask band** | ≥0.80 similarity where a failed judge yields a human card |
| **alias** | an alternate wording a task is known by; aliases make matching learn |
| **held** | a signal waiting for its target to exist; retried each tick, expires in 7 days |
| **consumed** | a signal that produced its outcome and left the pool |
| **receipt** | evidence ref: utterance id + content hash behind any fact |

## Where to look things up

- `DEVELOPMENT.md` — build/test workflow. `cadence/CADENCE.md`,
  `PROFILES.md`, `deploy/windows/WINDOWS_PORT.md` — subsystem docs.
- `docs/system_map.html` — an interactive atlas of every component with
  live code excerpts; rebuilt by `./tl map`, verified by the suite.
- **`git log` is the real documentation.** Every commit message here
  explains what broke, how it was measured, and why the fix is shaped
  the way it is. When you wonder "why is this like this," `git log -S
  <term>` will usually answer with a story.
- The policy file `policy/matching_policy.yaml` is config *and* design
  essay; read it once, entirely.

Welcome aboard. Run the demo, break things on synthetic data, and write
down everything that confused you — that list is your first
deliverable, because it's the one thing nobody here can see anymore.
