#!/usr/bin/env python3
"""Build docs/presenter_companion.html -- the PRIVATE presenter screen.

Dark single-file HTML, zero dependencies, ES5 only. The audience sees the
dashboard/atlas; this is the teleprompter: verbatim openers, per-domain
scripts, design rationale, scaling answers, and likely-question
accordions. All content lives in P below; the build self-checks
completeness. Rebuild: python3 scripts/build_presenter.py
"""

import json
import os
import re

ROOT = os.path.normpath(os.path.join(os.path.dirname(
    os.path.abspath(__file__)), ".."))
OUT = os.path.join(ROOT, "docs", "presenter_companion.html")

P = {
    "opener": [
        "This is Task Ledger, a personal project I built to answer a "
        "question we all live with: work gets decided in meetings, chat, "
        "and email -- and then somebody has to remember to write it down. "
        "What if the writing-down were automatic, but nothing became "
        "official without a human saying yes?",
        "The pipeline you'll see listens to workplace communication, "
        "extracts task claims with a local AI, matches them against a "
        "task board, and then stops -- every change is proposed to a "
        "person, and only an accepted proposal touches the board. Every "
        "confirmed fact carries a receipt: click it and you're at the "
        "exact message that produced it.",
        "Two numbers before we start: extraction recall is 94 to 97 "
        "percent on synthetic messy-communication benchmarks I built to "
        "stress it, with zero fabrications and zero negation-trap hits -- "
        "and marginal inference cost is zero, because every model runs "
        "locally on this laptop. Nothing you'll see makes a cloud call.",
        "I'll walk the pipeline left to right, then we can go as deep as "
        "you want -- every box on this map drills down to the actual "
        "running code.",
    ],
    "faqs": [
        ["What did this cost to run?",
         "Marginal cost: zero dollars. Extraction and judging run on "
         "mistral-small, a 24-billion-parameter model, on my own "
         "MacBook via Ollama; embeddings likewise. The entire "
         "evaluation program -- hundreds of model calls -- cost "
         "electricity. An enterprise deployment would choose between "
         "self-hosted models (fixed hardware cost, zero per-call) or "
         "Azure OpenAI (order of a fraction of a cent per "
         "conversation window -- verify current pricing)."],
        ["Where does the data live? What about privacy?",
         "Three-layer answer. Transport: the system receives pointers, "
         "never content pushes -- content is fetched on demand under "
         "the app's own credentials. Storage: evidence is stored as "
         "message references plus SHA-256 content hashes -- the ledger "
         "can prove what was said without keeping a copy. Access: the "
         "dashboard is read-only, viewer-scoped, with provenance "
         "gated behind a separate grant. And with local models, "
         "message text never leaves the machine during processing."],
        ["Which AI models, and are we locked in?",
         "mistral-small 3.1 for extraction and judging, nomic-embed "
         "for similarity -- both open-weight, both swappable behind "
         "a one-function interface. The eval harness is the lock-in "
         "insurance: swap the model, rerun two fixed benchmark seeds, "
         "and you know exactly what you gained or lost before "
         "trusting it."],
        ["How long until something like this is useful?",
         "The local loop you're seeing took evenings-and-weekends "
         "scale effort and is fully functional today with a "
         "drop-folder input. The Microsoft-native version is code-"
         "complete and tested offline -- what remains is tenant "
         "registration and one Microsoft approval form. Realistic "
         "pilot timeline is bounded by approvals, not engineering."],
        ["Why should anyone trust an AI touching a task board?",
         "It can't touch it. There is no code path from AI output to "
         "a board mutation -- only accepted human decisions apply, "
         "each recorded with rationale. The AI's failure modes are "
         "measured on adversarial benchmarks, its known blind spots "
         "are patched with deterministic code guards, and anything "
         "uncertain is held rather than guessed. The design principle "
         "throughout: trust proportional to blast radius."],
    ],
    "domains": [
        {"id": "capture", "name": "01 Capture", "color": "#7FA6C9",
         "say": ["The front door. The system never watches a stream -- "
                 "Microsoft sends a notification that a message exists, "
                 "and we group nearby messages into a conversation "
                 "window. One window becomes one AI call; that line is "
                 "the whole cost model.",
                 "Notice what's NOT here: no message content in the "
                 "queue. Pointers in, content fetched on demand. That's "
                 "a privacy posture, but it's also just good "
                 "engineering -- you can't leak what you never stored."],
         "why": "Windowing exists because a Teams thread fires one "
                "notification per message; extracting per-message wastes "
                "calls and loses conversational context. Quiet-edge "
                "detection (60-180s of silence closes a window) with a "
                "30-minute cap bounds both cost and latency.",
         "scaling": "Per-team scale is trivial: a team's daily windows "
                    "number in the dozens. Org scale is a queue-depth "
                    "question -- the batcher is stateless per "
                    "conversation, so it shards naturally. The Graph "
                    "subscription model is Microsoft's own push "
                    "infrastructure; renewal is automated at half-life.",
         "questions": [
             ["Does this read everything everyone says?",
              "It receives existence-notifications for subscribed "
              "resources only, fetches content transiently for "
              "extraction, and stores hashes plus references -- not "
              "text. Subscription scope is an explicit, auditable "
              "permission list a tenant admin consents to."],
             ["What happens when the listener is down?",
              "Notifications missed while down are recovered on "
              "resubscription; locally, inbox files simply wait. The "
              "design bias everywhere is: late beats lost."]],
         "deep": [
             {"name": "WindowBatcher",
              "say": "This little class is the cost model. Every "
                     "message pointer files under its conversation; "
                     "quiet or age flushes the window as one extraction "
                     "job.",
              "why": "One AI call per conversation-burst instead of "
                     "per message -- a 10-20x call reduction on chatty "
                     "threads.",
              "scaling": "Stateless per conversation key; shards by "
                         "team or mailbox with no coordination.",
              "questions": [["Why 60 seconds of quiet?",
                             "Tuned to chat rhythm: long enough that a "
                             "reply-burst stays whole, short enough "
                             "that the afternoon tick sees fresh "
                             "material. It's a config knob, not a "
                             "constant."]]}]},
        {"id": "extract", "name": "02 Extract", "color": "#6FBFAF",
         "say": ["One call per window, and the AI answers in a strict "
                 "format: typed claims -- assignment, status, blocker, "
                 "completion -- each citing the numbered message it came "
                 "from. Then code takes over: every claim is validated "
                 "field by field, and anything uncheckable is deleted. "
                 "Measured across two benchmark corpora: zero "
                 "fabrications survived.",
                 "My favorite part is the guard for a measured blind "
                 "spot: the model read 'the deadline is dropped' as a "
                 "NEW deadline three out of three times at temperature "
                 "zero, through targeted prompt guidance. So that "
                 "pattern dies in code now, with the drop reason "
                 "recorded. When prompting loses, engineering wins."],
         "why": "The AI proposes, code disposes. Salvage keeps one "
                "malformed claim from sinking a batch; citation "
                "verification kills hallucinated evidence; unknown "
                "people are dropped and shaky ownership demoted because "
                "attribution feeds the approval gates downstream.",
         "scaling": "Extraction parallelizes per window. Small local "
                    "models handle it (that's the point); a hosted "
                    "model would raise attribution accuracy a few "
                    "points at per-call cost. Prompt versions are "
                    "stamped into every batch, so A/B across models is "
                    "an eval run, not a leap of faith.",
         "questions": [
             ["What recall/precision, really?",
              "Seed 7 (used during prompt development): 97% recall, "
              "83% attribution. Seed 11, never seen during "
              "development: 94% recall, 75% attribution. Both: 100% "
              "vague-date safety, zero traps, zero fabrications. "
              "Attribution's residual is absorbed by fail-closed "
              "gates downstream."],
             ["What's a negation trap?",
              "'We are NOT doing the docs rewrite this quarter' -- "
              "extracting that as a docs-rewrite task is the trap. "
              "The benchmark plants one guaranteed trap per synthetic "
              "day; current count across both corpora: zero hits."],
             ["Why mistral-small and not GPT-4-class?",
              "The thesis was that careful engineering around a small "
              "local model beats raw model size for structured "
              "extraction -- and the guard-plus-gate architecture is "
              "what makes 24B viable. Also: zero marginal cost made "
              "the whole eval program free."]],
         "deep": [
             {"name": "The guard gauntlet (_build_signal)",
              "say": "Every claim passes through here. Missing "
                     "confidence defaults instead of dropping -- small "
                     "models omit it, and dropping cost five of five "
                     "claims on the first live run. Citations resolve "
                     "to real messages or the claim dies. And the "
                     "deadline-removal guard is three lines of code "
                     "doing what three prompt iterations couldn't.",
              "why": "Deterministic guards are auditable; prompt "
                     "guidance is probabilistic. Blind spots that "
                     "survive prompting graduate to code.",
              "scaling": "Guards are model-independent -- swap the "
                         "model, keep the gauntlet.",
              "questions": [["How do you find new blind spots?",
                             "The benchmark scorer names every miss, "
                             "fabrication, and trap hit with its text. "
                             "That list IS the next prompt version's "
                             "spec -- v1.1 was built exactly that "
                             "way."]]},
             {"name": "The eval loop",
              "say": "Score, read the named failures, patch the "
                     "prompt, bump the version, re-run the same seed "
                     "for the diff, then a fresh seed to check for "
                     "overfitting. v1.0 to v1.1 moved recall 76 to 97 "
                     "on the dev seed and held 94 on the unseen one.",
              "why": "Versioned prompts plus committed score reports "
                     "make 'the extraction got better' a diffable "
                     "claim, not a vibe.",
              "scaling": "Same harness grades any backend -- it's the "
                         "model-swap insurance policy.",
              "questions": [["Could the harness be fooled?",
                             "It was verified honest first: a cheating "
                             "reader of the answer key scores 100%, an "
                             "empty extractor scores 0%. Real scores "
                             "live between proven poles."]]}]},
        {"id": "pool", "name": "03 Pool", "color": "#B0A79F",
         "say": ["Claims don't act -- they wait. Each person's claims "
                 "sit in an append-only pool: pooled, taken, returned. "
                 "Truth at any moment is just the latest entry, and "
                 "history is never rewritten. A completion nobody "
                 "corroborated stays here until corroboration arrives -- "
                 "next week is fine."],
         "why": "Append-only plus content-hash identity gives two "
                "properties for free: re-processing inserts zero rows, "
                "and every past state is reconstructible. No timers, no "
                "state machine -- held work is just unconsumed rows.",
         "scaling": "It's Postgres. The pool view is one indexed "
                    "DISTINCT-ON; per-person partitioning is already "
                    "the access pattern.",
         "questions": [
             ["Doesn't the ledger grow forever?",
              "Events are tiny rows; years of a team's claims is "
              "megabytes. Archival by age is trivial precisely "
              "because history is append-only."],
             ["Has the audit trail actually paid for itself?",
              "Yes -- during the Windows port, a mystery failure was "
              "diagnosed in one read of the pooled-returned-taken "
              "trail. The ledger debugged its own test suite."]],
         "deep": [
             {"name": "Idempotent ingestion (write_batch)",
              "say": "Identity is a hash of content. Run the same "
                     "window twice, insert zero rows -- double-"
                     "ingestion is structurally impossible, not "
                     "procedurally avoided.",
              "why": "Retries, replays, and overlapping subscriptions "
                     "all become safe by construction.",
              "scaling": "Hash identity shards perfectly.",
              "questions": [["What if the same task is discussed in "
                             "two channels?",
                             "Both sightings pool -- deduplication is "
                             "deliberately the matcher's job, with "
                             "human review behind it, not the "
                             "ingester's guess."]]}]},
        {"id": "reconcile", "name": "04 Reconcile", "color": "#8B95C9",
         "say": ["Claims meet the board. Similarity sorts each claim "
                 "into three bands: near-identical auto-links, clearly "
                 "new proposes a creation, and the gray zone goes to an "
                 "AI judge that must name its deciding factor. And one "
                 "rule overrides all scores: completions and cross-"
                 "person claims ALWAYS face the judge -- trust "
                 "proportional to blast radius.",
                 "If the judge answers nonsense? That parses as 'no "
                 "match', which means held. A confused judge causes "
                 "delay, never damage."],
         "why": "Wrongly closing work is the worst error the system "
                "could make, so closure demands owner self-claim or "
                "two independent sources, and brand-new tasks hold "
                "even owner claims. Every gate fails closed into the "
                "pool.",
         "scaling": "Embedding comparison is microseconds; the judge "
                    "is the only per-claim AI cost and only fires in "
                    "the gray band. Thresholds live in one YAML file "
                    "and the calibration plan feeds on accumulated "
                    "reject reasons.",
         "questions": [
             ["How often is the judge wrong?",
              "On live runs it's been observed matching correctly at "
              "0.95 confidence with sensible stated reasons; the "
              "honest answer is that reject-reason accumulation is "
              "the designed measurement, and the fail-closed posture "
              "bounds the cost of any error at 'delayed', not "
              "'wrong'."],
             ["What stops duplicate tasks accumulating?",
              "The new-task band checks against existing aliases; "
              "accepted links annotate alias sets so tasks get easier "
              "to match over time; and a periodic merge scan is on "
              "the roadmap for the residue."]],
         "deep": [
             {"name": "The closure gate",
              "say": "Owner says done, one source suffices. Anyone "
                     "else says done, we need an independent second "
                     "source -- two mentions in one thread is one "
                     "source. Task created seconds ago? Even the "
                     "owner's claim holds. In live testing this gate "
                     "held a task's own creation echo, exactly as "
                     "designed.",
              "why": "Every threshold here was chosen by asking 'what "
                     "does it cost if this is wrong' -- closure is "
                     "where wrong costs most.",
              "scaling": "Corroboration requirements are per-type "
                         "config, tightening or loosening per team "
                         "culture.",
              "questions": [["Isn't that annoyingly conservative?",
                             "Held claims surface at the next "
                             "corroborating mention -- usually the "
                             "next standup. The alternative is a "
                             "board that closes things people didn't "
                             "finish. Conservative is the feature."]]},
             {"name": "Supersede semantics",
              "say": "Every write retires the person's previous open "
                     "packet -- at most ONE review awaits you, ever, "
                     "enforced in the storage layer so every writer "
                     "inherits it. Its first live sweep retired four "
                     "stale packets, including two kinds that were "
                     "invisible to the UI.",
              "why": "Unattended weeks must not rot into a pile of "
                     "contradictory reviews.",
              "scaling": "It's one transaction; concurrency is "
                         "Postgres's problem, already solved.",
              "questions": [["What happens to the superseded "
                             "packet's claims?",
                             "Undecided ones return to the pool and "
                             "ride the next round -- superseded is "
                             "recycled, never lost."]]}]},
        {"id": "review", "name": "05 Review", "color": "#C98B7F",
         "say": ["The human gate, and the reason everything upstream "
                 "is safe to be wrong. There is no code path from AI "
                 "output to a board change -- only an accepted human "
                 "decision applies, and every decision is recorded "
                 "with its rationale, forever.",
                 "One subtlety I'm proud of: accepting re-validates "
                 "against the board as it is NOW, not as it was when "
                 "the packet was built. My first real session hit "
                 "exactly that -- a stale completion was blocked with "
                 "the reason recorded. Optimistic concurrency, "
                 "borrowed straight from database design."],
         "why": "Rejections aren't waste -- reject reasons are the "
                "training data for threshold calibration. The system "
                "improves by being used, and the review is deliberately "
                "a two-minute ritual, not a chore.",
         "scaling": "Teams Adaptive Cards deliver reviews where "
                    "conversation already lives; the local CLI writes "
                    "identical records. Card payloads paginate under "
                    "Teams' 28KB limit by construction -- an oversized "
                    "card fails at build, not delivery.",
         "questions": [
             ["Won't people rubber-stamp?",
              "Maybe -- and a rubber stamp is still a recorded human "
              "decision with a name on it, which is more accountability "
              "than most task boards have. The evidence quote on each "
              "card makes the two-second sanity check genuinely easy."],
             ["What if nobody reviews for a month?",
              "Packets expire, claims return to the pool, supersede "
              "keeps exactly one packet waiting. The system's answer "
              "to neglect is patience, not decay."]],
         "deep": [
             {"name": "apply_decision",
              "say": "Every accepted op maps to exactly one call of "
                     "the one blessed board-mutation function, with "
                     "the decision id attached. The dashboard, the "
                     "CLI, the bot -- none of them touch board tables "
                     "directly. One door, always guarded.",
              "why": "A single mutation path is what makes the audit "
                     "claim checkable rather than aspirational.",
              "scaling": "The function is already concurrency-safe; "
                         "review volume shards per person trivially.",
              "questions": [["Can an admin bypass it?",
                             "An admin with database credentials can "
                             "do anything -- that's true of every "
                             "system. What this design guarantees is "
                             "that the APPLICATION cannot, and that "
                             "bypass would be visible as events "
                             "without decisions."]]}]},
        {"id": "board", "name": "06 Board", "color": "#7FB8C9",
         "say": ["The payoff. A read-only dashboard -- the database "
                 "session physically cannot write -- showing each "
                 "person's confirmed work. And the gold marker is the "
                 "thesis: 'Trace to source' walks any fact back "
                 "through decision, proposal, and claim to the hash "
                 "of the exact message that started it. Facts with "
                 "receipts."],
         "why": "Viewer scoping is field-level, not row-level: "
                "colleagues see that things happened; full-trace "
                "grants see why and on whose word. Provenance is a "
                "grant, not a default -- deliberate information "
                "architecture, not an afterthought.",
         "scaling": "It reads materialized projections, so board "
                    "loads are milliseconds regardless of ledger "
                    "size. Production swaps the viewer header for "
                    "Azure AD claims feeding the same choke-point "
                    "function.",
         "questions": [
             ["Could this integrate with Planner/Jira instead of its "
              "own board?",
              "Architecturally yes -- the ledger is the source of "
              "truth and the board is a projection; projecting into "
              "an external tool is an adapter, with the caveat that "
              "external tools can't render the provenance rail."]],
         "deep": [
             {"name": "The provenance walk",
              "say": "Board event, to decision, to proposal, to "
                     "claims, to evidence hashes. Six joins, one "
                     "endpoint, 403 below full-trace clearance. This "
                     "is the demo's mic-drop click.",
              "why": "If a fact can't show its receipt, it shouldn't "
                     "be on the board -- the walk makes that "
                     "enforceable.",
              "scaling": "Hash-verified evidence means even a "
                         "migrated or archived message store can "
                         "still prove what was said.",
              "questions": [["Why hashes instead of storing the "
                             "message?",
                             "Storing content copies data governance "
                             "problems into a new system. A hash "
                             "proves integrity without custody -- "
                             "the compliance-friendly shape."]]}]},
        {"id": "proving", "name": "R1 Proving Ground", "color": "#B08FB5",
         "say": ["How I know the numbers I quoted. A seeded generator "
                 "fakes messy workdays -- meeting filler, chat typos, "
                 "email signatures -- with guaranteed hard cases: "
                 "negation traps, secondhand completions, vague "
                 "deadlines, name variants. A scorer runs the REAL "
                 "extraction against known answers.",
                 "And before trusting any score, I verified the "
                 "harness itself: a cheating reader scores 100%, an "
                 "empty extractor scores 0%. Real scores live between "
                 "proven poles."],
         "why": "Fixed seeds make prompt changes diffable; a held-out "
                "seed catches overfitting. This is standard ML "
                "discipline applied where most tools ship vibes.",
         "scaling": "The same harness grades any model or prompt -- "
                    "it's how a model swap becomes a measured "
                    "decision. Scenario difficulty tiers are the "
                    "natural next step.",
         "questions": [
             ["Synthetic data isn't real data, though?",
              "Correct, and that's the designed sequence: synthetic "
              "benchmarks bound the failure modes cheaply and "
              "privately BEFORE any real message is processed; live "
              "reject-reasons then calibrate against reality. "
              "Synthetic first is the privacy-respecting order."]],
         "deep": [
             {"name": "The scorer's honesty",
              "say": "Lenient on wording, strict on what feeds "
                     "decisions: types, owners, dates, traps. "
                     "Duplicate sightings deliberately unpunished -- "
                     "dedup is the matcher's job, and a scorer that "
                     "punished it would teach the extractor to hide "
                     "evidence.",
              "why": "Scoring the wrong thing produces confidently "
                     "wrong systems; the scorer grades meaning.",
              "scaling": "Golden-label format matches the general "
                         "eval framework in my synthkit project -- "
                         "campaigns transfer.",
              "questions": [["What did the scorer catch?",
                             "v1.0's failure list -- mention-echoes "
                             "missed, announcement dates extracted "
                             "as deadlines, hedged completions "
                             "suppressed -- each became a named v1.1 "
                             "fix, re-measured on an unseen seed."]]}]},
        {"id": "cadence", "name": "R2 Cadence", "color": "#A6A874",
         "say": ["The system runs itself: one tick every weekday at "
                 "4:45 -- ingest anything new, reconcile, and notify "
                 "me ONLY if proposals await. Silence is a valid "
                 "output. And if the AI is down, the tick degrades "
                 "instead of failing: files wait, matching falls back "
                 "to offline mode, uncertain items hold. Exit code "
                 "zero either way."],
         "why": "Degraded-not-failed plus supersede means unattended "
                "weeks cannot rot -- the two properties that make "
                "'runs itself' true rather than aspirational.",
         "scaling": "launchd here, Task Scheduler on Windows (both "
                    "proven), cron or a container anywhere else -- "
                    "the tick is plain Python with environment "
                    "config.",
         "questions": [
             ["What does a failure actually look like?",
              "A log line and a nonzero exit -- and the strong "
              "distinction that a down AI or an empty day is NOT "
              "failure. The log is the operational contract."]],
         "deep": [
             {"name": "Graceful degradation",
              "say": "A three-second probe decides the tick's "
                     "posture. AI up: full pipeline. AI down: inbox "
                     "waits, offline matching links the obvious and "
                     "holds the rest. Nothing is lost by a bad day; "
                     "everything is late-not-lost.",
              "why": "Learned live: a busy shared AI queue looks "
                     "identical to a dead server; generous timeouts "
                     "plus honest fallbacks beat retries.",
              "scaling": "The same probe pattern gates any backend -- "
                         "local, LAN, or cloud.",
              "questions": [["Why 4:45 PM?",
                             "End-of-day is when a review is "
                             "actionable -- tomorrow's plan is being "
                             "formed. It's one config line; a "
                             "morning tick works identically."]]}]},
    ],
}


