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
                     "verified. Measured: 94-97% recall, 0 fabrications on "
                     "synthetic messy corpora.",
            "modules": [
                {"name": "Prompt (extract-v1.1)",
                 "file": "extraction/extraction_prompts.py",
                 "components": [
                     {"symbol": "build_messages",
                      "blurb": "The versioned contract with the model: "
                               "numbered items, mandatory citations, and "
                               "negative guidance grown from measured "
                               "failures (announcement dates, negation "
                               "reversals, hedge-lowers-confidence)."},
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
                     "empty one 0%. This is how extract-v1.1's numbers "
                     "exist at all.",
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
                {"name": "Degradation",
                 "file": "cadence/cadence_tick.py",
                 "components": [
                     {"symbol": "ollama_up",
                      "blurb": "A 3-second reachability probe decides the "
                               "tick's whole posture -- learned the hard "
                               "way when a shared Ollama queue looked "
                               "identical to a hung server."},
                     {"symbol": "notify",
                      "blurb": "macOS notification only when pending > 0; "
                               "suppressed in tests; logs on other "
                               "platforms. Zero pending, zero noise."},
                 ]},
            ],
        },
    ],
}

# ---------------------------------------------------------- code extraction


def extract_symbol(path, dotted):
    """Return the source of `dotted` ('fn' or 'Class.method' or 'Class')
    from `path`, or None if absent."""
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
    return None


def hydrate(atlas):
    errors = []
    n_components = 0
    for dom in atlas["domains"]:
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
                n_components += 1
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
font-size:14px;line-height:1.5;padding:0 0 48px}
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
    stamp += " \u00b7 built {0}".format(
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
    for dom in parsed["domains"]:
        for mod in dom["modules"]:
            for comp in mod["components"]:
                assert comp.get("code"), \
                    "self-check: {0} carries no code".format(comp["symbol"])
                n += 1
    for marker in ("function showModule", "function showDomain",
                   "function buildHub", "function loadCode",
                   "codebox.id='codebox'", "prefers-reduced-motion"):
        assert marker in page, "self-check: missing " + marker
    print("self-check passed: {0} components all carry live code; "
          "interactive markers present".format(n))


if __name__ == "__main__":
    build()
