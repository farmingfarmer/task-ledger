"""Task Ledger review CLI: the local human loop.

The terminal equivalent of the Teams review card. Lists pending packets,
walks each proposal (rationale, change detail, evidence, conflicts), takes a
decision, and commits it through the SAME paths the bot host uses:

  - every decision is a DecisionRecord (schema constraints enforced: rejects
    carry an enumerated reason, edited payloads only with accept_edited,
    UNIQUE(proposal_id, decided_by) makes double-answers idempotent)
  - accepts re-validate against the CURRENT board first (optimistic
    concurrency): a proposal the board has moved past is recorded with
    invalidated_reason and NOT applied -- mirroring the orchestrator
  - board mutations go exclusively through apply_task_event(); the CLI never
    touches tasks_current
  - creates must name an owner: plain accept refuses an ownerless create
    and edit-accept insists on one, because a task with no owner_ref is
    invisible on every board (a first-live-run casualty)
  - defers return source signals to the pool for re-reconciliation
  - packet lifecycle events recorded: draft -> delivered on open,
    partially_decided on first decision, closed when nothing remains

Usage:
  python3 review.py --as aad-dev --dsn "host=... dbname=taskintel ..."          # review
  python3 review.py --as aad-dev --dsn "..." --list                             # just list
  python3 review.py --as aad-dev --dsn "..." --accept-unconflicted              # bulk

Identity note: --as is trusted local identity, appropriate for a
single-operator machine. The production surface (Teams card) stamps identity
from the authenticated activity; this CLI is its consenting-adult sibling.

Python 3.8 compatible. Requires psycopg2.
"""

import argparse
import datetime
import json
import sys

import psycopg2
import psycopg2.extras

REJECT_REASONS = [
    ("1", "not_a_task", "Not actually a task"),
    ("2", "duplicate", "Duplicate of existing task"),
    ("3", "wrong_task_match", "Matched the wrong task"),
    ("4", "wrong_status", "Wrong status"),
    ("5", "wrong_owner", "Wrong owner"),
    ("6", "bad_date_resolution", "Date resolved wrong"),
    ("7", "stale", "Out of date"),
    ("8", "other", "Other"),
]

OP_EVENT = {
    "update_status": "status_changed",
    "update_deadline": "deadline_changed",
    "reassign": "reassigned",
    "add_blocker": "blocker_added",
    "resolve_blocker": "blocker_resolved",
    "complete_task": "completed",
    "cancel_task": "cancelled",
}


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def dsn_hint():
    """A DSN example in the shell that will actually run it. cmd.exe knows
    %VAR%, not $VAR; the Windows port lives on port 5433 under %USERNAME%
    (deploy/windows/setup_windows.bat). A hint that pastes wrong is worse
    than no hint."""
    if sys.platform == "win32":
        return ('set DB_DSN=host=localhost port=5433 dbname=taskintel '
                'user=%USERNAME%\n'
                '  python review.py --as <user> --dsn "%DB_DSN%"')
    return ('export DB_DSN="host=localhost port=5432 dbname=taskintel '
            'user=$(whoami)"\n'
            '  python3 review.py --as <user> --dsn "$DB_DSN"')


def needs_owner(prop):
    """True for a create the board cannot show anyone.

    apply_task_event files a created event under the accepting user, but
    every board view keys off owner_ref: a create accepted with none lands
    as a task that appears on nobody's board and surfaces in no rollup.
    The first live run produced exactly that, so plain accept refuses it
    and edit-accept insists on an owner."""
    return (prop["op"] == "create_task"
            and not (prop["payload"] or {}).get("owner_ref"))


# ------------------------------------------------------------------ queries

def pending_packets(cur, user):
    cur.execute("""
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT rp.packet_id, rp.generated_at, rp.board_version, l.state,
               count(p.proposal_id) AS proposals,
               count(p.proposal_id) - count(d.decision_id) AS undecided
        FROM review_packets rp
        JOIN latest l USING (packet_id)
        JOIN proposals p USING (packet_id)
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')
        GROUP BY rp.packet_id, rp.generated_at, rp.board_version, l.state
        HAVING count(p.proposal_id) - count(d.decision_id) > 0
        ORDER BY rp.generated_at""", (user,))
    return cur.fetchall()


