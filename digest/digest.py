"""Digest builder: a week of the ledger, in a form somebody will read.

The review card asks a person for a decision. A digest asks nothing -- it
is the weekly account of what the ledger did, and it exists because a tool
you have to remember to open is a tool that gets forgotten. Everything in
it is already in the database; the work here is choosing what deserves
somebody's attention and saying it in a sentence.

Two kinds, with different rules:

  PERSONAL   for one person, about their own work. Self-only, the same bar
             as the pending/held/calibration endpoints: it carries
             unreviewed claims and their own decisions, which no grant
             opens up.
  PROJECT    for the people a project has been shared with. Redacted at
             progress level BY CONSTRUCTION -- the builder never receives
             rationales, deciders, confidence, or provenance for a project
             digest, so there is no field to forget to strip. A stakeholder
             digest can therefore say what changed, and cannot say what was
             said in a meeting they were not in.

Input is plain dicts (see build_personal / build_project) so the core is
storage-agnostic and testable without a database; digest_io_pg.py adapts
Postgres. Renderers: text (mail, terminal, log), HTML (a readable page),
and an Adaptive Card payload the bot host can deliver into Teams the same
way it delivers review cards.

Python 3.8 compatible. Stdlib only. No emojis.
"""

import datetime
import json

MAX_LINES_PER_SECTION = 6
MAX_CARD_BYTES = 28 * 1024      # the Teams limit review_card.py also obeys


def _plural(n, word, plural=None):
    return "{0} {1}".format(n, word if n == 1 else (plural or word + "s"))


def _days_ago(iso, now):
    if not iso:
        return None
    try:
        when = datetime.datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return (now.date() - when).days


# --------------------------------------------------------------- assembly

