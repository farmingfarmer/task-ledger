"""Tests for the viewer-aware dashboard API.

Drives the real Flask app through its test client against a live database,
with its own fixture identities so it never depends on (or disturbs) the
extraction -> reconcile -> review chain the other suites share.

What matters here is not that the endpoints return rows -- it is that they
refuse to return the wrong ones:

  - the SELF-ONLY endpoints (pending, held, calibration) stay self-only
    even for a viewer holding a full_trace grant on the whole board, because
    unreviewed claims and reject reasons are not board state
  - progress-level viewers get the history skeleton and a 403 on provenance
  - a stranger sees nothing at all, and an anonymous request 401s
  - the demo identity picker is off unless explicitly enabled

Run: python3 test_dashboard.py   (skips politely without a database)
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "dashboard"))

CHECKS = [0]

OWNER = "dash-owner"
MATE = "dash-mate"          # full_trace grant on OWNER's board
STAKE = "dash-stake"        # progress grant on OWNER's board
STRANGER = "dash-stranger"  # no grants at all
BLOCKED = "dash-blocked"    # owns a task blocked on OWNER's task
EXPIRED = "dash-expired"    # holds a grant that has already lapsed

TASK = "task-dash-fixture"
PACKET = "packet-dash-fixture"
PROJECT = "proj-dash-fixture"
BLOCKED_TASK = "task-dash-blocked"
BATCH = "batch-dash-fixture"
SIGNAL = "signal-dash-fixture"
SIGNAL_ACCEPTED = "signal-dash-accepted"


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


def seed(conn):
    """Idempotent fixture: a task, a delivered packet with an undecided,
    a rejected and an accepted proposal, a pooled signal, and the two
    grants. Returns the id of the board event that carries provenance."""
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tasks_current WHERE task_id = %s",
                        (TASK,))
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                    "NULL,%s::jsonb)",
                    (TASK, OWNER, json.dumps({
                        "title": "Dashboard fixture task",
                        "status": "in_progress", "owner_ref": OWNER})))
            cur.execute("""
                INSERT INTO extraction_batches (batch_id, extracted_at,
                  extractor_model, prompt_version, source_type, source_ref,
                  window_start, window_end)
                VALUES (%s, now(), 'fixture', 'v0', 'meeting_transcript',
                        'dash-fixture', now() - interval '1 hour', now())
                ON CONFLICT (batch_id) DO NOTHING""", (BATCH,))
            cur.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type, title,
                  confidence, raw)
                VALUES (%s, %s, 'completion_claim',
                        'Fixture claim awaiting corroboration', 0.80, '{}')
                ON CONFLICT (signal_id) DO NOTHING""", (SIGNAL, BATCH))
            cur.execute("""
                INSERT INTO evidence_refs (signal_id, ordinal, source_type,
                  resource_id, content_hash)
                VALUES (%s, 1, 'transcript_utterance', 'utt-dash-1', 'hash')
                ON CONFLICT DO NOTHING""", (SIGNAL,))
            cur.execute("SELECT 1 FROM signal_pool_events WHERE signal_id = %s",
                        (SIGNAL,))
            if cur.fetchone() is None:
                cur.execute("""
                    INSERT INTO signal_pool_events (signal_id, user_ref, event,
                      run_id) VALUES (%s,%s,'pooled','dash-fixture')""",
                    (SIGNAL, OWNER))
            cur.execute("""
                INSERT INTO review_packets (packet_id, user_ref,
                  cadence_run_id, board_version, generated_at, expires_at)
                VALUES (%s,%s,'dash-fixture',1, now(), now() + interval '2 days')
                ON CONFLICT (packet_id) DO NOTHING""", (PACKET, OWNER))
            cur.execute("SELECT 1 FROM packet_state_events WHERE packet_id = %s",
                        (PACKET,))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO packet_state_events (packet_id, state)"
                            " VALUES (%s,'delivered')", (PACKET,))
            for pid, op, conf in (
                    ("prop-dash-open", "update_status", 0.80),
                    ("prop-dash-rejected", "create_task", 0.70),
                    ("prop-dash-accepted", "update_deadline", 0.90)):
                cur.execute("""
                    INSERT INTO proposals (proposal_id, packet_id, op,
                      target_task_id, rationale, confidence, conflict_type,
                      conflict_detail, payload)
                    VALUES (%s,%s,%s,%s,'fixture rationale',%s,NULL,NULL,%s)
                    ON CONFLICT (proposal_id) DO NOTHING""",
                    (pid, PACKET, op, None if op == "create_task" else TASK,
                     conf, json.dumps({"kind": op})))
            # A second signal stands behind the accepted proposal, so the
            # held list keeps exactly one entry and the source count has
            # something real to count.
            cur.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type, title,
                  confidence, deadline_raw, author_ref, raw)
                VALUES (%s, %s, 'deadline_signal', 'Fixture deadline claim',
                        0.90, 'end of next sprint', 'dash-speaker', '{}')
                ON CONFLICT (signal_id) DO NOTHING""", (SIGNAL_ACCEPTED, BATCH))
            cur.execute("""
                INSERT INTO evidence_refs (signal_id, ordinal, source_type,
                  resource_id, content_hash)
                VALUES (%s, 1, 'teams_message', 'msg-dash-1', 'hash-2')
                ON CONFLICT DO NOTHING""", (SIGNAL_ACCEPTED,))
            cur.execute("""
                INSERT INTO proposal_signals (proposal_id, signal_id)
                VALUES ('prop-dash-accepted', %s)
                ON CONFLICT DO NOTHING""", (SIGNAL_ACCEPTED,))
            cur.execute("""
                INSERT INTO decisions (proposal_id, packet_id, decision,
                  reject_reason, decided_by, decided_at)
                VALUES ('prop-dash-rejected',%s,'reject','duplicate',%s,now())
                ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                (PACKET, OWNER))
            cur.execute("""
                INSERT INTO projects (project_id, name, owner_ref)
                VALUES (%s, 'Dashboard fixture project', %s)
                ON CONFLICT (project_id) DO NOTHING""", (PROJECT, OWNER))
            cur.execute("SELECT project_id FROM tasks_current WHERE task_id=%s",
                        (TASK,))
            if (cur.fetchone() or [None])[0] is None:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'project_assigned',"
                    "'manual_edit',NULL,%s::jsonb)",
                    (TASK, OWNER, json.dumps({"project_id": PROJECT})))
            # A second person's task, blocked on the fixture task: the edge
            # is what earns them status_only sight of it.
            cur.execute("SELECT 1 FROM tasks_current WHERE task_id = %s",
                        (BLOCKED_TASK,))
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                    "NULL,%s::jsonb)",
                    (BLOCKED_TASK, BLOCKED, json.dumps({
                        "title": "Work waiting on the fixture task",
                        "status": "in_progress", "owner_ref": BLOCKED})))
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'blocker_added',"
                    "'manual_edit',NULL,%s::jsonb)",
                    (BLOCKED_TASK, BLOCKED, json.dumps({
                        "description": "waiting on the fixture task",
                        "blocking_task_id": TASK})))
            for who, level in ((MATE, "full_trace"), (STAKE, "progress")):
                cur.execute("""
                    SELECT 1 FROM v_current_grants
                    WHERE grantee_ref = %s AND subject_type = 'user'
                      AND subject_id = %s AND level = %s""",
                    (who, OWNER, level))
                if cur.fetchone() is None:
                    cur.execute("SELECT grant_access(%s,%s,'user',%s,%s,%s)",
                                (OWNER, who, OWNER, level, "dashboard test"))
            # The accepted proposal becomes a board event through the only
            # writer, carrying the raw phrase the reviewer saw.
            cur.execute("""
                SELECT event_id FROM task_events
                WHERE task_id = %s AND event_type = 'deadline_changed'
                ORDER BY event_id LIMIT 1""", (TASK,))
            existing = cur.fetchone()
            if existing:
                return existing[0]
            cur.execute("""
                INSERT INTO decisions (proposal_id, packet_id, decision,
                  decided_by, decided_at)
                VALUES ('prop-dash-accepted',%s,'accept',%s,now())
                ON CONFLICT (proposal_id, decided_by) DO NOTHING
                RETURNING decision_id""", (PACKET, OWNER))
            did = cur.fetchone()[0]
            cur.execute(
                "SELECT apply_task_event(%s,%s,'deadline_changed',"
                "'user_decision',%s,%s::jsonb)",
                (TASK, OWNER, did,
                 json.dumps({"from_deadline": None,
                             "to_deadline": "2026-09-30",
                             "raw_text": "end of next sprint"})))
            cur.execute("""
                SELECT event_id FROM task_events
                WHERE task_id = %s AND event_type = 'deadline_changed'
                ORDER BY event_id LIMIT 1""", (TASK,))
            return cur.fetchone()[0]