def undecided_proposals(cur, packet_id):
    cur.execute("""
        SELECT p.*, t.title AS target_title, t.status AS target_status,
               t.merged_into AS target_merged
        FROM proposals p
        LEFT JOIN tasks_current t ON t.task_id = p.target_task_id
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE p.packet_id = %s AND d.decision_id IS NULL
        ORDER BY (p.conflict_type IS NULL), p.confidence DESC""",
        (packet_id,))
    return cur.fetchall()


def evidence_for(cur, proposal_id):
    cur.execute("""
        SELECT DISTINCT ev.source_type, ev.resource_id, ev.content_hash
        FROM proposal_signals ps
        JOIN evidence_refs ev USING (signal_id)
        WHERE ps.proposal_id = %s ORDER BY ev.resource_id""", (proposal_id,))
    return cur.fetchall()


def record_state(cur, packet_id, state):
    cur.execute("INSERT INTO packet_state_events (packet_id, state) "
                "VALUES (%s, %s)", (packet_id, state))


# ------------------------------------------------------------ apply accepts

def still_valid(prop):
    """Optimistic concurrency, mirroring the orchestrator's checks."""
    op = prop["op"]
    if op in ("create_task",):
        return True, None
    if prop["target_task_id"] is None and op != "create_task":
        return False, "no_target"
    if prop["target_merged"]:
        return False, "target_merged_away"
    if prop["target_title"] is None and op != "create_task":
        return False, "target_missing"
    terminal = prop["target_status"] in ("completed", "cancelled")
    if op in ("complete_task", "cancel_task", "update_status",
              "update_deadline", "add_blocker", "resolve_blocker",
              "reassign", "add_subtask") and terminal:
        return False, "target_already_{0}".format(prop["target_status"])
    return True, None


def apply_accept(cur, user, prop, payload):
    """Map an accepted proposal to its board event via apply_task_event.
    Returns the task_id acted on (new id for creates)."""
    op = prop["op"]
    if op in ("create_task", "add_subtask"):
        task_id = "task-" + prop["proposal_id"][:8]
        body = {
            "title": payload["title"],
            "description": payload.get("description"),
            "status": payload.get("status", "proposed"),
            "owner_ref": payload.get("owner_ref"),
            "deadline": payload.get("deadline"),
            "parent_task_id": (payload.get("parent_task_id")
                               if op == "create_task"
                               else prop["target_task_id"]
                               or payload.get("parent_task_id")),
        }
        cur.execute("SELECT apply_task_event(%s,%s,'created','user_decision',"
                    "currval(pg_get_serial_sequence('decisions',"
                    "'decision_id')), %s::jsonb)",
                    (task_id, user, json.dumps(body)))
        return task_id
    if op == "merge_tasks":
        for merged in payload["merged_task_ids"]:
            cur.execute("SELECT apply_task_event(%s,%s,'merged_into',"
                        "'user_decision',currval(pg_get_serial_sequence("
                        "'decisions','decision_id')), %s::jsonb)",
                        (merged, user,
                         json.dumps({"surviving_task_id":
                                     payload["surviving_task_id"]})))
        return payload["surviving_task_id"]
    event = OP_EVENT[op]
    cur.execute("SELECT apply_task_event(%s,%s,%s,'user_decision',"
                "currval(pg_get_serial_sequence('decisions','decision_id')),"
                " %s::jsonb)",
                (prop["target_task_id"], user, event, json.dumps(payload)))
    return prop["target_task_id"]


def defer_signals(cur, user, prop):
    cur.execute("""
        INSERT INTO signal_pool_events (signal_id, user_ref, event, run_id)
        SELECT ps.signal_id, %s, 'returned', 'review-defer'
        FROM proposal_signals ps WHERE ps.proposal_id = %s""",
        (user, prop["proposal_id"]))


# -------------------------------------------------------------- interaction

