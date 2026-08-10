#!/usr/bin/env python3
"""Build the Task Ledger system atlas: docs/system_map.html.

A single self-contained HTML file -- pipeline hub, domain views, module
explorer -- carrying the ACTUAL source of ~39 money symbols, extracted by
ast at build time. If a symbol moves or vanishes, the build FAILS LOUDLY:
the refusal is the map's accuracy guarantee. Rebuild after any code
change:

    python3 scripts/build_system_map.py

Pattern: farmingfarmer/synthkit scripts/build_system_map.py.
"""

import ast
import datetime
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "docs", "system_map.html")
MAX_LINES = 90

# ---------------------------------------------------------------- the atlas

ATLAS = {
    "input": "a workplace conversation \u2192",
    "output": "\u2192 a provenance-backed board fact",
    "domains": [
        {
            "id": "capture", "kind": "stage", "stage": "01",
            "name": "Capture", "color": "#3E5C76",
            "note": "Graph subscriptions and a drop-folder inbox\nbecome bounded extraction windows",
            "blurb": "Nothing downstream sees raw streams. Teams, chat, and "
                     "mail arrive as change notifications (or inbox files, "
                     "pre-Graph) and are cut into quiet-edge windows -- the "
                     "unit every later stage reasons about.",
            "modules": [
                {"name": "Graph subscriptions",
                 "file": "integration/ingestion/graph_ingestion.py",
                 "components": [
                     {"symbol": "SubscriptionManager.renew_due",
                      "blurb": "Graph subscriptions expire; this renews at "
                               "half-life so a missed tick never means a "
                               "silent gap in capture."},
                     {"symbol": "SubscriptionManager.transcript_poll_plan",
                      "blurb": "Meeting transcripts aren't push-notified "
                               "like messages -- ended meetings get a "
                               "bounded poll plan instead."},
                 ]},
                {"name": "Windowing",
                 "file": "integration/ingestion/graph_ingestion.py",
                 "components": [
                     {"symbol": "WindowBatcher.add",
                      "blurb": "Per-conversation debounce: 180s of quiet "
                               "closes a window, a 30-minute cap bounds it. "
                               "Content never enters this queue -- only "
                               "resource pointers."},
                     {"symbol": "WindowBatcher.flush_due",
                      "blurb": "The moment a window becomes an extraction "
                               "job. One LLM call per flush is the cost "
                               "model of the whole system."},
                 ]},
                {"name": "Local inbox",
                 "file": "cadence/cadence_tick.py",
                 "components": [
                     {"symbol": "process_inbox",
                      "blurb": "The pre-Graph input path: transcript JSONs "
                               "dropped in a folder. Ollama down means "
                               "files WAIT rather than vanish -- extraction "
                               "loses nothing by being patient."},
                 ]},
            ],
        },
        {
            "id": "extract", "kind": "stage", "stage": "02",
            "name": "Extract", "color": "#2E6E63",
            "note": "one LLM call per window; typed, attributed,\nhash-anchored signals -- or nothing",
            "blurb": "mistral-small3.1 reads a window and emits typed "
                     "signals with attribution ladders and cited evidence. "
                     "Everything it says is checked: salvage keeps good "
                     "signals out of bad JSON, guards delete what can't be "
                     "verified. Measured: 94-100% recall, 0 fabrications on "
                     "synthetic messy corpora.",
            "modules": [
                {"name": "Prompt (extract-v1.3)",
                 "file": "extraction/extraction_prompts.py",
                 "components": [
                     {"symbol": "build_messages",
                      "blurb": "The versioned contract with the model: "
                               "numbered items, mandatory citations, and "
                               "negative guidance grown from measured "
                               "failures (announcement dates, negation "
                               "reversals, hedge-lowers-confidence)."},
                     {"symbol": "NEGATIVE_GUIDANCE",
                      "blurb": "The do-not-emit list, and every clause is "
                               "a scored failure rather than a guess: "
                               "administrative dates read as deadlines, "
                               "negations read affirmatively, hedged "
                               "sightings suppressed instead of "
                               "downgraded. Prompt text is versioned and "
                               "diffed like code because it behaves like "
                               "code -- and what it still cannot fix "
                               "(deadline-removal, 3 of 3 at temperature "
                               "0) graduates to a guard in the worker."},
                     {"symbol": "ATTRIBUTION_SPEC",
                      "blurb": "Who owns the work, and the fourth answer "
                               "most prompts forget: nobody. Given three "
                               "bases and no permission to return none, "
                               "the model pinned impersonal sentences on "
                               "whoever said them -- so 'no first person "
                               "and no name -> empty' is stated as a "
                               "structural rule, not a confidence "
                               "threshold, because 'when in doubt' is what "
                               "produced the guessing."},
                 ]},
                {"name": "Worker & guards",
                 "file": "extraction/extraction_worker.py",
                 "components": [
                     {"symbol": "ExtractionWorker.run_job",
                      "blurb": "One window in, one schema-valid batch out, "
                               "with deterministic content-hash ids -- the "
                               "same transcript can never double-ingest."},
                     {"symbol": "ExtractionWorker._build_signal",
                      "blurb": "The guard gauntlet. Missing confidence "
                               "defaults to 0.6 (small models omit it; "
                               "dropping cost us 5/5 signals on the first "
                               "live run). Deadline-removal language is "
                               "deleted in code after the prompt lost 3/3 "
                               "at temperature 0."},
                     {"symbol": "ExtractionWorker._resolve_owners",
                      "blurb": "Display names (Alexander/Alex/AM) resolve "
                               "to person_refs via the directory; unknown "
                               "people are dropped, bad bases demoted to "
                               "inferred -- attribution feeds gates, so it "
                               "fails conservative."},
                 ]},
            ],
        },
        {
            "id": "pool", "kind": "stage", "stage": "03",
            "name": "Pool", "color": "#6B615C",
            "note": "signals wait, idempotently --\nheld work re-enters on its own",
            "blurb": "An append-only pool per person. Signals a later gate "
                     "can't decide on are simply not consumed -- no timers, "
                     "no state machine -- so corroboration that arrives next "
                     "week meets the claim from last Tuesday.",
            "modules": [
                {"name": "Store",
                 "file": "extraction/signal_store_pg.py",
                 "components": [
                     {"symbol": "write_batch",
                      "blurb": "Idempotent on content-hash ids: re-running "
                               "extraction over the same window inserts "
                               "zero rows. Pooling routes to owner "
                               "candidates, else window participants."},
                 ]},
                {"name": "Pool reads",
                 "file": "reconciler/board_io_pg.py",
                 "components": [
                     {"symbol": "load_signals",
                      "blurb": "v_unconsumed_signals is the single source "
                               "of 'what awaits reconciliation' -- latest "
                               "pool event wins, so holds and returns are "
                               "just rows."},
                     {"symbol": "load_board",
                      "blurb": "The matchable board: alias sets, open "
                               "blockers, deadlines, and task AGE -- which "
                               "the completion youth-gate reads (a "
                               "seconds-old task held its own 'done' echo "
                               "in live testing, exactly as designed)."},
                 ]},
            ],
        },
        {
            "id": "reconcile", "kind": "stage", "stage": "04",
            "name": "Reconcile", "color": "#52619B",
            "note": "embeddings place, a judge verifies,\ngates decide what may even be proposed",
            "blurb": "Signals meet the board through similarity bands: "
                     "auto-link above 0.90, an LLM judge in the gray zone, "
                     "new-task below. Trust is proportional to blast "
                     "radius -- closures are never auto-linked, and a "
                     "malformed judge answer holds rather than guesses.",
            "modules": [
                {"name": "Matching bands",
                 "file": "reconciler/reconciler.py",
                 "components": [
                     {"symbol": "Reconciler._match",
                      "blurb": "The band logic plus its guards: closures "
                               "always judged, cross-owner always judged, "
                               "short titles penalized. mistral judged "
                               "'Fix intake OCR crash' onto 'Fix OCR crash "
                               "in intake' at 0.95 on its first live "
                               "outing."},
                     {"symbol": "build_signal_judge_messages",
                      "file": "policy/judge_prompts.py",
                      "blurb": "The judge sees top-3 candidates and must "
                               "name a deciding_factor -- the field a human "
                               "reads to decide whether to trust local "
                               "judging at all."},
                     {"symbol": "parse_judge_response",
                      "file": "policy/judge_prompts.py",
                      "blurb": "Strict parsing; anything malformed becomes "
                               "'distinct', which downstream means HELD. "
                               "The failure mode of a bad judge is delay, "
                               "never a wrong merge."},
                 ]},
                {"name": "Op gates",
                 "file": "reconciler/reconciler.py",
                 "components": [
                     {"symbol": "Reconciler._gate_closure",
                      "blurb": "The strictest path: owner self-claim on one "
                               "source, anyone else needs independent "
                               "corroboration, sub-day tasks hold even "
                               "owner claims. A closure that fails to "
                               "match is held, not consumed -- a fix found "
                               "when the live loop nearly ate a real "
                               "completion."},
                     {"symbol": "Reconciler._gate_assignment",
                      "blurb": "The attribution ladder as code: explicit "
                               "reassigns, self-claims on owned tasks "
                               "raise competing_owner conflicts, inferred "
                               "never proposes."},
                 ]},
                {"name": "Aggregate & write",
                 "file": "reconciler/reconciler.py",
                 "components": [
                     {"symbol": "Reconciler._aggregate",
                      "blurb": "Same-op proposals combine noisy-OR under a "
                               "cap; per-op floors filter. proposal_id is "
                               "salted with the run -- unsalted ids "
                               "collided across packets in live testing "
                               "and silently wrote zero proposals."},
                     {"symbol": "supersede_open_packets",
                      "file": "reconciler/board_io_pg.py",
                      "blurb": "At most one open packet per person, "
                               "enforced at the storage layer so EVERY "
                               "writer inherits it. Its first live sweep "
                               "cleared four stale packets including two "
                               "invisible classes of debris."},
                     {"symbol": "write_packet",
                      "file": "reconciler/board_io_pg.py",
                      "blurb": "One transaction: supersede, insert, mark "
                               "signals taken, apply alias annotations. "
                               "Empty packets insert nothing -- silence is "
                               "a valid output."},
                 ]},
                {"name": "Embeddings",
                 "file": "reconciler/embedders.py",
                 "components": [
                     {"symbol": "OllamaEmbedder",
                      "blurb": "nomic-embed-text locally; timeout is 600s "
                               "because a cold Ollama load once killed a "
                               "run at 120s while a synthkit campaign held "
                               "the queue."},
                     {"symbol": "TokenEmbedder",
                      "blurb": "Deterministic bag-of-tokens cosine: the "
                               "offline fallback AND the reason band tests "
                               "can place similarities on purpose."},
                 ]},
            ],
        },
        {
            "id": "review", "kind": "stage", "stage": "05",
            "name": "Review", "color": "#9C4A38",
            "note": "a human accepts, edits, rejects, defers --\nnothing applies without a decision record",
            "blurb": "The gate that makes everything upstream safe to be "
                     "wrong. Proposals become DecisionRecords through the "
                     "Teams card or the local CLI -- both write identical "
                     "records, and accepts re-validate against the board "
                     "as it is NOW, not as it was at packet time.",
            "modules": [
                {"name": "Orchestrator",
                 "file": "review_loop/orchestrator.py",
                 "components": [
                     {"symbol": "Orchestrator.apply_decision",
                      "blurb": "Optimistic concurrency: a proposal whose "
                               "target moved is recorded with "
                               "invalidated_reason and NOT applied. The "
                               "first real CLI session hit this exact path "
                               "on a stale completion."},
                     {"symbol": "Orchestrator.expire_sweep",
                      "blurb": "Expired packets return their undecided "
                               "signals to the pool -- unanswered is a "
                               "recoverable state, not a lost one."},
                 ]},
                {"name": "Teams card",
                 "file": "review_loop/review_card.py",
                 "components": [
                     {"symbol": "build_review_card",
                      "blurb": "Adaptive Card 1.5 with Action.Execute; "
                               "paginates at 8 proposals and refuses to "
                               "exceed Teams' 28KB payload cap by "
                               "construction."},
                     {"symbol": "_assert_size",
                      "blurb": "The cap as a hard assert -- a card that "
                               "can't deliver is a review that never "
                               "happens, so oversize fails the build of "
                               "the card, not the delivery."},
                 ]},
                {"name": "Review CLI",
                 "file": "cli/review.py",
                 "components": [
                     {"symbol": "still_valid",
                      "blurb": "The CLI's board re-check before any "
                               "accept: terminal targets, merged-away "
                               "tasks, and vanished targets all block "
                               "application."},
                     {"symbol": "apply_accept",
                      "blurb": "Every accepted op maps to exactly one "
                               "apply_task_event call. The CLI never "
                               "touches tasks_current -- no surface does."},
                 ]},
            ],
        },
        {
            "id": "board", "kind": "stage", "stage": "06",
            "name": "Board", "color": "#33707E",
            "note": "viewer-scoped facts that carry\ntheir own receipts",
            "blurb": "The dashboard reads projections through one choke "
                     "point (visible_tasks) with two grant levels: "
                     "progress redacts rationales, deciders, and "
                     "provenance; full_trace opens the rail. Every "
                     "confirmed fact traces to the utterance hash that "
                     "produced it.",
            "modules": [
                {"name": "Board API",
                 "file": "integration/dashboard/dashboard_api.py",
                 "components": [
                     {"symbol": "board",
                      "blurb": "Read-only sessions, viewer header, every "
                               "query through visible_tasks -- the API "
                               "cannot mutate even if asked nicely."},
                     {"symbol": "blocked_on",
                      "blurb": "The dependency view is owner-only: who is "
                               "waiting on you is your business, not your "
                               "stakeholders'."},
                 ]},
                {"name": "Provenance & redaction",
                 "file": "integration/dashboard/dashboard_api.py",
                 "components": [
                     {"symbol": "provenance",
                      "blurb": "The 'Trace to source' walk: board event -> "
                               "decision -> proposal -> signals -> "
                               "evidence hashes. Returns 403 below "
                               "full_trace -- provenance is a grant, not a "
                               "default."},
                     {"symbol": "_redact_event",
                      "blurb": "progress-level viewers see that things "
                               "happened, not why or by whose word -- the "
                               "redaction is field-by-field, not "
                               "row-by-row."},
                 ]},
            ],
        },
        {
            "id": "proving", "kind": "rail",
            "name": "Proving Ground", "color": "#7C5A80",
            "stage": "rail",
            "note": "synthetic mess in, honest scores out -- every prompt change re-measured on fixed seeds",
            "blurb": "A seeded generator of messy multi-channel comms "
                     "(guaranteed negation traps, hedged claims, vague "
                     "dates, name variants) and a scorer verified honest "
                     "in both directions: a cheating LLM scores 100%, an "
                     "empty one 0%. This is how the prompt has numbers "
                     "at all.",
            "modules": [
                {"name": "Corpus generator",
                 "file": "testdata/synthesize.py",
                 "components": [
                     {"symbol": "build_events",
                      "blurb": "Density plus guarantees: the hard cases "
                               "(traps, third-party completions, vague "
                               "deadlines, cross-channel echoes) are "
                               "never left to the dice."},
                     {"symbol": "render",
                      "blurb": "One event, four registers: transcript "
                               "fillers, chat mangling, email quoting. "
                               "The golden label rides along with each "
                               "rendering."},
                 ]},
                {"name": "Scorer",
                 "file": "testdata/score_extraction.py",
                 "components": [
                     {"symbol": "score_window",
                      "blurb": "Lenient on titles, strict on what feeds "
                               "gates: type family, attribution, dates, "
                               "trap hits. Duplicate sightings are "
                               "deliberately unpunished -- dedup is the "
                               "reconciler's job."},
                     {"symbol": "match_type",
                      "blurb": "Type families forgive defensible "
                               "readings (completion-as-status) without "
                               "forgiving trap readings -- the scorer "
                               "grades meaning, not string equality."},
                 ]},
            ],
        },
        {
            "id": "cadence", "kind": "rail",
            "name": "Cadence & Resilience", "color": "#6E7431",
            "stage": "rail",
            "note": "weekdays 16:45 the system runs itself; a down Ollama degrades, never fails",
            "blurb": "launchd (or Task Scheduler) fires one tick: drain "
                     "the inbox, reconcile, notify only if proposals "
                     "await. Silence is an output; degraded is not "
                     "failure. The supersede layer means unattended weeks "
                     "cannot accumulate stale packets.",
            "modules": [
                {"name": "The tick",
                 "file": "cadence/cadence_tick.py",
                 "components": [
                     {"symbol": "main",
                      "blurb": "Inbox -> reconcile -> pending check -> "
                               "maybe one notification. Exit 0 on an "
                               "empty day AND on a down Ollama -- the log "
                               "is the contract."},
                     {"symbol": "run_reconcile",
                      "blurb": "Ollama up: real embeddings and judge. "
                               "Down: TokenEmbedder and no judge, so "
                               "judge-band work holds for a better day "
                               "instead of being guessed at."},
                 ]},
                {"name": "Ask the ledger",
                 "file": "ask/ask.py",
                 "components": [
                     {"symbol": "answer",
                      "blurb": "Questions answered from rows, never by a "
                               "model: routing is all the model would be "
                               "allowed to do, and here even that is a "
                               "regex. The branch that matters is the last "
                               "one -- when nothing is confirmed it checks "
                               "the held pool and reports 'someone said so' "
                               "rather than 'so', which is the difference "
                               "between a ledger and a rumour mill."},
                 ]},
                {"name": "Weekly digest",
                 "file": "digest/digest_io_pg.py",
                 "components": [
                     {"symbol": "load_project",
                      "blurb": "The stakeholder digest cannot leak the "
                               "discussion behind a change, because this "
                               "query never SELECTS a rationale, a decider, "
                               "a confidence or an evidence pointer. "
                               "Redaction by construction beats redaction "
                               "by filter: there is no field here to forget "
                               "to strip."},
                 ]},
                {"name": "Degradation",
                 "file": "cadence/cadence_tick.py",
                 "components": [
                     {"symbol": "ollama_up",
                      "blurb": "A 3-second reachability probe decides the "
                               "tick's whole posture -- learned the hard "
                               "way when a shared Ollama queue looked "
                               "identical to a hung server."},
                     {"symbol": "notify",
                      "blurb": "Notifies only when pending > 0: osascript "
                               "on macOS, the no-admin PowerShell toast on "
                               "Windows, the log elsewhere. Suppressed in "
                               "tests. Zero pending, zero noise."},
                 ]},
            ],
        },
    ],
}