def as_viewer(client, path, who=None):
    headers = {"X-Viewer": who} if who else {}
    r = client.get(path, headers=headers)
    body = r.get_json()
    return r.status_code, body


def main():
    dsn = os.environ.get(
        "DB_DSN",
        "host=/home/claude/pgsock port=5433 dbname=taskintel user=postgres")
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
    except Exception as e:
        print("skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return
    os.environ["DB_DSN"] = dsn
    os.environ.pop("TL_VIEWER_PICKER", None)
    import dashboard_api
    dashboard_api.DSN = dsn
    client = dashboard_api.app.test_client()

    EVENT = [seed(conn)]
    conn.close()

    print("identity")
    code, _ = as_viewer(client, "/api/board/" + OWNER)
    ok(code == 401, "anonymous request is refused")

    print("self-only endpoints")
    code, pend = as_viewer(client, "/api/pending", OWNER)
    ok(code == 200 and pend["undecided"] == 1
       and pend["packets"][0]["packet_id"] == PACKET,
       "owner sees their own pending packet and undecided count")
    code, mate_pend = as_viewer(client, "/api/pending", MATE)
    ok(code == 200 and mate_pend["undecided"] == 0
       and mate_pend["packets"] == [],
       "full_trace grantee sees NO pending packets of the board owner")

    code, held = as_viewer(client, "/api/held", OWNER)
    ok(code == 200 and any(h["signal_id"] == SIGNAL for h in held)
       and [h for h in held if h["signal_id"] == SIGNAL][0]["sources"] == 1,
       "owner sees the pooled claim with its source count")
    code, mate_held = as_viewer(client, "/api/held", MATE)
    ok(code == 200 and not [h for h in mate_held if h["signal_id"] == SIGNAL],
       "full_trace grantee sees none of the owner's held claims")

    code, cal = as_viewer(client, "/api/calibration", OWNER)
    reasons = {(r["op"], r["reject_reason"]): r["n"] for r in cal["rejects"]}
    ok(code == 200 and cal["decisions_total"] >= 1
       and reasons.get(("create_task", "duplicate"), 0) >= 1,
       "owner's reject histogram routes duplicate/create_task")
    ok(any(d["op"] == "create_task" and d["accept_rate"] == 0.0
           for d in cal["drift"]),
       "acceptance rate reflects the rejected proposal")
    code, mate_cal = as_viewer(client, "/api/calibration", MATE)
    ok(code == 200 and mate_cal["decisions_total"] == 0
       and mate_cal["rejects"] == [],
       "full_trace grantee sees none of the owner's reject reasons")

    print("board scoping (unchanged paths)")
    code, board = as_viewer(client, "/api/board/" + OWNER, STAKE)
    ok(code == 200 and any(t["task_id"] == TASK for t in board)
       and [t for t in board if t["task_id"] == TASK][0]["access_level"]
       == "progress",
       "progress grantee sees the board row at progress level")
    code, board = as_viewer(client, "/api/board/" + OWNER, STRANGER)
    ok(code == 200 and board == [],
       "stranger sees an empty board, not an error revealing it exists")

    code, hist = as_viewer(client, "/api/task/{0}/history".format(TASK), STAKE)
    ok(code == 200 and hist["access_level"] == "progress"
       and all(e["decided_by"] is None for e in hist["events"]),
       "progress history keeps the skeleton and drops decider identity")
    code, _ = as_viewer(client, "/api/task/{0}/history".format(TASK), STRANGER)
    ok(code == 403, "stranger is refused the history")

    print("board information")
    code, board = as_viewer(client, "/api/board/" + OWNER, OWNER)
    row = [t for t in board if t["task_id"] == TASK][0]
    ok(row["quiet_days"] is not None and row["quiet_days"] >= 0,
       "board rows carry quiet-since, so stale work is visible without a "
       "deadline")
    ok(row["deadline_raw"] == "end of next sprint",
       "the phrase behind the date travels to the board ({0})".format(
           row["deadline_raw"]))
    ok(row["sources"] >= 1,
       "confirmed state carries how many distinct claims back it")
    code, board = as_viewer(client, "/api/board/" + OWNER, STAKE)
    row = [t for t in board if t["task_id"] == TASK][0]
    ok(row["deadline_raw"] is None and row["sources"] is None
       and row["quiet_days"] is not None,
       "progress level keeps quiet-since but not the quote or the "
       "source count")

    print("provenance authorship")
    code, prov = as_viewer(client, "/api/event/{0}/provenance".format(EVENT[0]),
                           OWNER)
    ok(code == 200 and prov[0]["author_ref"] == "dash-speaker",
       "the rail says who spoke, not just which message")
    ok(all("author_ref" in r for r in prov),
       "author travels with every provenance row, NULL where unrecorded "
       "(append-only signals predating the migration are never backfilled)")

    print("evidence integrity")
    code, prov = as_viewer(client, "/api/event/{0}/provenance".format(EVENT[0]),
                           OWNER)
    ok(code == 200 and prov and prov[0]["integrity"] == "unverified"
       and "no message fetcher" in prov[0]["integrity_note"],
       "an unchecked hash says so rather than looking checked")

    print("blocker-implied visibility")
    code, board = as_viewer(client, "/api/board/" + OWNER, BLOCKED)
    rows = [t for t in board if t["task_id"] == TASK]
    ok(code == 200 and len(rows) == 1
       and rows[0]["access_level"] == "status_only"
       and rows[0].get("blocking_you") is True,
       "being blocked on a task earns sight of that task")
    ok("deadline" not in rows[0] and "owner_ref" not in rows[0]
       and "quiet_days" not in rows[0] and "project_name" not in rows[0],
       "status_only is title and status: no deadline, owner, project or age")
    code, _ = as_viewer(client, "/api/task/{0}/history".format(TASK), BLOCKED)
    ok(code == 403, "status_only earns no history")
    code, waiting = as_viewer(client, "/api/waiting-on/" + BLOCKED, BLOCKED)
    ok(code == 200 and any(w["blocking_task"] == TASK
                           and w["blocking_status"] is not None
                           for w in waiting),
       "the blocked person can see whether the blocking task is moving")

    print("grant expiry")
    with psycopg2.connect(dsn) as c2:
        with c2.cursor() as cur2:
            cur2.execute("""
                INSERT INTO access_grant_events (grantee_ref, subject_type,
                  subject_id, level, event, granted_by, expires_at)
                VALUES (%s,'user',%s,'progress','granted',%s,
                        now() - interval '1 hour')""",
                (EXPIRED, OWNER, OWNER))
    c2.close()
    code, board = as_viewer(client, "/api/board/" + OWNER, EXPIRED)
    ok(code == 200 and board == [],
       "an expired grant stops applying without anyone revoking it")
    code, _ = as_viewer(client, "/api/task/{0}/history".format(TASK), EXPIRED)
    ok(code == 403, "expiry closes the history path too, via the same view")

    print("project directory")
    code, projs = as_viewer(client, "/api/projects", OWNER)
    mine = [p for p in projs if p["project_id"] == PROJECT]
    ok(code == 200 and mine and mine[0]["is_owner"] is True
       and mine[0]["tasks"] >= 1,
       "an owner sees their project and its visible task count")
    code, projs = as_viewer(client, "/api/projects", STRANGER)
    ok(code == 200 and not [p for p in projs if p["project_id"] == PROJECT],
       "a stranger does not learn the project exists")

    print("demo picker")
    code, _ = as_viewer(client, "/api/viewers", OWNER)
    ok(code == 404, "identity picker is off by default")
    os.environ["TL_VIEWER_PICKER"] = "1"
    code, v = as_viewer(client, "/api/viewers", OWNER)
    ok(code == 200 and OWNER in v["viewers"],
       "identity picker answers only when explicitly enabled")
    os.environ.pop("TL_VIEWER_PICKER", None)


if __name__ == "__main__":
    main()
    print("\nall {0} dashboard checks passed".format(CHECKS[0]))
