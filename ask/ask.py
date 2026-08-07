"""Ask the ledger: questions answered from confirmed state, with receipts.

The rule that makes this safe to ship in a system whose whole claim is
provenance: **the answer text is assembled from retrieved rows, never
written by a model.** Question understanding is a routing problem -- which
intent, which subject -- and routing is all a model is allowed to do here.
Every sentence in an answer is built from a database row and carries the
task it came from, so there is no path by which this can state something
the ledger does not hold.

Three consequences worth stating plainly:

  IT REFUSES. If the ledger has only an unconfirmed claim about the
  subject -- a pooled signal that never passed a gate -- the answer says
  so and does NOT assert it. "Someone said X, it is not confirmed" is a
  different sentence from "X", and conflating them is exactly how a
  provenance tool becomes a rumour mill.

  IT IS SCOPED. Retrieval runs through visible_tasks(viewer), so an answer
  can never mention work the asker could not already see on the board.
  Held claims are self-only, like everywhere else.

  IT CITES. Every fact carries its task id and last board event; at
  full_trace it also carries the evidence pointers behind that event, so
  "why do you say that" terminates in a hash.

The retriever is injected (see ask_io_pg.Retriever) so the core is
storage-agnostic and testable without a database. An optional embed
callable improves subject matching; without one, matching is lexical.

Python 3.8 compatible. Stdlib only. No emojis.
"""

import re

STOP = set("""a an the is are was were be been being do does did doing of on
in at to for with about from by what whats which who whom whose when where
why how any all my our their his her its this that these those i we you
they it me us them there here still currently now please tell show me
status update on going happening""".split())

# Intent patterns, in priority order. Deliberately boring: a regex that
# misroutes gives a wrong-shaped answer, while a model that misroutes can
# be talked into a wrong-content one.
INTENTS = [
    # Order matters: "waiting for me" is a question about the review queue,
    # while "waiting on the IAM role" is a question about a blocker. The
    # narrower pattern has to be tried first.
    ("review_queue", r"\bwaiting (for|on) (me|you)\b|"
                     r"\b(needs?|awaiting|await)\b.*\breview\b|"
                     r"\breview (queue|list)\b|\bmy queue\b"),
    ("blocked_what", r"\b(what|which|anything)\b.*\bblock(ed|ing)?\b"),
    ("blocked_on", r"\bblock(ed|ing)\b.*\bon\b|\bwaiting (on|for)\b"),
    ("overdue", r"\b(overdue|late|past due|slipped|missed .*(date|deadline))\b"),
    ("due", r"\b(due|deadline|by when|when is .* due)\b"),
    ("waiting", r"\b(unconfirmed|not confirmed|waiting|pending|to review|"
                r"needs? (my )?review)\b"),
    ("who", r"\bwho\b"),
    ("meeting", r"\b(meeting|standup|scrum|call|sync)\b"),
    ("history", r"\b(what happened|history|when did|how did)\b"),
    ("status", r"\b(status|where is|how is|state of|progress on)\b"),
]


def parse_question(q):
    """-> {intent, subject, terms}. Rule-based and deterministic; a model
    may refine the subject later, but never the facts."""
    text = (q or "").strip()
    low = text.lower()
    intent = "search"
    for name, pattern in INTENTS:
        if re.search(pattern, low):
            intent = name
            break
    terms = [t for t in re.findall(r"[a-z0-9][a-z0-9'-]*", low)
             if t not in STOP and len(t) > 2]
    subject = " ".join(terms) if terms else None
    return {"intent": intent, "subject": subject, "terms": terms,
            "question": text}


def _score(terms, *fields):
    """Lexical overlap, the fallback when no embedder is injected."""
    hay = " ".join([f for f in fields if f]).lower()
    if not terms:
        return 0.0
    hits = sum(1 for t in terms if t in hay)
    return hits / float(len(terms))


def rank_tasks(parsed, tasks, embed=None, floor=0.34):
    """Best-matching visible tasks for the question's subject."""
    terms = parsed["terms"]
    scored = []
    if embed and terms:
        import_ok = True
        try:
            vecs = embed([parsed["subject"]] +
                         [t["title"] + " " + " ".join(t.get("aliases") or [])
                          for t in tasks])
        except Exception:
            import_ok = False
        if import_ok:
            qv = vecs[0]
            for t, tv in zip(tasks, vecs[1:]):
                scored.append((_cosine(qv, tv), t))
    if not scored:
        for t in tasks:
            scored.append((_score(terms, t["title"],
                                  " ".join(t.get("aliases") or [])), t))
    scored.sort(key=lambda p: -p[0])
    return [(s, t) for s, t in scored if s >= floor]