def render(prop, evidence, idx, total):
    p = prop["payload"]
    lines = []
    lines.append("")
    lines.append("[{0}/{1}] {2}  conf {3:.0%}{4}".format(
        idx, total, prop["op"].upper().replace("_", " "),
        float(prop["confidence"]),
        "   ** CONFLICT **" if prop["conflict_type"] else ""))
    if prop["target_title"]:
        lines.append("  task: {0} ({1}, currently {2})".format(
            prop["target_title"], prop["target_task_id"],
            prop["target_status"]))
    lines.append("  " + prop["rationale"])
    detail = {
        "create_task": lambda: "  new: \"{0}\"  owner={1}  deadline={2}".format(
            p.get("title"), p.get("owner_ref") or "-",
            p.get("deadline") or "-"),
        "add_subtask": lambda: "  new subtask: \"{0}\"".format(p.get("title")),
        "update_status": lambda: "  {0} -> {1}".format(
            p.get("from_status"), p.get("to_status")),
        "update_deadline": lambda: "  deadline -> {0}  (from phrase: \"{1}\")".format(
            p.get("to_deadline"), p.get("raw_text") or "-"),
        "reassign": lambda: "  owner -> {0} ({1})".format(
            p.get("to_owner_ref"), p.get("basis")),
        "add_blocker": lambda: "  blocked on: {0}".format(p.get("description")),
    }.get(prop["op"])
    if detail:
        lines.append(detail())
    if prop["conflict_type"]:
        lines.append("  conflict: {0}".format(prop["conflict_detail"]))
    if p.get("match_note"):
        lines.append("  duplicate check: {0}".format(p["match_note"]))
    for ev in evidence[:3]:
        lines.append("  source: {0} · {1}  hash {2}".format(
            ev["source_type"].replace("_", " "), ev["resource_id"],
            (ev["content_hash"] or "")[:12]))
    return "\n".join(lines)


def known_owners(cur):
    """Everyone this ledger has already seen, for catching a typo'd owner.

    Not a permission check and not a closed list: a create may legitimately
    name somebody new, and refusing that would make the CLI unable to record
    a real decision. It exists because owner_ref took any non-empty string
    at all, so "huh?" typed at the wrong prompt became a permanent owner and
    the task landed on nobody's board -- the same invisible-task failure the
    ownerless-create guard was built to stop, walked in through the front
    door.
    """
    refs = set()
    try:
        cur.execute("""
            SELECT user_ref AS r FROM user_boards
            UNION SELECT owner_ref FROM tasks_current
                   WHERE owner_ref IS NOT NULL
            UNION SELECT user_ref FROM signal_pool_events
            UNION SELECT author_ref FROM signals WHERE author_ref IS NOT NULL
            """)
        for row in cur.fetchall():
            val = (list(row.values())[0] if isinstance(row, dict)
                   else row[0])
            if val:
                refs.add(val)
    except Exception:                                   # noqa: BLE001
        return set()          # validation is a courtesy, never a blocker
    return refs


def prompt_title(current, inp, out, known=None):
    """Enter keeps the title the proposal came with; typing replaces it.

    The replace is the trap. A reviewer told "use [e]dit-accept to name an
    owner" arrives at a prompt and types a person into it, and the task is
    then titled with somebody's name -- observed on the first live review.
    Naming a person here is therefore refused rather than accepted, because
    no title is ever a user_ref and the alternative is silent.
    """
    while True:
        out.write("  title [{0}]: ".format(current))
        out.flush()
        line = inp.readline()
        if not line:
            return None
        t = line.strip()
        if not t:
            return current
        if known and t in known:
            out.write("  '{0}' is a person, not a title -- this prompt "
                      "renames the task.\n"
                      "  the owner is asked for separately; press enter to "
                      "keep \"{1}\".\n".format(t, current))
            continue
        return t[:120]


def _norm_words(s):
    return " ".join((s or "").lower().split())


def teach_wordings(cur, user, task_id, task_title, wordings):
    """An accepted link teaches the task the wording it was accepted under.

    An ask-band proposal exists because the wording and the title diverged
    too far for the auto-link and the judge would not commit. The human
    committing IS the verdict, and it would be absurd to ask them again
    next week about the same phrase -- so the wording becomes an alias,
    the alias set is what the matcher embeds, and the next occurrence
    links at cosine 1.0 with nobody asked and no judge run. Every accept
    makes the vocabulary bigger and the questions rarer; that is the whole
    trade.
    """
    n = 0
    for sid, title in wordings:
        if not title or _norm_words(title) == _norm_words(task_title):
            continue
        cur.execute(
            "SELECT apply_task_event(%s,%s,'alias_added','system',NULL,"
            "%s::jsonb)",
            (task_id, user, json.dumps(
                {"alias": title, "source_signal_id": sid})))
        n += 1
    return n


