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


def prompt_owner(current, inp, out):
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
        if val:
            return val
        out.write("  an owner is required: a task with none is invisible "
                  "on every board\n")


def prompt_decision(prop, inp, out):
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
                out.write("  title [{0}]: ".format(payload.get("title")))
                out.flush()
                t = inp.readline().strip()
                if t:
                    payload["title"] = t[:120]
                out.write("  deadline YYYY-MM-DD [{0}]: ".format(
                    payload.get("deadline") or "-"))
                out.flush()
                d = inp.readline().strip()
                if d:
                    payload["deadline"] = d
                if prop["op"] == "create_task":
                    owner = prompt_owner(payload.get("owner_ref"), inp, out)
                    if owner is None:
                        return ("q", None, None)
                    payload["owner_ref"] = owner
            elif prop["op"] == "update_deadline":
                out.write("  deadline YYYY-MM-DD [{0}]: ".format(
                    payload.get("to_deadline")))
                out.flush()
                d = inp.readline().strip()
                if d:
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
             "skipped": 0, "created_tasks": []}
    try:
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            packs = pending_packets(cur, user)
            if not packs:
                out.write("Nothing waiting for {0}.\n".format(user))
                conn.rollback()
                return stats
            out.write("Pending for {0}:\n".format(user))
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
                    choice, reason, edited = prompt_decision(prop, inp, out)
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
                    stats["accepted"] += 1
                elif choice == "reject":
                    cur.execute("""
                        INSERT INTO decisions (proposal_id, packet_id,
                          decision, reject_reason, decided_by, decided_at)
                        VALUES (%s,%s,'reject',%s,%s,%s)
                        ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                        (prop["proposal_id"], packet["packet_id"], reason,
                         user, now()))
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
            for t in stats["created_tasks"]:
                out.write("  created {0}\n".format(t))
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