def _cosine(u, v):
    dot = sum(a * b for a, b in zip(u, v))
    nu = sum(a * a for a in u) ** 0.5
    nv = sum(b * b for b in v) ** 0.5
    return 0.0 if not nu or not nv else dot / (nu * nv)


# ------------------------------------------------------------------ answer

def answer(question, retriever, embed=None):
    """Answer from the ledger, or decline to.

    retriever supplies rows already scoped to the asker (see
    ask_io_pg.Retriever): .tasks(), .held(), .blocked(), .overdue(),
    .meetings(), .history(task_id), .evidence(task_id).
    """
    parsed = parse_question(question)
    intent = parsed["intent"]
    out = {"question": parsed["question"], "intent": intent,
           "facts": [], "unconfirmed": [], "refusal": None,
           "headline": "", "scope_note":
               "Answered from tasks you can see, and your own pooled claims."}

    if not parsed["terms"] and intent == "search":
        out["refusal"] = ("Ask about a task, a person, a deadline, or a "
                          "meeting -- for example \"what is blocked\" or "
                          "\"status of the intake work\".")
        out["headline"] = "No question found."
        return out

    if intent == "review_queue":
        pend = retriever.pending()
        held = retriever.held()
        out["unconfirmed"] = [_held_item(h) for h in held]
        if pend.get("undecided"):
            out["facts"].append({
                "text": "{0} proposal(s) awaiting your review".format(
                    pend["undecided"]),
                "task_id": None, "status": None,
                "citations": [{"kind": "board",
                               "what": "review packet, oldest " +
                                       (pend.get("oldest_at") or "")[:10]}]})
        out["headline"] = _queue_headline(pend, held)
    elif intent in ("blocked_what", "blocked_on"):
        rows = retriever.blocked()
        out["facts"] = [_blocked_fact(r) for r in rows]
        out["headline"] = ("{0} blocked".format(len(rows)) if rows
                           else "Nothing on your boards is blocked.")
    elif intent == "overdue":
        rows = retriever.overdue()
        out["facts"] = [_overdue_fact(r) for r in rows]
        out["headline"] = ("{0} past its date".format(len(rows)) if rows
                           else "Nothing is past its date.")
    elif intent == "waiting":
        held = retriever.held()
        out["unconfirmed"] = [_held_item(h) for h in held]
        out["headline"] = ("{0} claim(s) heard but not confirmed".format(
            len(held)) if held else "Nothing is waiting.")
        if not held:
            out["facts"] = []
    elif intent == "meeting":
        rows = retriever.meetings(parsed["terms"])
        out["facts"] = [_meeting_fact(m) for m in rows]
        out["headline"] = ("{0} conversation(s)".format(len(rows)) if rows
                           else "No conversation of yours matches that.")
    else:
        # Subject questions: status, who, due, history, or a plain search.
        hits = rank_tasks(parsed, retriever.tasks(), embed=embed)
        if hits:
            for score, task in hits[:3]:
                out["facts"].append(_task_fact(task, retriever, intent))
            out["headline"] = _subject_headline(intent, hits[0][1])
        else:
            # Nothing confirmed. Before saying "no", check whether the
            # ledger heard something and is holding it -- that is a
            # materially different answer and the one people need.
            claims = _matching_claims(parsed, retriever.held())
            if claims:
                out["unconfirmed"] = claims
                out["refusal"] = (
                    "Nothing confirmed on that. The ledger has "
                    "{0} unconfirmed claim(s) about it -- heard, not "
                    "corroborated, and not on any board.".format(len(claims)))
                out["headline"] = "Heard, not confirmed."
            else:
                out["refusal"] = ("The ledger has nothing on that. It only "
                                  "knows what was said in the conversations "
                                  "it processes, and only what a human then "
                                  "confirmed.")
                out["headline"] = "Nothing in the ledger."
    return out