def seed_candidate_aliases(cur, user, prop, task_id):
    """Give a task born from the register every wording it was heard under.

    The register merges paraphrases while the work is only a candidate --
    "Synth kit project", "Synth kit project capability" and "Add new tests
    to synth kit" corroborate one another at same_candidate_floor, and that
    merging is why the work promoted at all. The moment the create was
    accepted, all of that was thrown away: the task began life knowing only
    its one canonical title, and the very variants that proved the work was
    real sat in the pool failing to match it -- each one needing a judge
    call the model kept declining. The matcher embeds alias sets, so the
    fix is to hand the wordings over: a held signal whose title IS an alias
    matches at cosine 1.0 and auto-links with no judge at all.

    Written through apply_task_event('alias_added'), the same door every
    alias goes through, each carrying the sighting's signal_id -- so even
    an alias traces back to the sentence that produced it. Title joins the
    candidate because the payload cannot carry a candidate_id; the ORIGINAL
    proposal title must be passed here, not an edit-accept rename.
    """
    if prop["op"] != "create_task":
        return 0
    title = (prop["payload"] or {}).get("title")
    if not title:
        return 0
    cur.execute("""
        SELECT DISTINCT ON (e.title) e.title, e.signal_id
        FROM task_candidate_events e
        JOIN v_task_candidates c USING (candidate_id, user_ref)
        WHERE c.user_ref = %s AND c.title = %s
          AND c.state IN ('promoted', 'accepted')
          AND e.event = 'sighted' AND e.title <> c.title
        ORDER BY e.title, e.event_id DESC""", (user, title))
    n = 0
    # run_session's cursor is a RealDictCursor; the review tests use one
    # too, and plain tuples appear when this is called from elsewhere.
    # Unpacking a dict row as a tuple yields its KEY STRINGS -- which the
    # FK on source_signal_id rejected, taking the whole session with it.
    for row in cur.fetchall():
        alias, sid = ((row["title"], row["signal_id"])
                      if isinstance(row, dict) else (row[0], row[1]))
        cur.execute(
            "SELECT apply_task_event(%s,%s,'alias_added','system',NULL,"
            "%s::jsonb)",
            (task_id, user, json.dumps(
                {"alias": alias, "source_signal_id": sid})))
        n += 1
    return n


def dismiss_promoted_candidate(cur, user, prop):
    """A rejected promotion must not be offered again.

    Promotion moves a candidate to 'promoted' and the projection makes that
    stick, so it cannot promote twice on its own. But rejecting the card
    says something stronger than "already asked" -- it says this is not
    work -- and nothing recorded that. The candidate stayed in 'promoted'
    forever, indistinguishable from a card still waiting for an answer, so
    the register could not report how much of what it surfaced was wanted,
    and a reviewer had no way to tell the two apart.

    Title is the join because CreateTaskPayload is additionalProperties:
    false and cannot carry the candidate_id. That is exact for a reject,
    which offers no chance to edit the title; an edit-accept can rename,
    but an edit-accept is an acceptance and never reaches here.

    Returns the number dismissed, which is 0 for every proposal that did
    not come from the register.
    """
    if prop["op"] != "create_task":
        return 0
    title = (prop["payload"] or {}).get("title")
    if not title:
        return 0
    cur.execute("""
        INSERT INTO task_candidate_events
          (candidate_id, user_ref, event, title, run_id)
        SELECT candidate_id, user_ref, 'dismissed', title, 'review-reject'
        FROM v_task_candidates
        WHERE user_ref = %s AND title = %s AND state = 'promoted'""",
        (user, title))
    return cur.rowcount


def prompt_owner(current, inp, out, known=None):
    """Edit-accept of a create ends with an owner. Enter keeps the current
    one when there is one; an empty answer with none re-asks. Returns the
    ref, or None at EOF -- a closed stdin quits rather than loops."""
    while True:
        out.write("  owner ref [{0}]: ".format(current or "required"))
        out.flush()
        line = inp.readline()
        if not line:
            return None
        val = line.strip() or (current or "")
        if not val:
            out.write("  an owner is required: a task with none is invisible "
                      "on every board\n")
            continue
        if known and val not in known:
            near = sorted(k for k in known
                          if val.lower() in k.lower())[:3]
            out.write("  '{0}' is not somebody this ledger has seen.\n"
                      .format(val))
            if near:
                out.write("  did you mean: {0}\n".format(", ".join(near)))
            out.write("  [y] use it anyway  [enter] type it again > ")
            out.flush()
            conf = inp.readline()
            if not conf:
                return None
            if conf.strip().lower() != "y":
                continue
        return val