# ------------------------------------------------------- plain-english layer

NARRATIVE = {
    "pipeline": {
        "capture": ["Listen where work is actually discussed",
                    "Teams, chat, and email arrive as pointers, not copies, "
                    "and get cut into tidy conversation windows"],
        "extract": ["Read each window once and pull out task claims",
                    "a local AI names who owns what, citing the exact "
                    "message for every claim; anything it cannot back up "
                    "is deleted on the spot"],
        "pool": ["Let claims wait their turn",
                 "each person's claims sit in a pool until the evidence is "
                 "sufficient, so nothing is lost and nothing is rushed"],
        "reconcile": ["Match claims against the live board",
                      "obvious matches link automatically, gray areas go "
                      "to an AI judge, and risky changes are held rather "
                      "than guessed"],
        "review": ["Ask a human before anything changes",
                   "every proposed change is accepted or rejected by a "
                   "person, and that decision is recorded forever"],
        "board": ["Show facts that carry their own receipts",
                  "the board displays only confirmed items, each "
                  "traceable to the message that produced it"],
        "proving": ["Grade the AI on synthetic messy days",
                    "a generator fakes realistic workplace chatter with "
                    "traps, and a scorer measures exactly how well "
                    "extraction holds up"],
        "cadence": ["Run itself every weekday at 4:45",
                    "new material is processed on schedule; you are "
                    "pinged only when something needs a decision, and a "
                    "down AI means wait, never fail"],
    },
    "domains": {
        "capture": {
            "plain": "This is the front door. The system never watches a "
                     "raw stream: it receives notifications that a "
                     "message exists, groups nearby messages into a "
                     "conversation window, and hands the window on.",
            "steps": [
                ["Subscriptions tell us a message exists",
                 "Microsoft sends a pointer, never the content -- content "
                 "is fetched only when needed, under the app's own "
                 "credentials"],
                ["Nearby messages become one window",
                 "a burst of chat is one conversation; processing it "
                 "together keeps context and costs one AI call instead "
                 "of twenty"],
                ["Quiet closes the window",
                 "sixty to one-eighty seconds of silence means the burst "
                 "ended; a thirty-minute cap bounds even endless threads"],
                ["A drop folder does the same job locally",
                 "before Microsoft wiring, transcripts dropped in a "
                 "folder ride the identical path -- same format, same "
                 "everything downstream"],
            ],
        },
        "extract": {
            "plain": "One AI call per window turns conversation into "
                     "typed claims: assignments, status, blockers, "
                     "completions. Every claim must cite its message, and "
                     "code -- not the AI -- gets the last word on what "
                     "survives.",
            "steps": [
                ["The AI reads numbered messages and answers in a strict "
                 "format", "numbering makes citations checkable; the "
                 "format makes bad answers detectable"],
                ["Good claims are salvaged from bad answers",
                 "one malformed claim never sinks the batch; each is "
                 "kept or dropped individually, with the reason recorded"],
                ["Uncheckable claims are deleted",
                 "a claim citing a message that does not exist, or "
                 "naming a person we do not know, dies here -- measured "
                 "result: zero fabrications on two synthetic corpora"],
                ["Known AI blind spots are handled in code",
                 "the model kept reading 'the deadline is dropped' as a "
                 "new deadline (three out of three tries), so a "
                 "deterministic guard deletes that pattern with a "
                 "recorded reason"],
            ],
        },
        "pool": {
            "plain": "Claims do not act; they wait. The pool is an "
                     "append-only ledger per person -- pooled, taken, "
                     "returned -- so any moment's truth is just the "
                     "latest entry, and history is never rewritten.",
            "steps": [
                ["Every claim is written once, idempotently",
                 "re-processing the same window inserts zero rows, so "
                 "double-ingestion is structurally impossible"],
                ["Claims wait for sufficient evidence",
                 "a completion nobody corroborated simply stays pooled; "
                 "corroboration arriving next week meets it there"],
                ["The ledger never forgets",
                 "when the Windows port failed mysteriously, the "
                 "pooled-returned-taken trail diagnosed it in one read -- "
                 "the audit design debugging itself"],
            ],
        },
        "reconcile": {
            "plain": "Claims meet the board. Similarity places each "
                     "claim; trust is proportional to blast radius -- "
                     "the riskier the change, the more proof demanded -- "
                     "and everything uncertain is held, never guessed.",
            "steps": [
                ["Similarity sorts claims into three bands",
                 "near-identical links automatically, clearly-new "
                 "creates, and the gray zone goes to an AI judge that "
                 "must name its deciding factor"],
                ["Completions face the strictest gate",
                 "closing work wrongly is the worst error, so owner "
                 "self-claims pass alone but anyone else needs an "
                 "independent second source"],
                ["A confused judge causes delay, not damage",
                 "malformed judge answers count as 'no match', which "
                 "means held -- the failure mode is patience"],
                ["Each run supersedes the last",
                 "at most one open review packet per person, enforced in "
                 "storage; its first sweep retired four stale packets "
                 "including two invisible kinds of debris"],
            ],
        },
        "review": {
            "plain": "The human gate. Nothing -- literally no path in "
                     "the code -- changes the board without a person's "
                     "recorded decision. Reviews re-check the board at "
                     "decision time, because packets age.",
            "steps": [
                ["Proposals arrive as cards or a terminal session",
                 "Teams card and local CLI write identical decision "
                 "records; the interface is cosmetic, the record is not"],
                ["Accepting re-validates against the board as it is NOW",
                 "if the target moved since the packet was built, the "
                 "accept is blocked and the reason recorded -- the first "
                 "real session hit exactly this"],
                ["Rejections teach",
                 "reject reasons accumulate into the tuning data for "
                 "matching thresholds -- the system improves by being "
                 "used"],
                ["Unanswered is recoverable",
                 "expired packets return their claims to the pool; "
                 "silence loses nothing"],
            ],
        },
        "board": {
            "plain": "The payoff surface. A read-only dashboard shows "
                     "each person's confirmed work, and every fact "
                     "carries a receipt: click through to the exact "
                     "message that produced it.",
            "steps": [
                ["The API physically cannot write",
                 "read-only database sessions mean even a bug cannot "
                 "mutate the board from here"],
                ["Viewers see by grant",
                 "colleagues see progress; auditors with full-trace see "
                 "rationale, decider, and provenance -- redaction is "
                 "field-by-field"],
                ["Every fact traces to source",
                 "board event to decision to proposal to claim to "
                 "message hash -- the gold 'Trace to source' walk is the "
                 "whole thesis in one click"],
            ],
        },
        "proving": {
            "plain": "How we know the numbers. A seeded generator fakes "
                     "messy workdays -- traps included -- and a scorer "
                     "grades the real extraction against known answers. "
                     "The harness itself is proven honest first.",
            "steps": [
                ["Fake days include guaranteed hard cases",
                 "negation traps, secondhand completions, vague "
                 "deadlines, and name variants are never left to "
                 "chance"],
                ["The scorer cannot be gamed",
                 "a cheating AI scores 100%, an empty one scores 0% -- "
                 "whatever the real model scores between those poles is "
                 "real"],
                ["Failures become named fixes",
                 "every miss and fabrication is printed with its text; "
                 "prompt version 1.1 was built directly from that list "
                 "and re-measured on an unseen day: 94% recall, zero "
                 "traps, zero fabrications"],
            ],
        },
        "cadence": {
            "plain": "The system runs itself. A scheduler fires one tick "
                     "every weekday afternoon; silence is a valid "
                     "outcome, and a down AI degrades the tick instead "
                     "of failing it.",
            "steps": [
                ["One tick: ingest, reconcile, maybe notify",
                 "you hear from the system only when proposals await -- "
                 "zero pending, zero noise"],
                ["A down AI means wait, never fail",
                 "inbox files stay put, matching falls back to offline "
                 "mode, uncertain items hold for a better day, and the "
                 "tick still exits cleanly"],
                ["Unattended weeks cannot rot",
                 "the supersede rule means packets never stack up no "
                 "matter how long nobody looks"],
            ],
        },
    },
    "walkthroughs": {
        "integration/ingestion/graph_ingestion.py::SubscriptionManager.renew_due": [
            ["Check each subscription's expiry clock",
             "Microsoft subscriptions die every few days by design"],
            ["Renew any past half-life",
             "renewing early means one missed run never becomes a "
             "silent gap in listening"],
        ],
        "integration/ingestion/graph_ingestion.py::SubscriptionManager.transcript_poll_plan": [
            ["Notice which meetings have ended",
             "transcripts are not pushed like messages; they must be "
             "asked for"],
            ["Schedule a bounded series of polite checks",
             "transcripts appear minutes after a meeting; a capped poll "
             "plan catches them without hammering the API"],
        ],
        "integration/ingestion/graph_ingestion.py::WindowBatcher.add": [
            ["File the new message pointer under its conversation",
             "only the pointer -- content is never queued"],
            ["Open a window if none exists, else extend it",
             "a burst of replies becomes one shared window instead of "
             "many fragments"],
        ],
        "integration/ingestion/graph_ingestion.py::WindowBatcher.flush_due": [
            ["Find windows that have gone quiet or grown old",
             "quiet means the burst ended; the age cap bounds endless "
             "threads"],
            ["Turn each into one extraction job",
             "one job equals one AI call -- this line is the system's "
             "entire cost model"],
        ],
        "cadence/cadence_tick.py::process_inbox": [
            ["Look for transcript files in the drop folder",
             "the manual input path -- drop a file, the system ingests "
             "it at 4:45"],
            ["If the AI is down, leave everything untouched",
             "extraction loses nothing by waiting a day"],
            ["Extract, store, and archive each file",
             "processed files move aside so nothing runs twice"],
        ],
        "extraction/extraction_prompts.py::build_messages": [
            ["Number every message and name every speaker",
             "numbered items make the AI's citations checkable by code"],
            ["State what counts and what must be ignored",
             "the ignore-list is battle scars: announcement dates, "
             "negation reversals, and hedge-handling each earned their "
             "line from a measured failure"],
        ],
        "extraction/extraction_prompts.py::ATTRIBUTION_SPEC": [
            ["Name who would do the work -- or name nobody",
             "an owner is only reported when the sentence actually "
             "identifies one: the speaker taking it on, or a person "
             "addressed by name"],
            ["Saying it out loud is not the same as owning it",
             "'the migration is stuck waiting on the platform team' "
             "names nobody, and pinning it on whoever raised it puts "
             "work on the wrong person's board"],
            ["Empty is a correct answer, so the prompt says so",
             "the rule is structural rather than a confidence bar, "
             "because a model told to guess when unsure will always "
             "find a candidate; an unowned signal still gets routed "
             "downstream, while a wrongly owned one has to be rejected "
             "by a human"],
        ],
        "extraction/extraction_prompts.py::NEGATIVE_GUIDANCE": [
            ["Every rule here is a mistake that actually happened",
             "none of this is guesswork about what an AI might get "
             "wrong; each line was written after a scored benchmark run "
             "caught the error and named the sentence that caused it"],
            ["A date on the calendar is not a deadline on the work",
             "'benefits enrollment closes friday' was becoming "
             "a due date, so announcements are now ruled out by name -- "
             "a date only counts when work is attached to it"],
            ["Read the direction of a sentence, not its keywords",
             "'we are not doing X' and 'X is no longer "
             "blocked' both contain the words of the thing they "
             "deny; reversals are spelled out so they are not read "
             "forwards"],
            ["Hedging lowers confidence, it does not erase the sighting",
             "'i think Dev finished the docs refresh?' was "
             "being dropped entirely; it is now reported with lower "
             "confidence, and the downstream gates decide what it is "
             "worth"],
            ["What the prompt cannot fix moves into code",
             "one misread survived every attempt to phrase it away -- "
             "reading 'the deadline is dropped' as a new "
             "deadline, three times out of three at temperature zero -- "
             "so that pattern is now deleted by a guard in the worker, "
             "with the reason recorded"],
        ],
        "extraction/extraction_worker.py::ExtractionWorker.run_job": [
            ["Send one window to the AI, get typed claims back",
             "one call per window, deterministic ids from content "
             "hashes"],
            ["Run every claim through the guard gauntlet",
             "the AI proposes; code disposes"],
            ["Emit a schema-valid batch no matter what",
             "even a fully garbled AI answer yields a clean empty "
             "batch, not a crash"],
        ],
        "extraction/extraction_worker.py::ExtractionWorker._build_signal": [
            ["Validate one claim field by field",
             "type, title, citations, dates -- each checked against "
             "what is actually in the window"],
            ["Default missing confidence instead of dropping",
             "small AIs often omit it; dropping cost five of five "
             "claims on the first live run"],
            ["Delete deadline-removal language in code",
             "the AI misread 'deadline is dropped' three of three "
             "times at temperature zero, so code took over"],
        ],
        "extraction/extraction_worker.py::ExtractionWorker._resolve_owners": [
            ["Match spoken names to known people",
             "Alexander, Alex, and AM must all land on the same "
             "person"],
            ["Drop unknowns, demote shaky claims",
             "ownership drives approvals downstream, so it fails "
             "conservative"],
        ],
        "extraction/signal_store_pg.py::write_batch": [
            ["Insert the batch under content-hash identity",
             "the same window re-processed inserts zero rows -- ever"],
            ["Pool each claim to the people it concerns",
             "owner candidates first, window participants as "
             "fallback"],
        ],
        "reconciler/board_io_pg.py::load_signals": [
            ["Read each person's waiting claims",
             "one view answers 'what awaits' -- the latest ledger "
             "entry per claim wins"],
        ],
        "reconciler/board_io_pg.py::load_board": [
            ["Load the matchable board with its aliases and ages",
             "task age matters: a seconds-old task held its own 'done' "
             "echo in live testing, exactly as designed"],
        ],
        "reconciler/reconciler.py::Reconciler._match": [
            ["Compare each claim to every open task",
             "text similarity places the claim on a confidence scale"],
            ["Route by band: link, judge, or new",
             "certainty links, ambiguity gets judged, novelty creates"],
            ["Force the judge for anything dangerous",
             "completions and cross-person claims never auto-link "
             "regardless of score"],
        ],
        "policy/judge_prompts.py::build_signal_judge_messages": [
            ["Show the judge the claim and its top candidates",
             "a narrow question gets a reliable answer from a small "
             "model"],
            ["Demand a deciding factor in the verdict",
             "the stated reason is what a human audits to decide "
             "whether to trust local judging at all"],
        ],
        "policy/judge_prompts.py::parse_judge_response": [
            ["Parse the verdict strictly",
             "anything malformed becomes 'no match'"],
            ["Make confusion mean delay",
             "a held claim waits for a better day; a wrong merge would "
             "corrupt the board"],
        ],
        "reconciler/reconciler.py::Reconciler._gate_closure": [
            ["Ask who is claiming the completion",
             "the owner saying 'done' and a bystander saying 'I think "
             "it's done' are different evidence"],
            ["Demand a second independent source for hearsay",
             "two mentions in one thread is one source; the check is "
             "on distinct origins"],
            ["Hold whatever fails",
             "held claims stay pooled -- a fix added after the live "
             "loop nearly swallowed a real completion"],
        ],
        "reconciler/reconciler.py::Reconciler._gate_assignment": [
            ["Walk the attribution ladder",
             "explicit assignment reassigns; self-claims on owned work "
             "raise a conflict; inferred never proposes"],
            ["Prefer a flagged conflict over a silent guess",
             "competing owners is information for the human, not a "
             "coin flip for the code"],
        ],
        "reconciler/reconciler.py::Reconciler._aggregate": [
            ["Combine same-change proposals",
             "three status hints merge into one proposal with pooled "
             "confidence"],
            ["Stamp identity with the run",
             "unsalted proposal ids once collided across packets and "
             "silently wrote nothing -- the salt is the scar"],
        ],
        "reconciler/board_io_pg.py::supersede_open_packets": [
            ["Retire every open packet for this person",
             "at most one review awaits anyone, ever"],
            ["Return undecided claims to the pool",
             "superseded is not lost; the claims queue for the next "
             "round"],
        ],
        "reconciler/board_io_pg.py::write_packet": [
            ["Do everything in one transaction",
             "supersede, insert, mark claims taken -- all or nothing"],
            ["Write nothing for an empty round",
             "silence is a valid output, not an error"],
        ],
        "reconciler/embedders.py::OllamaEmbedder": [
            ["Turn text into vectors with a local model",
             "similarity math needs numbers; nothing leaves the "
             "machine"],
            ["Wait generously for cold starts",
             "a two-minute timeout once killed a run while another "
             "project held the AI queue; ten minutes fixed it"],
        ],
        "reconciler/embedders.py::TokenEmbedder": [
            ["Fall back to plain word-overlap vectors",
             "no AI required -- the offline mode and the reason tests "
             "can stage exact similarities on purpose"],
        ],
        "review_loop/orchestrator.py::Orchestrator.apply_decision": [
            ["Re-check the target before applying",
             "the board may have moved since the packet was built"],
            ["Apply through the one blessed function",
             "every change is an event with a decision attached"],
            ["Record invalidation instead of forcing",
             "a stale accept is written down as stale -- honesty over "
             "convenience"],
        ],
        "review_loop/orchestrator.py::Orchestrator.expire_sweep": [
            ["Find packets past their deadline",
             "unanswered reviews should not linger as zombies"],
            ["Return their claims to the pool",
             "expiry recycles; it never discards"],
        ],
        "review_loop/review_card.py::build_review_card": [
            ["Render proposals as a Teams card",
             "evidence quotes, rationale, and buttons -- decisions "
             "where the conversation already lives"],
            ["Paginate before the size limit",
             "eight proposals per card keeps every card deliverable"],
        ],
        "review_loop/review_card.py::_assert_size": [
            ["Refuse to build an oversized card",
             "a card that cannot deliver is a review that never "
             "happens; failing at build time is the honest failure"],
        ],
        "cli/review.py::still_valid": [
            ["Check the proposal against the board right now",
             "terminal states, merged-away tasks, and vanished targets "
             "all block an accept"],
        ],
        "cli/review.py::apply_accept": [
            ["Map the accepted change to exactly one board event",
             "the CLI never touches the board tables directly -- no "
             "surface does"],
        ],
        "integration/dashboard/dashboard_api.py::board": [
            ["Serve each person's board read-only",
             "the database session physically cannot write"],
            ["Filter through the visibility choke point",
             "one function decides what any viewer may see; every "
             "query goes through it"],
        ],
        "integration/dashboard/dashboard_api.py::blocked_on": [
            ["Show who waits on whom, owner-only",
             "your dependency map is your business, not your "
             "stakeholders'"],
        ],
        "integration/dashboard/dashboard_api.py::provenance": [
            ["Walk a fact back to its source",
             "board event, decision, proposal, claim, message hash -- "
             "the full receipt"],
            ["Refuse below full-trace clearance",
             "provenance is a grant, not a default"],
        ],
        "integration/dashboard/dashboard_api.py::_redact_event": [
            ["Blank the sensitive fields for progress viewers",
             "they see that things happened, not why or on whose "
             "word -- field-level, not row-level"],
        ],
        "testdata/synthesize.py::build_events": [
            ["Roll a day of workplace events",
             "assignments, updates, blockers, completions across a "
             "cast with name variants"],
            ["Force the hard cases every day",
             "traps, hearsay completions, and vague deadlines are "
             "guaranteed, never left to dice"],
        ],
        "testdata/synthesize.py::render": [
            ["Voice each event in its channel's register",
             "meeting filler, chat typos, email signatures -- the mess "
             "is the test"],
            ["Attach the answer key as it renders",
             "every window ships with its golden labels for scoring"],
        ],
        "testdata/score_extraction.py::score_window": [
            ["Match extracted claims to the answer key",
             "lenient on wording, strict on what feeds decisions: "
             "types, owners, dates"],
            ["Count traps and fabrications separately",
             "the failure kinds that erode trust get their own "
             "ledger lines"],
        ],
        "testdata/score_extraction.py::match_type": [
            ["Accept defensible readings, reject trap readings",
             "completion-read-as-status is forgivable; parked work "
             "revived is not -- the scorer grades meaning"],
        ],
        "cadence/cadence_tick.py::main": [
            ["Run the whole afternoon ritual",
             "ingest, reconcile, count pending, maybe notify"],
            ["Exit cleanly on empty days and down AIs",
             "degraded is not failure; the log is the contract"],
        ],
        "cadence/cadence_tick.py::run_reconcile": [
            ["Use the real AI when reachable, offline math when not",
             "offline mode still links the obvious and holds the "
             "uncertain"],
        ],
        "cadence/cadence_tick.py::ollama_up": [
            ["Probe the AI for three seconds",
             "one cheap check decides the tick's whole posture -- "
             "learned when a busy AI queue looked identical to a dead "
             "one"],
        ],
        "ask/ask.py::answer": [
            ["Work out what kind of question it is",
             "blocked, overdue, waiting, who, a meeting, or a subject -- "
             "and each kind has its own shape of answer"],
            ["Answer from the board, in the board's own words",
             "every sentence is built from a stored row and carries the "
             "task it came from, so the answer cannot say anything the "
             "ledger does not hold"],
            ["Say 'heard, not confirmed' when that is the truth",
             "if the only thing on file is a claim nobody corroborated, "
             "it is reported as a claim with a name attached -- never "
             "restated as fact"],
            ["Say nothing when there is nothing",
             "no match and no claim means the honest answer is that the "
             "ledger has not heard about it"],
        ],
        "digest/digest_io_pg.py::load_project": [
            ["Write the stakeholder's weekly note",
             "people who follow a project get what changed and where it "
             "stands, without having to open anything"],
            ["Leave the conversation out by not asking for it",
             "the query fetches titles, states and dates and nothing "
             "else -- so a digest sent to someone outside the team "
             "cannot quote a meeting they were not in, even by mistake"],
            ["Let the grant list decide who receives it",
             "recipients come from current access, so a share that "
             "expired or was withdrawn simply stops arriving"],
        ],
        "cadence/cadence_tick.py::notify": [
            ["Pop a notification only when reviews await",
             "zero pending means zero noise; tests stay silent"],
            ["Use whatever the machine actually has",
             "a Mac pops an alert, a locked-down Windows laptop pops its "
             "own toast, and anything else writes the line to the log -- "
             "a notification that cannot be shown never fails the tick"],
        ],
    },
}