CSS = """
:root{--bg:#14181D;--panel:#1C2229;--panel2:#222A33;--ink:#E8ECF0;
--dim:#8A97A3;--line:#2E3843;--mono:ui-monospace,'SF Mono',Menlo,Consolas,
monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--mono);
font-size:15.5px;line-height:1.6;padding:0 0 60px}
header{padding:16px 26px;border-bottom:1px solid var(--line);
background:var(--panel);display:flex;gap:14px;align-items:baseline;
flex-wrap:wrap}
header .mark{font-weight:800;font-size:16px}
header .priv{color:#E0B341;font-size:12px;letter-spacing:.06em;
font-weight:700}
.wrap{max-width:980px;margin:0 auto;padding:18px 26px}
h2{font-size:15px;letter-spacing:.06em;margin:26px 0 10px;color:var(--dim)}
.say{background:var(--panel2);border-left:4px solid var(--dc,#E0B341);
padding:12px 16px;margin:10px 0;border-radius:0 6px 6px 0;
box-shadow:0 2px 8px rgba(0,0,0,.25)}
.say::before{content:'SAY THIS \\2014  VERBATIM';display:block;
font-size:10.5px;letter-spacing:.1em;color:var(--dim);margin-bottom:6px}
.why,.scale{background:var(--panel);border:1px solid var(--line);
padding:12px 16px;margin:10px 0;border-radius:6px;color:var(--ink)}
.why::before{content:'WHY IT IS BUILT THIS WAY';display:block;
font-size:10.5px;letter-spacing:.1em;color:var(--dim);margin-bottom:6px}
.scale::before{content:'SCALING & FUTURE CONFIGURATION';display:block;
font-size:10.5px;letter-spacing:.1em;color:var(--dim);margin-bottom:6px}
.qa{border:1px solid var(--line);border-radius:6px;margin:8px 0;
overflow:hidden}
.qa .q{padding:10px 14px;cursor:pointer;background:var(--panel2);
font-weight:700;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.qa .q:hover{background:#28313C}
.qa .a{display:none;padding:10px 14px;border-top:1px solid var(--line);
color:var(--dim)}
.qa.open .a{display:block}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:14px 0}
.card{flex:1 1 200px;max-width:300px;background:var(--panel);
border:1px solid var(--line);border-top:3px solid var(--dc,#888);
border-radius:6px;padding:12px 14px;cursor:pointer;
box-shadow:0 2px 6px rgba(0,0,0,.3);transition:transform .16s}
.card:hover{transform:translateY(-2px)}
.card:active{transform:translateY(0)}
.deep{border:1px dashed var(--line);border-radius:6px;padding:4px 14px 12px;
margin:16px 0}
.deep h3{font-size:14px;margin:10px 0 2px}
.crumb{color:var(--dim);cursor:pointer;text-decoration:underline;
font-size:13px}
.hidden{display:none}
.stamp{color:var(--dim);font-size:12px;margin-left:auto}
"""