_KEEP_ASKING = object()   # EOF at a prompt: quit, do not loop forever


def prompt_deadline(current, inp, out):
    """A date the board can actually store, or none at all.

    deadline is a date column and this prompt used to pass whatever was
    typed straight into it. "08-15-2026" survived only because Postgres
    reads ambiguous dates as MDY by default -- "05-06-2026" would have
    silently become May 6 for somebody who meant June 5, and there is no
    way to notice afterwards. A genuinely malformed string was worse: it
    raised inside the transaction, ending the review session rather than
    re-asking one field.

    Enter keeps what is there, '-' clears it, anything else must parse as
    YYYY-MM-DD. A date written the American way is named as such rather
    than rejected blankly, because being told "not a date" about a string
    that is obviously a date teaches nothing.
    """
    while True:
        out.write("  deadline YYYY-MM-DD [{0}]: ".format(current or "-"))
        out.flush()
        line = inp.readline()
        if not line:
            return _KEEP_ASKING
        d = line.strip()
        if not d:
            return current
        if d == "-":
            return None
        # Slashes are accepted and normalised: 2026/08/15 states the same
        # unambiguous order and being strict about the separator serves
        # nobody. Order is the part that matters, and 08/15/2026 still
        # fails below.
        for text in (d, d.replace("/", "-")):
            try:
                datetime.datetime.strptime(text, "%Y-%m-%d")
                return text
            except ValueError:
                continue
        parts = d.replace("/", "-").split("-")
        if len(parts) == 3 and len(parts[-1]) == 4 and parts[0].isdigit():
            # >12 in the first field can only be a day, so the order is
            # unambiguous; otherwise assume the American reading, which is
            # what the ambiguity costs.
            day, month = ((parts[0], parts[1]) if int(parts[0]) > 12
                          else (parts[1], parts[0]))
            out.write("  '{0}' is not YYYY-MM-DD. Did you mean "
                      "{1}-{2}-{3}?\n".format(
                          d, parts[2], month.zfill(2), day.zfill(2)))
        else:
            out.write("  '{0}' is not a date. Use YYYY-MM-DD, '-' for none, "
                      "or enter to keep what is there.\n".format(d))


def prompt_decision(prop, inp, out, known=None):
    editable = prop["op"] in ("create_task", "add_subtask",
                              "update_deadline", "update_status")
    menu = "  [a]ccept  [r]eject  [d]efer  [s]kip" + \
           ("  [e]dit-accept" if editable else "") + "  [q]uit > "
    while True:
        out.write(menu)
        out.flush()
        line = inp.readline()
        if not line:
            return ("q", None, None)
        c = line.strip().lower()
        if c == "a":
            if needs_owner(prop):
                out.write("  this create names no owner, and a task with "
                          "none is invisible on every board.\n"
                          "  use [e]dit-accept to name one.\n")
                continue
            return ("accept", None, None)
        if c == "d":
            return ("defer", None, None)
        if c == "s":
            return ("skip", None, None)
        if c == "q":
            return ("q", None, None)
        if c == "r":
            out.write("  reason: " + "  ".join(
                "[{0}] {1}".format(k, label)
                for k, _, label in REJECT_REASONS) + " > ")
            out.flush()
            rline = inp.readline().strip()
            reason = dict((k, v) for k, v, _ in REJECT_REASONS).get(
                rline, "other")
            return ("reject", reason, None)
        if c == "e" and editable:
            payload = json.loads(json.dumps(prop["payload"]))
            if prop["op"] in ("create_task", "add_subtask"):
                # Owner first when a missing owner is what sent the reviewer
                # here. They were just told "use [e]dit-accept to name one",
                # and asking for the title before the owner invites them to
                # type the person into it -- which silently renames the task
                # and leaves the owner still unanswered. Observed on the
                # first live review, so the prompt now matches the sentence
                # that sent them to it. When the proposal already carries an
                # owner there is no such instruction and the familiar order
                # stands.
                owner_first = (prop["op"] == "create_task"
                               and needs_owner(prop))
                if owner_first:
                    owner = prompt_owner(payload.get("owner_ref"), inp, out,
                                         known)
                    if owner is None:
                        return ("q", None, None)
                    payload["owner_ref"] = owner
                title = prompt_title(payload.get("title"), inp, out, known)
                if title is None:
                    return ("q", None, None)
                payload["title"] = title
                d = prompt_deadline(payload.get("deadline"), inp, out)
                if d is _KEEP_ASKING:
                    return ("q", None, None)
                payload["deadline"] = d
                if prop["op"] == "create_task" and not owner_first:
                    owner = prompt_owner(payload.get("owner_ref"), inp, out,
                                         known)
                    if owner is None:
                        return ("q", None, None)
                    payload["owner_ref"] = owner
            elif prop["op"] == "update_deadline":
                d = prompt_deadline(payload.get("to_deadline"), inp, out)
                if d is _KEEP_ASKING:
                    return ("q", None, None)
                payload["to_deadline"] = d
            elif prop["op"] == "update_status":
                out.write("  status [{0}]: ".format(payload.get("to_status")))
                out.flush()
                s = inp.readline().strip()
                if s in ("proposed", "in_progress", "blocked",
                         "completed", "cancelled"):
                    payload["to_status"] = s
            return ("accept_edited", None, payload)
        out.write("  ?\n")