def _task_fact(task, retriever, intent):
    events = retriever.history(task["task_id"])
    last = events[0] if events else None
    bits = ["{0} is {1}".format(task["title"],
                                (task["status"] or "unknown").replace("_", " "))]
    if task.get("owner_ref"):
        bits.append("owned by " + task["owner_ref"])
    if task.get("deadline"):
        bits.append("due " + str(task["deadline"])[:10])
    if task.get("open_blockers"):
        bits.append("{0} open blocker(s)".format(task["open_blockers"]))
    fact = {"text": ", ".join(bits), "task_id": task["task_id"],
            "status": task["status"], "citations": []}
    if last:
        fact["citations"].append({
            "kind": "event", "event_id": last.get("event_id"),
            "what": "{0} on {1}".format(
                (last.get("event_type") or "").replace("_", " "),
                (last.get("occurred_at") or "")[:10]),
            "actor": last.get("actor_type")})
    for ev in retriever.evidence(task["task_id"]):
        fact["citations"].append({
            "kind": "evidence", "resource_id": ev.get("resource_id"),
            "source_type": ev.get("source_type"),
            "content_hash": (ev.get("content_hash") or "")[:12],
            "author_ref": ev.get("author_ref")})
    return fact


def _subject_headline(intent, task):
    if intent == "who":
        return "{0}: {1}".format(task["title"],
                                 task.get("owner_ref") or "no owner")
    if intent == "due":
        return "{0}: {1}".format(
            task["title"],
            "due " + str(task["deadline"])[:10] if task.get("deadline")
            else "no date on the board")
    return "{0} is {1}".format(task["title"],
                               (task["status"] or "unknown").replace("_", " "))


def _queue_headline(pend, held):
    parts = []
    if pend.get("undecided"):
        parts.append("{0} awaiting review".format(pend["undecided"]))
    if held:
        parts.append("{0} heard but unconfirmed".format(len(held)))
    return " · ".join(parts) if parts else "Nothing is waiting for you."


def _blocked_fact(r):
    return {"text": "{0} is blocked{1}{2}".format(
        r["title"],
        " on " + r["blocking_title"] if r.get("blocking_title") else "",
        " ({0})".format(r["blocking_owner"]) if r.get("blocking_owner")
        else ""),
        "task_id": r["task_id"], "status": "blocked",
        "citations": [{"kind": "event", "event_id": r.get("opened_event_id"),
                       "what": "blocked since " +
                               (r.get("opened_at") or "")[:10]}]}


def _overdue_fact(r):
    return {"text": "{0} was due {1} and is {2}".format(
        r["title"], str(r["deadline"])[:10],
        (r["status"] or "").replace("_", " ")),
        "task_id": r["task_id"], "status": r["status"],
        "citations": [{"kind": "board", "what": "deadline on the board"}]}


def _meeting_fact(m):
    return {"text": "{0}: {1} claim(s), {2} became work{3}".format(
        (m["source_ref"] or "").replace("copilot-", "").replace("_", " "),
        m["claims"], m["landed"],
        ", {0} still waiting".format(m["waiting"]) if m.get("waiting")
        else ""),
        "task_id": None, "status": None,
        "citations": [{"kind": "meeting", "what": m["source_ref"],
                       "when": (m.get("met_at") or "")[:10]}]}


def _held_item(h):
    return {"text": h["title"], "signal_type": h.get("signal_type"),
            "author_ref": h.get("author_ref"), "sources": h.get("sources"),
            "signal_id": h.get("signal_id")}


def _matching_claims(parsed, held):
    out = []
    for h in held:
        if _score(parsed["terms"], h.get("title")) >= 0.34:
            out.append(_held_item(h))
    return out


# --------------------------------------------------------------- rendering

def render_text(ans):
    lines = [ans["headline"]]
    if ans.get("refusal"):
        lines.append("")
        lines.append(ans["refusal"])
    for f in ans["facts"]:
        lines.append("")
        lines.append("- " + f["text"])
        for c in f["citations"]:
            lines.append("    " + _cite_text(c))
    if ans["unconfirmed"]:
        lines.append("")
        lines.append("UNCONFIRMED (heard, not on any board):")
        for u in ans["unconfirmed"]:
            lines.append("  - \"{0}\"{1}{2}".format(
                u["text"],
                " said by " + u["author_ref"] if u.get("author_ref") else "",
                " ({0} source(s))".format(u["sources"])
                if u.get("sources") else ""))
    lines.append("")
    lines.append(ans["scope_note"])
    return "\n".join(lines) + "\n"


def _cite_text(c):
    if c["kind"] == "evidence":
        return "source: {0} {1}{2}{3}".format(
            (c.get("source_type") or "").replace("_", " "),
            c.get("resource_id"),
            " · said by " + c["author_ref"] if c.get("author_ref") else "",
            " · hash " + c["content_hash"] if c.get("content_hash") else "")
    if c["kind"] == "meeting":
        return "from: {0} ({1})".format(c.get("what"), c.get("when"))
    return "board: {0}{1}".format(
        c.get("what"),
        " · " + c["actor"].replace("_", " ") if c.get("actor") else "")