def build_personal(user_ref, data, now=None):
    """Assemble one person's weekly digest.

    data keys (all optional, all lists unless noted):
      confirmed   [{title, event_type, decision, occurred_at}]
      pending     {undecided, conflicts, oldest_at}
      held        [{title, signal_type, sources, pooled_at}]
      quiet       [{task_id, title, status, quiet_days}]
      overdue     [{task_id, title, deadline}]
      blocked_on  [{my_title, blocking_title, blocking_owner,
                    blocking_status}]
      blocking    [{blocked_user, blocked_title}]
      meetings    [{source_ref, met_at, claims, landed, waiting}]
      latency     {median_hours, n}
    Returns a dict with a headline, sections, and a `quiet` flag: a digest
    with nothing worth saying should not be sent at all.
    """
    now = now or datetime.datetime.now()
    d = dict(data or {})
    sections = []

    pending = d.get("pending") or {}
    if pending.get("undecided"):
        age = _days_ago(pending.get("oldest_at"), now)
        sections.append({
            "key": "review", "title": "Waiting for you",
            "urgency": 2 if (age or 0) >= 3 else 1,
            "count": pending["undecided"],
            "lines": [_plural(pending["undecided"], "proposal") +
                      " awaiting review" +
                      (", oldest " + _plural(age, "day") + " old"
                       if age else "") +
                      (" · " + _plural(pending["conflicts"], "flagged item")
                       if pending.get("conflicts") else "")],
            "action": "python3 cli/review.py --as " + user_ref})

    confirmed = d.get("confirmed") or []
    if confirmed:
        sections.append({
            "key": "confirmed", "title": "You confirmed",
            "urgency": 0,
            "lines": [_verb(c) + " " + c["title"] for c in
                      confirmed[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(confirmed) - MAX_LINES_PER_SECTION)})

    overdue = d.get("overdue") or []
    if overdue:
        sections.append({
            "key": "overdue", "title": "Past their date",
            "urgency": 2,
            "lines": ["{0} — due {1}".format(t["title"], t["deadline"])
                      for t in overdue[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(overdue) - MAX_LINES_PER_SECTION)})

    blocked = d.get("blocked_on") or []
    if blocked:
        sections.append({
            "key": "blocked", "title": "You are waiting on",
            "urgency": 2,
            "lines": ["{0} — waiting on {1}{2}".format(
                b["my_title"], b.get("blocking_title") or "something",
                " (" + b["blocking_owner"] + ", " +
                (b.get("blocking_status") or "unknown") + ")"
                if b.get("blocking_owner") else "")
                for b in blocked[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(blocked) - MAX_LINES_PER_SECTION)})

    blocking = d.get("blocking") or []
    if blocking:
        sections.append({
            "key": "blocking", "title": "Waiting on you",
            "urgency": 2,
            "lines": ["{0} is blocked by your work on {1}".format(
                b["blocked_user"], b["blocked_title"])
                for b in blocking[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(blocking) - MAX_LINES_PER_SECTION)})

    held = d.get("held") or []
    if held:
        sections.append({
            "key": "held", "title": "Heard, not yet confirmed",
            "urgency": 1,
            "lines": ["{0} ({1}, {2})".format(
                h["title"], h["signal_type"].replace("_", " "),
                _plural(h.get("sources") or 0, "source"))
                for h in held[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(held) - MAX_LINES_PER_SECTION),
            "note": "These wait for a second source rather than being "
                    "guessed at."})

    quiet = d.get("quiet") or []
    if quiet:
        sections.append({
            "key": "quiet", "title": "Gone quiet",
            "urgency": 1,
            "lines": ["{0} — no change in {1}".format(
                t["title"], _plural(t["quiet_days"], "day"))
                for t in quiet[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(quiet) - MAX_LINES_PER_SECTION)})

    meetings = d.get("meetings") or []
    if meetings:
        sections.append({
            "key": "meetings", "title": "From this week's conversations",
            "urgency": 0,
            "lines": ["{0}: {1} of {2} became work{3}".format(
                _meeting_name(m["source_ref"]), m.get("landed") or 0,
                m.get("claims") or 0,
                ", " + _plural(m["waiting"], "claim") + " still waiting"
                if m.get("waiting") else "")
                for m in meetings[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(meetings) - MAX_LINES_PER_SECTION)})

    lat = d.get("latency") or {}
    footer = None
    if lat.get("median_hours") is not None and lat.get("n"):
        footer = ("Talk to tracking this week: {0} median, across {1}."
                  .format(_hours(lat["median_hours"]),
                          _plural(lat["n"], "task")))

    return {"kind": "personal", "audience": user_ref,
            "generated_at": now.isoformat(),
            "headline": _headline(sections),
            "sections": sections, "footer": footer,
            "quiet": not sections}


def build_project(project, data, now=None):
    """Assemble a project digest for people the project is shared with.

    data keys: changes [{title, event_type, occurred_at, actor_type}],
    open [{title, status, owner_ref, deadline, overdue}], counts {...}.
    Nothing here carries a rationale, a decider, a confidence or a source:
    the loader does not select them for a project digest, so the redaction
    is structural rather than a filter someone has to remember.
    """
    now = now or datetime.datetime.now()
    d = dict(data or {})
    sections = []
    counts = d.get("counts") or {}

    changes = d.get("changes") or []
    if changes:
        sections.append({
            "key": "changes", "title": "What changed",
            "urgency": 0,
            "lines": [_verb(c) + " " + c["title"] for c in
                      changes[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(changes) - MAX_LINES_PER_SECTION)})

    overdue = [t for t in (d.get("open") or []) if t.get("overdue")]
    if overdue:
        sections.append({
            "key": "overdue", "title": "Past their date",
            "urgency": 2,
            "lines": ["{0} — due {1} ({2})".format(
                t["title"], t["deadline"], t.get("owner_ref") or "unowned")
                for t in overdue[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(overdue) - MAX_LINES_PER_SECTION)})

    blocked = [t for t in (d.get("open") or [])
               if t.get("status") == "blocked"]
    if blocked:
        sections.append({
            "key": "blocked", "title": "Blocked",
            "urgency": 2,
            "lines": ["{0} ({1})".format(t["title"],
                                         t.get("owner_ref") or "unowned")
                      for t in blocked[:MAX_LINES_PER_SECTION]],
            "more": max(0, len(blocked) - MAX_LINES_PER_SECTION)})

    if counts:
        sections.append({
            "key": "state", "title": "Where it stands",
            "urgency": 0,
            "lines": ["{0} in progress · {1} blocked · {2} proposed · "
                      "{3} completed".format(
                          counts.get("in_progress", 0),
                          counts.get("blocked", 0),
                          counts.get("proposed", 0),
                          counts.get("completed", 0))]})

    return {"kind": "project", "audience": project.get("name"),
            "project_id": project.get("project_id"),
            "generated_at": now.isoformat(),
            "headline": _headline(sections),
            "sections": sections,
            "footer": "Shared at progress level: what changed and when. "
                      "The discussion behind it stays with the team.",
            "quiet": not sections}


def _verb(c):
    return {"created": "started", "completed": "finished",
            "cancelled": "dropped", "reopened": "reopened",
            "status_changed": "moved", "deadline_changed": "re-dated",
            "reassigned": "handed over", "blocker_added": "flagged blocked",
            "blocker_resolved": "unblocked"}.get(
                c.get("event_type"), c.get("event_type", "changed"))


def _meeting_name(ref):
    name = (ref or "").replace("copilot-", "")
    return name.replace("_", " ")


def _hours(h):
    if h is None:
        return "unknown"
    h = float(h)
    return ("{0} hours".format(int(round(h))) if h < 48
            else "{0} days".format(round(h / 24.0, 1)))


def _headline(sections):
    """One sentence, chosen by what is most worth saying. Urgency 2 first,
    because a digest that opens with good news while something is overdue
    is a digest people stop trusting."""
    if not sections:
        return "Nothing needs you this week."
    urgent = [s for s in sections if s["urgency"] == 2]
    pick = urgent[0] if urgent else sections[0]
    n = pick.get("count")
    if n is None:                       # one line per item, so count them
        n = len(pick["lines"]) + pick.get("more", 0)
    return "{0}: {1}".format(pick["title"], _plural(n, "item"))


# -------------------------------------------------------------- renderers

def render_text(digest, width=72):
    out = []
    title = ("Task Ledger — week to {0}".format(digest["generated_at"][:10]))
    out.append(title)
    out.append("=" * min(len(title), width))
    out.append(digest["headline"])
    for s in digest["sections"]:
        out.append("")
        out.append(s["title"].upper())
        for line in s["lines"]:
            out.append("  - " + line)
        if s.get("more"):
            out.append("  ... and {0} more".format(s["more"]))
        if s.get("note"):
            out.append("  ({0})".format(s["note"]))
        if s.get("action"):
            out.append("  -> " + s["action"])
    if digest.get("footer"):
        out.append("")
        out.append(digest["footer"])
    return "\n".join(out) + "\n"


def render_html(digest):
    """A self-contained page: no external CSS, so it survives being mailed,
    saved, or opened from a file path."""
    esc = _esc
    parts = ["<!doctype html><meta charset='utf-8'>",
             "<title>Task Ledger digest</title>",
             "<style>body{font:15px/1.5 -apple-system,Segoe UI,system-ui,"
             "sans-serif;color:#1B2733;max-width:640px;margin:32px auto;"
             "padding:0 20px}h1{font-size:19px;margin:0 0 2px}"
             ".sub{color:#5B6B7A;font-size:13px;margin-bottom:20px}"
             "h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;"
             "color:#5B6B7A;margin:22px 0 6px;border-bottom:1px solid #D8DEE4;"
             "padding-bottom:4px}ul{margin:0;padding-left:20px}"
             "li{margin:3px 0}.u2{color:#B3402F}.note,.foot{color:#5B6B7A;"
             "font-size:12.5px;margin-top:6px}.act{font-family:ui-monospace,"
             "Menlo,monospace;font-size:12px;background:#EEF1F4;padding:6px 8px;"
             "border-radius:5px;display:inline-block;margin-top:6px}</style>",
             "<h1>", esc(digest["headline"]), "</h1>",
             "<div class='sub'>", esc(str(digest.get("audience") or "")),
             " · week to ", esc(digest["generated_at"][:10]), "</div>"]
    for s in digest["sections"]:
        parts.append("<h2>" + esc(s["title"]) + "</h2><ul>")
        for line in s["lines"]:
            cls = " class='u2'" if s["urgency"] == 2 else ""
            parts.append("<li" + cls + ">" + esc(line) + "</li>")
        parts.append("</ul>")
        if s.get("more"):
            parts.append("<div class='note'>and {0} more</div>".format(
                s["more"]))
        if s.get("note"):
            parts.append("<div class='note'>" + esc(s["note"]) + "</div>")
        if s.get("action"):
            parts.append("<div class='act'>" + esc(s["action"]) + "</div>")
    if digest.get("footer"):
        parts.append("<div class='foot'>" + esc(digest["footer"]) + "</div>")
    return "".join(parts)


def _esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def build_digest_card(digest):
    """An Adaptive Card the bot host can deliver exactly like a review
    card. Same 28 KB ceiling, enforced here rather than discovered on
    delivery."""
    body = [{"type": "TextBlock", "text": digest["headline"],
             "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "spacing": "None", "isSubtle": True,
             "wrap": True,
             "text": "{0} · week to {1}".format(
                 digest.get("audience") or "", digest["generated_at"][:10])}]
    for s in digest["sections"]:
        body.append({"type": "TextBlock", "text": s["title"].upper(),
                     "weight": "Bolder", "size": "Small", "spacing": "Medium",
                     "color": "Attention" if s["urgency"] == 2 else "Default"})
        for line in s["lines"]:
            body.append({"type": "TextBlock", "text": "- " + line,
                         "wrap": True, "spacing": "None"})
        if s.get("more"):
            body.append({"type": "TextBlock", "spacing": "None",
                         "isSubtle": True,
                         "text": "and {0} more".format(s["more"])})
    if digest.get("footer"):
        body.append({"type": "TextBlock", "text": digest["footer"],
                     "wrap": True, "isSubtle": True, "spacing": "Medium"})
    card = {"type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4", "body": body}
    size = len(json.dumps(card).encode("utf-8"))
    if size > MAX_CARD_BYTES:
        raise ValueError(
            "digest card is {0} bytes, over the {1} limit -- lower "
            "MAX_LINES_PER_SECTION".format(size, MAX_CARD_BYTES))
    return card