# ------------------------------------------------------------------ session

def run_session(dsn, user, list_only=False, bulk_unconflicted=False,
                inp=None, out=None):
    inp = inp or sys.stdin
    out = out or sys.stdout
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    stats = {"accepted": 0, "rejected": 0, "deferred": 0, "invalidated": 0,
             "skipped": 0, "dismissed": 0, "aliases": 0,
             "created_tasks": []}
    try:
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            packs = pending_packets(cur, user)
            if not packs:
                out.write("Nothing waiting for {0}.\n".format(user))
                conn.rollback()
                return stats
            owners = known_owners(cur)
            out.write("Pending for {0}:\n".format(user))
            n_cards = sum(int(pk["undecided"]) for pk in packs)
            out.write("  {0} card(s) -- about {1} seconds. Nothing changes "
                      "until you answer.\n".format(n_cards, n_cards * 10))
            for pk in packs:
                out.write("  {0}  {1} undecided of {2}  (board v{3}, {4})\n"
                          .format(pk["packet_id"][:12], pk["undecided"],
                                  pk["proposals"], pk["board_version"],
                                  pk["state"]))
            if list_only:
                conn.rollback()
                return stats

            packet = packs[0]
            if packet["state"] == "draft":
                record_state(cur, packet["packet_id"], "delivered")
            props = undecided_proposals(cur, packet["packet_id"])
            total = len(props)
            first_decision = True

            for i, prop in enumerate(props, 1):
                evidence = evidence_for(cur, prop["proposal_id"])
                valid, why = still_valid(prop)

                if bulk_unconflicted:
                    if needs_owner(prop):
                        # Same refusal as the interactive path: the bulk
                        # sweep is a plain accept with no human at the
                        # prompt, so it must not create ownerless tasks.
                        out.write("  needs an owner, left pending: {0}\n"
                                  .format((prop["payload"] or {})
                                          .get("title")))
                        stats["skipped"] += 1
                        continue
                    if prop["conflict_type"] or not valid:
                        stats["skipped"] += 1
                        continue
                    choice, reason, edited = ("accept", None, None)
                else:
                    out.write(render(prop, evidence, i, total) + "\n")
                    if not valid:
                        out.write("  BOARD MOVED: {0} -- accept is "
                                  "unavailable\n".format(why))
                    choice, reason, edited = prompt_decision(
                        prop, inp, out, owners)
                    if choice == "q":
                        break
                    if choice == "skip":
                        stats["skipped"] += 1
                        continue
                    if choice in ("accept", "accept_edited") and not valid:
                        # record the intent, invalidated, do not apply
                        cur.execute("""
                            INSERT INTO decisions (proposal_id, packet_id,
                              decision, decided_by, decided_at,
                              invalidated_reason)
                            VALUES (%s,%s,'accept',%s,%s,%s)
                            ON CONFLICT (proposal_id, decided_by)
                            DO NOTHING""",
                            (prop["proposal_id"], packet["packet_id"],
                             user, now(), why))
                        stats["invalidated"] += 1
                        _post_decision(cur, packet, first_decision)
                        first_decision = False
                        continue

                if choice in ("accept", "accept_edited"):
                    cur.execute("""
                        INSERT INTO decisions (proposal_id, packet_id,
                          decision, edited_payload, decided_by, decided_at)
                        VALUES (%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (proposal_id, decided_by) DO NOTHING
                        RETURNING decision_id""",
                        (prop["proposal_id"], packet["packet_id"], choice,
                         json.dumps(edited) if edited else None, user, now()))
                    if cur.fetchone() is None:
                        stats["skipped"] += 1
                        continue
                    payload = edited or prop["payload"]
                    acted = apply_accept(cur, user, prop, payload)
                    if prop["op"] in ("create_task", "add_subtask"):
                        stats["created_tasks"].append(acted)
                        stats["aliases"] += seed_candidate_aliases(
                            cur, user, prop, acted)
                    elif prop["target_task_id"]:
                        cur.execute("""
                            SELECT s.signal_id, s.title
                            FROM proposal_signals ps
                            JOIN signals s USING (signal_id)
                            WHERE ps.proposal_id = %s""",
                            (prop["proposal_id"],))
                        rows = [((r["signal_id"], r["title"])
                                 if isinstance(r, dict) else (r[0], r[1]))
                                for r in cur.fetchall()]
                        stats["aliases"] += teach_wordings(
                            cur, user, prop["target_task_id"],
                            prop.get("target_title"), rows)
                    stats["accepted"] += 1
                elif choice == "reject":
                    cur.execute("""
                        INSERT INTO decisions (proposal_id, packet_id,
                          decision, reject_reason, decided_by, decided_at)
                        VALUES (%s,%s,'reject',%s,%s,%s)
                        ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                        (prop["proposal_id"], packet["packet_id"], reason,
                         user, now()))
                    stats["dismissed"] += dismiss_promoted_candidate(
                        cur, user, prop)
                    stats["rejected"] += 1
                elif choice == "defer":
                    cur.execute("""
                        INSERT INTO decisions (proposal_id, packet_id,
                          decision, decided_by, decided_at)
                        VALUES (%s,%s,'defer',%s,%s)
                        ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                        (prop["proposal_id"], packet["packet_id"], user,
                         now()))
                    defer_signals(cur, user, prop)
                    stats["deferred"] += 1

                _post_decision(cur, packet, first_decision)
                first_decision = False

            remaining = undecided_proposals(cur, packet["packet_id"])
            if not remaining:
                record_state(cur, packet["packet_id"], "closed")
                out.write("\nPacket closed.")
            out.write("\nSession: {0} accepted, {1} rejected, {2} deferred, "
                      "{3} invalidated, {4} skipped\n".format(
                          stats["accepted"], stats["rejected"],
                          stats["deferred"], stats["invalidated"],
                          stats["skipped"]))
            if stats["dismissed"]:
                out.write("  {0} candidate(s) dismissed: rejecting the card "
                          "retires the work, so it stops being "
                          "offered\n".format(stats["dismissed"]))
            for t in stats["created_tasks"]:
                out.write("  created {0}\n".format(t))
            if stats["aliases"]:
                out.write("  {0} wording(s) learned this session: they "
                          "will match their work directly from now on\n"
                          .format(stats["aliases"]))
        conn.commit()
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _post_decision(cur, packet, first):
    if first and packet["state"] in ("draft", "delivered"):
        record_state(cur, packet["packet_id"], "partially_decided")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as", dest="user", required=True)
    ap.add_argument("--dsn", required=True,
                    help="postgres DSN for the ledger database")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--accept-unconflicted", action="store_true")
    args = ap.parse_args()
    try:
        run_session(args.dsn, args.user, list_only=args.list,
                    bulk_unconflicted=args.accept_unconflicted)
    except psycopg2.OperationalError as e:
        sys.stderr.write(
            "cannot reach the database: {0}\n"
            "  start it (scripts/doctor.py diagnoses the whole stack), "
            "or point --dsn at it:\n  {1}\n".format(
                str(e).strip(), dsn_hint()))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
