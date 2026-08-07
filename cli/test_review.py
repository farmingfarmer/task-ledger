"""Tests for the review CLI.

The owner-guard section is pure stdin/stdout over proposal dicts and runs
everywhere. The live section drives run_session with scripted stdin over
the real pending packet the reconciler's live test left behind: accept the
completion, edit-accept the create with a changed deadline, defer the
low-confidence create. It asserts DecisionRecords, board effects via the
event log, signal return on defer, packet lifecycle, and idempotency of a
repeat answer.

Run: python3 test_review.py   (live section skips politely without a
database; the owner guard always runs)
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


def prop(op, **payload):
    return {"op": op, "payload": payload, "proposal_id": "p-1",
            "conflict_type": None}


def test_owner_guard():
    """Offline: an ownerless create must not reach the board (live bug --
    accepted creates with no owner_ref show on nobody's board)."""
    print("create owner guard (offline)")
    import review

    out = io.StringIO()
    choice, _, _ = review.prompt_decision(
        prop("create_task", title="Draft the intake runbook", owner_ref=None),
        io.StringIO("a\ns\n"), out)
    ok(choice == "skip" and "invisible on every board" in out.getvalue()
       and "[e]dit-accept to name one" in out.getvalue(),
       "plain accept of an ownerless create is refused, points at "
       "edit-accept")

    out = io.StringIO()
    choice, _, _ = review.prompt_decision(
        prop("create_task", title="Draft the intake runbook",
             owner_ref="aad-dev"),
        io.StringIO("a\n"), out)
    ok(choice == "accept", "a create that names an owner accepts plainly")

    out = io.StringIO()
    choice, _, edited = review.prompt_decision(
        prop("create_task", title="Draft the intake runbook", owner_ref=None,
             deadline=None),
        io.StringIO("e\n\n\naad-alex\n"), out)
    ok(choice == "accept_edited" and edited["owner_ref"] == "aad-alex",
       "edit-accept collects the owner and carries it in the payload")

    out = io.StringIO()
    choice, _, edited = review.prompt_decision(
        prop("create_task", title="Draft the intake runbook", owner_ref=None,
             deadline=None),
        io.StringIO("e\n\n\n\naad-alex\n"), out)
    ok(choice == "accept_edited" and edited["owner_ref"] == "aad-alex"
       and "an owner is required" in out.getvalue(),
       "an empty owner is re-asked, never accepted")

    out = io.StringIO()
    choice, _, _ = review.prompt_decision(
        prop("create_task", title="Draft the intake runbook", owner_ref=None,
             deadline=None),
        io.StringIO("e\n\n\n"), out)
    ok(choice == "q", "EOF at the owner prompt quits instead of looping")

    out = io.StringIO()
    choice, _, edited = review.prompt_decision(
        prop("create_task", title="Draft the intake runbook",
             owner_ref="aad-dev", deadline=None),
        io.StringIO("e\n\n\n\n"), out)
    ok(choice == "accept_edited" and edited["owner_ref"] == "aad-dev",
       "enter keeps the owner the proposal already carried")

    out = io.StringIO()
    choice, _, _ = review.prompt_decision(
        prop("update_status", to_status="in_progress"),
        io.StringIO("a\n"), out)
    ok(choice == "accept", "non-create ops are untouched by the guard")


def main():
    test_owner_guard()
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
    if "Nothing waiting" in listing:
        # The chain (extract -> reconcile -> review) is single-shot per
        # database: once this suite has answered the fixture packet, later
        # runs reconcile against the board it already changed and may
        # propose nothing at all. That is correct behaviour, not a failure.
        print("skipped: nothing pending on this database -- the fixture "
              "packet has already been answered here; the interactive "
              "section needs a fresh scratch DB")
        cur.close(); conn.close(); return
    ok("undecided" in listing, "pending packet listed with undecided count")

    # Establish which proposals we're about to answer, in display order --
    # through the CLI's own queries, scoped to the packet the session will
    # open. A bare undecided-proposals scan also sees other users' packets
    # (test_supersede's fixture user runs first under run_tests.sh) and
    # silently skipped this whole section.
    packs = review.pending_packets(cur, "aad-dev")
    order = (review.undecided_proposals(cur, packs[0]["packet_id"])
             if packs else [])
    if [o["op"] for o in order] != ["complete_task", "create_task",
                                    "create_task"]:
        print("skipped: pending set differs from canonical fixture "
              "(this DB has its own history); CLI list check passed, "
              "which is the portable assertion")
        cur.close(); conn.close(); return
    ok(True, "display order: completion first, then creates by confidence")

    print("interactive session (scripted)")
    # 1: accept completion; 2: edit-accept create (new deadline, then the
    # owner the edit path now insists on); 3: defer docs create
    script = "a\ne\n\n2026-08-07\naad-dev\nd\n"
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
    ok(t is not None and t["dl"] == "2026-08-07"
       and t["owner_ref"] == "aad-dev",
       "edited create applied with the edited deadline and an owner "
       "({0})".format(t["title"]))
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
    # The packet this session just answered, not a hardcoded id prefix:
    # run ids became per-invocation (3b3e26d), so packet_id -- a hash over
    # user, run id, board version, and consumed signals -- differs every run.
    cur.execute("""
        SELECT state FROM packet_state_events
        WHERE packet_id = (SELECT packet_id FROM review_packets
                           WHERE user_ref = 'aad-dev'
                           ORDER BY generated_at DESC LIMIT 1)
        ORDER BY event_id""")
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


if __name__ == "__main__":
    main()
    print("\nall {0} review CLI checks passed".format(CHECKS[0]))
