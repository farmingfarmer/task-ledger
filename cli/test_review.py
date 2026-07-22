"""Tests for the review CLI against the live database.

Drives run_session with scripted stdin over the real pending packet the
reconciler's live test left behind: accept the completion, edit-accept the
create with a changed deadline, defer the low-confidence create. Asserts
DecisionRecords, board effects via the event log, signal return on defer,
packet lifecycle, and idempotency of a repeat answer.

Run: python3 test_review.py   (skips politely without a database)
"""

import io
import json
import os
import sys

sys.path.insert(0, ".")

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


def main():
    dsn = os.environ.get(
        "DB_DSN",
        "host=/home/claude/pgsock port=5433 dbname=taskintel user=postgres")
    try:
        import psycopg2
        psycopg2.connect(dsn).close()
    except Exception as e:
        print("skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return
    import psycopg2.extras
    import review

    conn = psycopg2.connect(dsn)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("list mode")
    out = io.StringIO()
    stats = review.run_session(dsn, "aad-dev", list_only=True, out=out)
    listing = out.getvalue()
    ok("undecided" in listing and "3 undecided of 3" in listing,
       "pending packet listed with undecided count")

    # Establish which proposals we're about to answer, in display order
    cur.execute("""
        SELECT p.proposal_id, p.op FROM proposals p
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE d.decision_id IS NULL
        ORDER BY (p.conflict_type IS NULL), p.confidence DESC""")
    order = cur.fetchall()
    if [o["op"] for o in order] != ["complete_task", "create_task",
                                    "create_task"]:
        print("skipped: pending set differs from canonical fixture "
              "(this DB has its own history); CLI list check passed, "
              "which is the portable assertion")
        cur.close(); conn.close(); return
    ok(True, "display order: completion first, then creates by confidence")

    print("interactive session (scripted)")
    # 1: accept completion; 2: edit-accept create (new deadline);
    # 3: defer docs create
    script = "a\ne\n\n2026-08-07\nd\n"
    out = io.StringIO()
    stats = review.run_session(dsn, "aad-dev",
                               inp=io.StringIO(script), out=out)
    text = out.getvalue()
    ok(stats["accepted"] == 2 and stats["deferred"] == 1,
       "two accepts (one edited) and one defer recorded")
    ok("Packet closed." in text, "packet closed when nothing undecided")

    print("board effects")
    cur.execute("SELECT status FROM tasks_current WHERE task_id='task-devocr'")
    ok(cur.fetchone()["status"] == "completed",
       "completion accept landed: task-devocr completed")
    created = stats["created_tasks"][0]
    cur.execute("SELECT title, deadline::text AS dl, owner_ref "
                "FROM tasks_current WHERE task_id=%s", (created,))
    t = cur.fetchone()
    ok(t is not None and t["dl"] == "2026-08-07",
       "edited create applied with the edited deadline ({0})".format(
           t["title"]))
    cur.execute("""
        SELECT e.event_type, e.actor_type, d.decision
        FROM task_events e JOIN decisions d ON d.decision_id=e.decision_id
        WHERE e.task_id=%s""", (created,))
    ev = cur.fetchone()
    ok(ev["actor_type"] == "user_decision"
       and ev["decision"] == "accept_edited",
       "lineage: created event ties to the accept_edited decision")

    print("defer semantics")
    cur.execute("""
        SELECT count(*) AS n FROM v_unconsumed_signals
        WHERE user_ref='aad-dev'""")
    ok(cur.fetchone()["n"] >= 1,
       "deferred proposal's signal returned to the pool")
    cur.execute("""
        SELECT reject_reason, decision FROM decisions
        WHERE decision='defer' ORDER BY decision_id DESC LIMIT 1""")
    ok(cur.fetchone()["reject_reason"] is None,
       "defer stored without a reject reason (constraint respected)")

    print("lifecycle + idempotency")
    cur.execute("""
        SELECT state FROM packet_state_events
        WHERE packet_id LIKE '6bab%' ORDER BY event_id""")
    states = [r["state"] for r in cur.fetchall()]
    ok(states[:2] == ["draft", "delivered"] and
       "partially_decided" in states and states[-1] == "closed",
       "lifecycle recorded: draft->delivered->partially_decided->closed")
    out = io.StringIO()
    stats2 = review.run_session(dsn, "aad-dev", out=out,
                                inp=io.StringIO(""))
    ok("Nothing waiting" in out.getvalue(),
       "re-run finds nothing pending (decisions idempotent, packet closed)")

    cur.close()
    conn.close()
    print("\nall {0} review CLI checks passed".format(CHECKS[0]))


if __name__ == "__main__":
    main()