# ---------------------------------------------------------- code extraction


def extract_symbol(path, dotted):
    """Return the source of `dotted` ('fn', 'Class', 'Class.method', or a
    module-level CONSTANT) from `path`, or None if absent."""
    with open(path) as f:
        src = f.read()
    tree = ast.parse(src)
    parts = dotted.split(".")
    node = _find(tree, parts)
    if node is None:
        return None
    lines = src.splitlines()[node.lineno - 1:node.end_lineno]
    # normalize indentation
    indent = len(lines[0]) - len(lines[0].lstrip())
    lines = [ln[indent:] if len(ln) >= indent else ln for ln in lines]
    if len(lines) > MAX_LINES:
        kept = lines[:MAX_LINES]
        kept.append("# ... {0} more lines".format(len(lines) - MAX_LINES))
        lines = kept
    return "\n".join(lines)


def _find(scope, parts):
    for node in ast.iter_child_nodes(scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.name == parts[0]:
            if len(parts) == 1:
                return node
            return _find(node, parts[1:])
        # A component may name a constant, not just a callable: the prompt
        # text the pipeline runs on is as much the code as the function
        # that formats it. Constants are leaves -- there is nothing below
        # them to descend into, so they only match the final part.
        if len(parts) == 1 and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for target in targets:
                if isinstance(target, ast.Name) and target.id == parts[0]:
                    return node
    return None


def hydrate(atlas):
    errors = []
    n_components = 0
    walks = dict(NARRATIVE["walkthroughs"])
    for dom in atlas["domains"]:
        nd = NARRATIVE["domains"].get(dom["id"])
        nl1 = NARRATIVE["pipeline"].get(dom["id"])
        if not nd or not nd.get("plain") or not nd.get("steps"):
            errors.append("NARRATIVE DRIFT: no plain-English entry for "
                          "domain `{0}` -- update NARRATIVE"
                          .format(dom["id"]))
        else:
            dom["plain"] = nd["plain"]
            dom["steps"] = nd["steps"]
        if not nl1:
            errors.append("NARRATIVE DRIFT: no pipeline line for domain "
                          "`{0}` -- update NARRATIVE".format(dom["id"]))
        else:
            dom["l1"] = nl1
        for mod in dom["modules"]:
            for comp in mod["components"]:
                rel = comp.get("file", mod["file"])
                comp["file"] = rel
                path = os.path.join(ROOT, rel)
                if not os.path.exists(path):
                    errors.append("MAP DRIFT: file {0} missing "
                                  "(component `{1}`)".format(rel,
                                                             comp["symbol"]))
                    continue
                code = extract_symbol(path, comp["symbol"])
                if code is None:
                    errors.append("MAP DRIFT: `{0}` not found in {1} -- "
                                  "update the generator".format(
                                      comp["symbol"], rel))
                    continue
                comp["code"] = code
                wkey = comp.get("file", mod["file"]) + "::" + comp["symbol"]
                walk = walks.pop(wkey, None)
                if walk is None:
                    errors.append("NARRATIVE DRIFT: no plain-English "
                                  "walkthrough for `{0}` -- update "
                                  "NARRATIVE".format(wkey))
                else:
                    comp["walk"] = walk
                n_components += 1
    for stale in sorted(walks):
        errors.append("NARRATIVE DRIFT: walkthrough for `{0}` points at "
                      "no component -- remove or fix it".format(stale))
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        sys.exit(1)
    return n_components

# ------------------------------------------------------------- html assembly

CSS = """
:root{--bg:#EDF0F3;--panel:#FFFFFF;--ink:#1B2733;--dim:#5B6B7A;
--line:#D8DEE4;--amber:#D9930D;--mono:ui-monospace,'SF Mono',Menlo,
Consolas,monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--mono);
font-size:15.5px;line-height:1.55;padding:0 0 48px}
header{display:flex;align-items:baseline;gap:14px;padding:18px 28px;
border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header .mark{font-weight:800;letter-spacing:-.01em;font-size:17px}
header .hint{color:var(--dim);font-size:12px;flex:1}
header .stamp{color:var(--dim);font-size:12px}
.crumbs{display:flex;justify-content:flex-end;gap:6px;padding:14px 28px 0;
font-size:12px;color:var(--dim)}
.crumbs a{color:var(--ink);text-decoration:none;border-bottom:1px solid
var(--line);cursor:pointer}
.crumbs span.sep{color:var(--line)}
.layer{display:none;padding:10px 28px 0}
.layer.on{display:block;animation:fade .16s ease}
@keyframes fade{from{opacity:0}to{opacity:1}}
@media (prefers-reduced-motion:reduce){.layer.on{animation:none}}
svg text{font-family:var(--mono)}
.stagebox{cursor:pointer}
.stagebox rect{transition:filter .16s}
.stagebox:hover rect{filter:brightness(.96)}
.rail{cursor:pointer}
.rail:hover rect{filter:brightness(.96)}
.cards{display:flex;gap:18px;flex-wrap:wrap;margin-top:6px}
.card{background:var(--panel);border:1px solid var(--line);
border-top:3px solid var(--dc,#888);padding:14px 16px;min-width:220px;
cursor:pointer;flex:1 1 220px;max-width:340px}
.card:hover{filter:brightness(.98)}
.card h3{font-size:14px;margin-bottom:4px}
.card .meta{color:var(--dim);font-size:12px}
.domhead{margin:8px 0 4px}
.domhead h2{font-size:16px}
.domhead p{color:var(--dim);max-width:76ch;font-size:13px;margin-top:4px}
.explorer{display:flex;gap:18px;align-items:flex-start;margin-top:10px}
.chips{display:flex;flex-direction:column;gap:8px;min-width:250px}
.chip{background:var(--panel);border:1px solid var(--line);
border-left:4px solid var(--dc,#888);padding:9px 12px;cursor:pointer;
font-size:13px}
.chip.active{background:var(--ink);color:#fff;border-color:var(--ink);
border-left-color:var(--dc,#888)}
.codebox{background:var(--panel);border:1px solid var(--line);flex:1;
min-width:0}
.codebox .head{display:flex;gap:10px;align-items:center;padding:10px 14px;
border-bottom:1px solid var(--line);font-size:12px;color:var(--dim)}
.codebox .head .tick{width:8px;height:8px;background:var(--amber);
border-radius:50%;flex:none}
.codebox .head b{color:var(--ink)}
.codebox .blurb{padding:12px 14px;border-bottom:1px solid var(--line);
font-size:13px;color:var(--ink);max-width:90ch}
.codebox pre{padding:14px;overflow-x:auto;font-size:12.5px;line-height:1.55}
.plainwrap{background:var(--panel);border:1px solid var(--line);
border-radius:6px;box-shadow:0 1px 2px rgba(23,34,44,.06),
0 4px 14px rgba(23,34,44,.05);padding:16px 18px;margin:18px 0 6px;
max-width:1100px}
.plainhead{font-size:12px;letter-spacing:.08em;color:var(--dim);
font-weight:700;margin-bottom:10px}
.step{display:flex;gap:10px;margin:8px 0;align-items:baseline}
.stepchip{flex:none;width:22px;height:22px;border-radius:6px;
background:var(--sc,#888);color:#fff;font-weight:700;font-size:12.5px;
display:flex;align-items:center;justify-content:center;
box-shadow:inset 0 1px 0 rgba(255,255,255,.35),0 1px 2px rgba(0,0,0,.18);
transform:translateY(3px)}
.stepact{font-weight:700}
.stepwhy{color:var(--dim)}
.card,.codebox{border-radius:6px;
box-shadow:0 1px 2px rgba(23,34,44,.05),0 5px 16px rgba(23,34,44,.06)}
.card{transition:transform .16s,box-shadow .16s}
.card:hover{transform:translateY(-2px);
box-shadow:0 3px 6px rgba(23,34,44,.08),0 10px 24px rgba(23,34,44,.09)}
.card:active{transform:translateY(0)}
.chip{border-radius:5px;
box-shadow:inset 0 1px 0 rgba(255,255,255,.7),0 1px 2px rgba(23,34,44,.07);
transition:transform .16s}
.chip:hover{transform:translateY(-1px)}
.chip:active{transform:translateY(0);
box-shadow:inset 0 1px 3px rgba(0,0,0,.12)}
.chip.active{box-shadow:inset 0 1px 3px rgba(0,0,0,.28)}
@media (prefers-reduced-motion:reduce){.card,.chip{transition:none}}
.tok-c{color:#2F7D32;font-style:italic}
.tok-s{color:#9C4A38}
.tok-k{color:#3E5C76;font-weight:700}
footer{color:var(--dim);font-size:12px;padding:26px 28px 0;
border-top:1px solid var(--line);margin:34px 28px 0;padding-left:0;
padding-right:0}
"""

JS = r"""
var DATA = JSON.parse(document.getElementById('atlas-data').textContent);
var state = {domain:null, module:null};
function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;
if(txt!=null)e.textContent=txt;return e;}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')
.replace(/>/g,'&gt;');}
function highlight(code){
  var out='',i=0,src=code;
  var re=/(#[^\n]*)|('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")/g,m;
  var parts=[];var last=0;
  while((m=re.exec(src))!==null){
    parts.push({t:'p',v:src.slice(last,m.index)});
    parts.push({t:m[1]?'c':'s',v:m[0]});last=re.lastIndex;}
  parts.push({t:'p',v:src.slice(last)});
  var kw=/\b(def|class|return|if|elif|else|for|while|try|except|finally|with|import|from|as|raise|not|and|or|in|is|None|True|False|lambda|yield|assert|continue|break|pass|global|nonlocal)\b/g;
  return parts.map(function(p){
    var e=esc(p.v);
    if(p.t==='c')return '<span class="tok-c">'+e+'</span>';
    if(p.t==='s')return '<span class="tok-s">'+e+'</span>';
    return e.replace(kw,'<span class="tok-k">$1</span>');
  }).join('');
}
function stepRow(parent,color,num,act,why){
  var row=el('div','step');
  var chip=el('span','stepchip',String(num));
  chip.style.setProperty('--sc',color);
  row.appendChild(chip);
  var body=el('span',null,'');
  var a=el('span','stepact',act);
  var w=el('span','stepwhy',' \u2014 '+why);
  body.appendChild(a);body.appendChild(w);row.appendChild(body);
  parent.appendChild(row);
}
function narHub(box){
  var wrap=el('div','plainwrap');
  wrap.appendChild(el('div','plainhead','THE PIPELINE IN PLAIN ENGLISH'));
  var stages=DATA.domains.filter(function(d){return d.kind==='stage';});
  var rails=DATA.domains.filter(function(d){return d.kind==='rail';});
  var n=0,i,d;
  for(i=0;i<stages.length;i++){d=stages[i];n++;
    stepRow(wrap,d.color,n,d.l1[0],d.l1[1]);}
  for(i=0;i<rails.length;i++){d=rails[i];n++;
    stepRow(wrap,d.color,n,d.l1[0]+' (runs alongside every step)',
            d.l1[1]);}
  box.appendChild(wrap);
}
function narDomain(box,dom){
  var wrap=el('div','plainwrap');
  wrap.appendChild(el('div','plainhead',
    'WHAT HAPPENS HERE, STEP BY STEP'));
  wrap.appendChild(el('div','stepwhy',dom.plain));
  for(var i=0;i<dom.steps.length;i++){
    stepRow(wrap,dom.color,i+1,dom.steps[i][0],dom.steps[i][1]);}
  box.appendChild(wrap);
}
function crumbs(){
  var c=document.getElementById('crumbs');c.innerHTML='';
  var a=el('a',null,'atlas');a.onclick=showHub;c.appendChild(a);
  if(state.domain){c.appendChild(el('span','sep','/'));
    var d=el('a',null,state.domain.name.toLowerCase());
    d.onclick=function(){showDomain(state.domain);};c.appendChild(d);}
  if(state.module){c.appendChild(el('span','sep','/'));
    c.appendChild(el('span',null,state.module.name.toLowerCase()));}
}
function setLayer(id){
  ['hub','domain','module'].forEach(function(l){
    document.getElementById('layer-'+l).classList.toggle('on',l===id);});
  crumbs();
}
function showHub(){state.domain=null;state.module=null;setLayer('hub');}
function showDomain(dom){state.domain=dom;state.module=null;
  var box=document.getElementById('layer-domain');box.innerHTML='';
  var head=el('div','domhead');var h=el('h2',null,'');
  var sw=el('span',null,(dom.kind==='stage'?dom.stage+' \u00b7 ':'')+dom.name);
  sw.style.borderBottom='3px solid '+dom.color;h.appendChild(sw);
  head.appendChild(h);head.appendChild(el('p',null,dom.blurb));
  box.appendChild(head);
  narDomain(box,dom);
  var W=Math.min(document.body.clientWidth-56,1200);
  var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width',W);svg.setAttribute('height',46);
  var n=dom.modules.length;
  for(var i=0;i<n;i++){
    var x0=W/2, x1=(W/(n+1))*(i+1);
    var p=document.createElementNS('http://www.w3.org/2000/svg','path');
    p.setAttribute('d','M'+x0+',2 C'+x0+',30 '+x1+',12 '+x1+',44');
    p.setAttribute('fill','none');p.setAttribute('stroke',dom.color);
    p.setAttribute('stroke-opacity','0.45');p.setAttribute('stroke-width','1.5');
    svg.appendChild(p);}
  box.appendChild(svg);
  var cards=el('div','cards');
  dom.modules.forEach(function(mod){
    var c=el('div','card');c.style.setProperty('--dc',dom.color);
    c.appendChild(el('h3',null,mod.name));
    c.appendChild(el('div','meta',mod.components.length+
      (mod.components.length===1?' component':' components')));
    c.appendChild(el('div','meta',mod.file));
    c.onclick=function(){showModule(dom,mod);};cards.appendChild(c);});
  box.appendChild(cards);setLayer('domain');}
function showModule(dom,mod){state.domain=dom;state.module=mod;
  var box=document.getElementById('layer-module');box.innerHTML='';
  var head=el('div','domhead');
  var h=el('h2',null,dom.name+' \u00b7 '+mod.name);head.appendChild(h);
  box.appendChild(head);
  var ex=el('div','explorer');var chips=el('div','chips');
  var codebox=el('div','codebox');codebox.id='codebox';
  ex.appendChild(chips);ex.appendChild(codebox);box.appendChild(ex);
  mod.components.forEach(function(comp,i){
    var ch=el('div','chip',comp.symbol);
    ch.style.setProperty('--dc',dom.color);
    ch.onclick=function(){
      chips.querySelectorAll('.chip').forEach(function(x){
        x.classList.remove('active');});
      ch.classList.add('active');loadCode(dom,mod,comp);};
    chips.appendChild(ch);
    if(i===0){ch.classList.add('active');loadCode(dom,mod,comp);}});
  setLayer('module');}
function loadCode(dom,mod,comp){
  var cb=document.getElementById('codebox');cb.innerHTML='';
  var head=el('div','head');head.appendChild(el('span','tick',''));
  var path=el('span',null,comp.file+' \u00b7 ');
  var sym=el('b',null,comp.symbol);
  head.appendChild(path);head.appendChild(sym);cb.appendChild(head);
  cb.appendChild(el('div','blurb',comp.blurb));
  if(comp.walk){
    var pw=el('div','plainwrap');pw.style.margin='10px 14px';
    pw.appendChild(el('div','plainhead','PLAIN ENGLISH WALKTHROUGH'));
    for(var wi=0;wi<comp.walk.length;wi++){
      stepRow(pw,dom.color,wi+1,comp.walk[wi][0],comp.walk[wi][1]);}
    cb.appendChild(pw);
  }
  var pre=el('pre');pre.innerHTML=highlight(comp.code);cb.appendChild(pre);}
function buildHub(){
  var box=document.getElementById('layer-hub');
  var stages=DATA.domains.filter(function(d){return d.kind==='stage';});
  var rails=DATA.domains.filter(function(d){return d.kind==='rail';});
  var W=Math.min(document.body.clientWidth-56,1240);
  var IN=150, OUT=170, gap=18;
  var bw=(W-IN-OUT-gap*(stages.length-1))/stages.length;
  var H=250+rails.length*64;
  var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width',W);svg.setAttribute('height',H);
  var midY=118;
  function T(x,y,txt,fill,size,anchor,style){
    var t=document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x',x);t.setAttribute('y',y);
    t.setAttribute('fill',fill||'#1B2733');
    t.setAttribute('font-size',size||13);
    if(anchor)t.setAttribute('text-anchor',anchor);
    if(style==='i')t.setAttribute('font-style','italic');
    if(style==='b')t.setAttribute('font-weight','700');
    t.textContent=txt;svg.appendChild(t);return t;}
  T(4,midY+4,DATA.input,'#5B6B7A',13,null,'i');
  T(W-4,midY+4,DATA.output,'#5B6B7A',13,'end','i');
  stages.forEach(function(dom,i){
    var x=IN+i*(bw+gap);
    if(i>0){var a=document.createElementNS('http://www.w3.org/2000/svg','path');
      a.setAttribute('d','M'+(x-gap+2)+','+midY+' L'+(x-4)+','+midY);
      a.setAttribute('stroke','#5B6B7A');a.setAttribute('stroke-width','1.5');
      a.setAttribute('marker-end','url(#arr)');svg.appendChild(a);}
    var g=document.createElementNS('http://www.w3.org/2000/svg','g');
    g.setAttribute('class','stagebox');
    var r=document.createElementNS('http://www.w3.org/2000/svg','rect');
    r.setAttribute('x',x);r.setAttribute('y',midY-30);
    r.setAttribute('width',bw);r.setAttribute('height',60);
    r.setAttribute('fill','#FFFFFF');r.setAttribute('stroke',dom.color);
    r.setAttribute('stroke-width','1.5');g.appendChild(r);
    svg.appendChild(g);
    var tn=T(x+10,midY-11,dom.stage,dom.color,12,null,'b');
    var name=T(x+bw/2,midY+9,dom.name,'#1B2733',14,'middle','b');
    g.appendChild(r);
    g.addEventListener('click',function(){showDomain(dom);});
    tn.style.pointerEvents='none';name.style.pointerEvents='none';
    var above=(i%2===0);
    var noteY=above?midY-52:midY+52;
    dom.note.split('\n').forEach(function(line,li){
      T(x+bw/2,noteY+(above?-14:14)*(dom.note.split('\n').length-1-li)*(above?1:-1)+(above?0:0)+(li*(above?0:0)),
        '', '#5B6B7A');});
    // simpler: draw each line stacked
    var lines=dom.note.split('\n');
    lines.forEach(function(line,li){
      var y=above?(midY-64+li*15):(midY+52+li*15);
      T(x+bw/2,y,line,'#5B6B7A',11,'middle');});
    var tie=document.createElementNS('http://www.w3.org/2000/svg','line');
    tie.setAttribute('x1',x+bw/2);tie.setAttribute('x2',x+bw/2);
    tie.setAttribute('y1',above?midY-48:midY+30);
    tie.setAttribute('y2',above?midY-30:midY+38);
    tie.setAttribute('stroke','#D8DEE4');svg.appendChild(tie);
  });
  var defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
  defs.innerHTML='<marker id="arr" markerWidth="8" markerHeight="8" '+
    'refX="6" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6 Z" '+
    'fill="#5B6B7A"/></marker>';
  svg.appendChild(defs);
  rails.forEach(function(dom,ri){
    var y=196+ri*64;
    stages.forEach(function(s,i){
      var x=IN+i*(bw+gap)+bw/2;
      var tie=document.createElementNS('http://www.w3.org/2000/svg','line');
      tie.setAttribute('x1',x);tie.setAttribute('x2',x);
      tie.setAttribute('y1',midY+30+(ri===0?8:0));tie.setAttribute('y2',y);
      tie.setAttribute('stroke',dom.color);
      tie.setAttribute('stroke-opacity','0.18');svg.appendChild(tie);});
    var g=document.createElementNS('http://www.w3.org/2000/svg','g');
    g.setAttribute('class','rail');
    var r=document.createElementNS('http://www.w3.org/2000/svg','rect');
    r.setAttribute('x',IN);r.setAttribute('y',y);
    r.setAttribute('width',W-IN-OUT);r.setAttribute('height',44);
    r.setAttribute('fill','#FFFFFF');r.setAttribute('stroke',dom.color);
    r.setAttribute('stroke-width','1.5');g.appendChild(r);svg.appendChild(g);
    var nm=T(IN+12,y+19,dom.name,dom.color,13,null,'b');
    var nt=T(IN+12,y+35,dom.note,'#5B6B7A',11);
    nm.style.pointerEvents='none';nt.style.pointerEvents='none';
    g.addEventListener('click',function(){showDomain(dom);});
  });
  box.appendChild(svg);
  narHub(box);
}
buildHub();showHub();
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Task Ledger \u00b7 system atlas</title>
<style>{css}</style></head>
<body>
<header>
  <span class="mark">task-ledger</span>
  <span class="hint">system atlas \u00b7 click nodes to descend</span>
  <span class="stamp">{stamp}</span>
</header>
<div class="crumbs" id="crumbs"></div>
<div class="layer" id="layer-hub"></div>
<div class="layer" id="layer-domain"></div>
<div class="layer" id="layer-module"></div>
<footer>{ndom} domains \u00b7 {nmod} modules \u00b7 {ncomp} components,
extracted live from source \u00b7 after any code change, rebuild with:
<b>python3 scripts/build_system_map.py</b> \u2014 the build fails loudly if
the map has drifted from the code.</footer>
<script type="application/json" id="atlas-data">{data}</script>
<script>{js}</script>
</body></html>
"""


def build():
    ncomp = hydrate(ATLAS)
    nmod = sum(len(d["modules"]) for d in ATLAS["domains"])

    # version stamp from the project's own versioned artifact
    stamp = "extract-?"
    with open(os.path.join(ROOT, "extraction",
                           "extraction_worker.py")) as f:
        m = re.search(r'PROMPT_VERSION = "([^"]+)"', f.read())
        if m:
            stamp = m.group(1)
    stamp = "atlas v2 \u00b7 " + stamp + " \u00b7 built {0}".format(
        datetime.date.today().isoformat())

    data = json.dumps(ATLAS).replace("</", "<\\/")
    html = TEMPLATE.format(css=CSS, js=JS, data=data, stamp=stamp,
                           ndom=len(ATLAS["domains"]), nmod=nmod,
                           ncomp=ncomp)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write(html)
    print("wrote {0} ({1:.0f} KB): {2} domains, {3} modules, "
          "{4} components".format(OUT, len(html) / 1024,
                                  len(ATLAS["domains"]), nmod, ncomp))

    # ------------------------------------------------- structural self-check
    with open(OUT) as f:
        page = f.read()
    m = re.search(r'<script type="application/json" id="atlas-data">(.*?)'
                  r'</script>', page, re.S)
    assert m, "self-check: embedded JSON block missing"
    parsed = json.loads(m.group(1).replace("<\\/", "</"))
    n = 0
    l1 = l2 = l3 = 0
    for dom in parsed["domains"]:
        assert dom.get("plain") and dom.get("steps") and dom.get("l1"), \
            "self-check: domain {0} missing narrative".format(dom["id"])
        l1 += 1
        l2 += len(dom["steps"])
        for mod in dom["modules"]:
            for comp in mod["components"]:
                assert comp.get("code"), \
                    "self-check: {0} carries no code".format(comp["symbol"])
                assert comp.get("walk"), \
                    "self-check: {0} carries no walkthrough".format(
                        comp["symbol"])
                l3 += len(comp["walk"])
                n += 1
    for marker in ("function showModule", "function showDomain",
                   "function buildHub", "function loadCode",
                   "codebox.id='codebox'", "prefers-reduced-motion",
                   "PLAIN ENGLISH WALKTHROUGH", "THE PIPELINE IN PLAIN",
                   "atlas v2", "function narHub", "function narDomain"):
        assert marker in page, "self-check: missing " + marker
    print("self-check passed: {0} components all carry live code + "
          "walkthroughs; markers present".format(n))
    print("narrative totals: L1 pipeline lines {0}, L2 domain steps {1}, "
          "L3 walkthrough steps {2}".format(l1, l2, l3))


if __name__ == "__main__":
    build()