JS = r"""
var P = JSON.parse(document.getElementById('p-data').textContent);
function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;
if(txt!=null)e.textContent=txt;return e;}
function qa(parent,items){
  for(var i=0;i<items.length;i++){
    var box=el('div','qa');
    var q=el('div','q',items[i][0]);
    var a=el('div','a',items[i][1]);
    box.appendChild(q);box.appendChild(a);parent.appendChild(box);
  }
}
document.addEventListener('click',function(ev){
  var n=ev.target;
  while(n && n!==document.body){
    if(n.className==='q'){n.parentNode.className =
      (n.parentNode.className==='qa open')?'qa':'qa open';return;}
    if(n.getAttribute && n.getAttribute('data-dom')!=null){
      showDomain(parseInt(n.getAttribute('data-dom'),10));return;}
    if(n.className==='crumb'){showHub();return;}
    n=n.parentNode;
  }
});
function block(parent,cls,text){
  var b=el('div',cls,text);parent.appendChild(b);return b;
}
function showHub(){
  var hub=document.getElementById('hub');
  var dom=document.getElementById('dom');
  hub.className='wrap';dom.className='wrap hidden';dom.innerHTML='';
}
function showDomain(i){
  var d=P.domains[i];
  var hub=document.getElementById('hub');
  var box=document.getElementById('dom');
  hub.className='wrap hidden';box.className='wrap';box.innerHTML='';
  box.appendChild(el('div','crumb','\u2190 back to hub'));
  var h=el('h2',null,d.name);h.style.color=d.color;box.appendChild(h);
  var j;
  for(j=0;j<d.say.length;j++){
    var s=block(box,'say',d.say[j]);s.style.setProperty('--dc',d.color);}
  block(box,'why',d.why);
  block(box,'scale',d.scaling);
  box.appendChild(el('h2',null,'LIKELY QUESTIONS'));
  qa(box,d.questions);
  for(j=0;j<d.deep.length;j++){
    var dp=d.deep[j];
    var dv=el('div','deep');
    dv.appendChild(el('h3',null,'DEEP DIVE \u00b7 '+dp.name));
    var s2=block(dv,'say',dp.say);s2.style.setProperty('--dc',d.color);
    block(dv,'why',dp.why);
    block(dv,'scale',dp.scaling);
    qa(dv,dp.questions);
    box.appendChild(dv);
  }
  window.scrollTo(0,0);
}
(function(){
  var hub=document.getElementById('hub');
  hub.appendChild(el('h2',null,'OPENER \u2014 60-90 SECONDS, VERBATIM'));
  var i;
  for(i=0;i<P.opener.length;i++){block(hub,'say',P.opener[i]);}
  hub.appendChild(el('h2',null,'GLOBAL FAQS'));
  qa(hub,P.faqs);
  hub.appendChild(el('h2',null,'DOMAINS \u2014 CLICK FOR SCRIPT'));
  var cards=el('div','cards');
  for(i=0;i<P.domains.length;i++){
    var c=el('div','card');
    c.setAttribute('data-dom',String(i));
    c.style.setProperty('--dc',P.domains[i].color);
    c.appendChild(el('div',null,P.domains[i].name));
    cards.appendChild(c);
  }
  hub.appendChild(cards);
})();
"""

TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Task Ledger \u00b7 presenter companion</title>
<style>{css}</style></head>
<body>
<header>
  <span class="mark">task-ledger</span>
  <span class="priv">PRESENTER COMPANION \u00b7 PRIVATE \u2014 for the
presenter's screen only</span>
  <span class="stamp">{stamp}</span>
</header>
<div class="wrap" id="hub"></div>
<div class="wrap hidden" id="dom"></div>
<script type="application/json" id="p-data">{data}</script>
<script>{js}</script>
</body></html>
"""


def build():
    # completeness self-check on P before building
    assert 3 <= len(P["opener"]) <= 5, "opener block count"
    assert 4 <= len(P["faqs"]) <= 6, "faq count"
    assert len(P["domains"]) == 8, "eight domains"
    deep_total = 0
    for d in P["domains"]:
        for key in ("id", "name", "color", "say", "why", "scaling",
                    "questions", "deep"):
            assert d.get(key), "domain {0} missing {1}".format(d["id"], key)
        assert 1 <= len(d["deep"]) <= 4, "deep node count " + d["id"]
        for dp in d["deep"]:
            for key in ("name", "say", "why", "scaling", "questions"):
                assert dp.get(key), "deep {0} missing {1}".format(
                    dp["name"], key)
            deep_total += 1
    import datetime
    stamp = "built {0}".format(datetime.date.today().isoformat())
    data = json.dumps(P).replace("</", "<\\/")
    html = TEMPLATE.format(css=CSS, js=JS, data=data, stamp=stamp)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write(html)
    nq = sum(len(d["questions"]) for d in P["domains"]) + \
        sum(len(dp["questions"]) for d in P["domains"] for dp in d["deep"])
    print("wrote {0} ({1:.0f} KB): {2} domains, {3} deep nodes, "
          "{4} global FAQs, {5} Q&A answers".format(
              OUT, len(html) / 1024, len(P["domains"]), deep_total,
              len(P["faqs"]), nq))
    # structural check of the written file
    page = open(OUT).read()
    for m in ("PRESENTER COMPANION", "SAY THIS", "LIKELY QUESTIONS",
              "SCALING", "p-data"):
        assert m in page, "marker missing: " + m
    js = page.split("<script>")[1].split("</script>")[0]
    assert not re.search(r"=>", js) and "`" not in js \
        and not re.search(r"\b(let|const)\s", js), "ES5 violation"
    print("self-check passed: markers present, embedded JS is ES5-clean")


if __name__ == "__main__":
    build()
