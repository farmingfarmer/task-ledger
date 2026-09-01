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
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "dashboard"))

CHECKS = [0]

OWNER = "dash-owner"
MATE = "dash-mate"          # full_trace grant on OWNER's board
STAKE = "dash-stake"        # progress grant on OWNER's board
STRANGER = "dash-stranger"  # no grants at all
BLOCKED = "dash-blocked"    # owns a task blocked on OWNER's task
EXPIRED = "dash-expired"    # holds a grant that has already lapsed
PACKET_USER = "dash-boardless"  # a packet waiting, no board at all

TASK = "task-dash-fixture"
PACKET = "packet-dash-fixture"
PROJECT = "proj-dash-fixture"
BLOCKED_TASK = "task-dash-blocked"
BATCH = "batch-dash-fixture"
SIGNAL = "signal-dash-fixture"
SIGNAL_ACCEPTED = "signal-dash-accepted"
SIGNAL_CREATE = "signal-dash-create"
# The owner-correction case is DECIDED by this run, and decisions are
# append-only -- a fixed id would be undecided on the first run and decided
# on every one after, so the undecided count would not survive a re-run.
# A fresh id per run keeps exactly one of these open at the point it counts.
ATTRIB_PROP = "prop-dash-attrib-{0}".format(int(time.time() * 1000))
ATTRIB_SIGNAL = "sig-dash-attrib-{0}".format(int(time.time() * 1000))
# A claim an assistant asserted and produced no locator for. Fresh id per
# run for the same reason as ATTRIB_PROP: this run rules on it.
UNCITED_PROP = "prop-dash-uncited-{0}".format(int(time.time() * 1000))
UNCITED_SIGNAL = "sig-dash-uncited-{0}".format(int(time.time() * 1000))
# Ruled on by this run, and candidate events are append-only, so a
# fixed id would be "sighted" once and "dismissed" ever after.
CAND_ID = "cand-dash-{0}".format(int(time.time() * 1000))
# Its own task: the auto-apply case appends status events, and
# hanging them off the shared fixture changed what "the last thing
# noted about it" meant for an assertion three sections earlier.
AUTO_TASK = "task-dash-auto"
CREATED_TASK = "task-dash-created"


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
            # FIRST, before any of the early returns below: close attrib
            # proposals left open by an earlier run. The fresh-id-per-run
            # trick assumes each run DECIDES the one it creates, and a run
            # that dies before that point leaks it -- every later run then
            # counts one more undecided proposal and fails an assertion
            # about state it never created. Deciding them is append-only,
            # and it restores the invariant the count depends on: exactly
            # one open attrib proposal, this run's.
            cur.execute("""
                INSERT INTO decisions (proposal_id, packet_id, decision,
                  decided_by, reject_reason, decided_at)
                SELECT p.proposal_id, p.packet_id, 'reject', %s, 'stale',
                       now()
                FROM proposals p
                LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
                WHERE (p.proposal_id LIKE 'prop-dash-attrib-%%'
                       OR p.proposal_id LIKE 'prop-dash-uncited-%%')
                  AND p.proposal_id NOT IN (%s, %s)
                  AND d.decision_id IS NULL
                ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                (OWNER, ATTRIB_PROP, UNCITED_PROP))
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
            # New work whose attribution is contestable: the extractor put
            # it on the owner but named a second person in the same window,
            # which is the case the owner-correction control exists for.
            # add_subtask rather than create_task deliberately -- the control
            # covers both, decisions are append-only, and every run of this
            # suite leaves one more behind, so borrowing create_task here
            # would inflate the acceptance-rate counts asserted above.
            cur.execute("""
                INSERT INTO proposals (proposal_id, packet_id, op,
                  target_task_id, rationale, confidence, conflict_type,
                  conflict_detail, payload)
                VALUES (%s,%s,'add_subtask',%s,
                        'fixture rationale',0.66,NULL,NULL,%s)
                ON CONFLICT (proposal_id) DO NOTHING""",
                (ATTRIB_PROP, PACKET, TASK,
                 json.dumps({"kind": "add_subtask",
                             "title": "Fixture attributed task",
                             "owner_ref": OWNER})))
            cur.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type, title,
                  confidence, author_ref, owner_candidates, raw)
                VALUES (%s, %s, 'assignment',
                        'Fixture attributed claim', 0.66, 'dash-speaker',
                        %s, '{}')
                ON CONFLICT (signal_id) DO NOTHING""",
                (ATTRIB_SIGNAL, BATCH, json.dumps([
                    {"person_ref": OWNER, "basis": "inferred",
                     "confidence": 0.6},
                    {"person_ref": MATE, "basis": "inferred",
                     "confidence": 0.4}])))
            # It needs a citation like any other signal. Without one this
            # card is indistinguishable from an assistant assertion with
            # nothing behind it, which the browser now refuses to let
            # anyone answer -- and the fixture would be testing owner
            # correction on a card no real reviewer could reach.
            cur.execute("""
                INSERT INTO evidence_refs (signal_id, ordinal, source_type,
                  resource_id, content_hash)
                VALUES (%s, 1, 'transcript_utterance', %s, 'hash-attrib')
                ON CONFLICT DO NOTHING""",
                (ATTRIB_SIGNAL, "utt-" + ATTRIB_SIGNAL))
            cur.execute("""
                INSERT INTO proposal_signals (proposal_id, signal_id)
                VALUES (%s,%s)
                ON CONFLICT DO NOTHING""", (ATTRIB_PROP, ATTRIB_SIGNAL))
            # And its opposite: the same shape of card, cited only as
            # 'assistant_claim' -- something asserted with no utterance
            # released to check it against. Seeded here so the refusal is
            # tested against a card that is otherwise perfectly ordinary.
            cur.execute("""
                INSERT INTO proposals (proposal_id, packet_id, op,
                  target_task_id, rationale, confidence, conflict_type,
                  conflict_detail, payload)
                VALUES (%s,%s,'create_task',NULL,
                        'asserted by an assistant',0.88,NULL,NULL,%s)
                ON CONFLICT (proposal_id) DO NOTHING""",
                (UNCITED_PROP, PACKET,
                 json.dumps({"kind": "create_task",
                             "title": "Fixture uncited claim",
                             "owner_ref": OWNER})))
            cur.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type, title,
                  confidence, owner_candidates, raw)
                VALUES (%s, %s, 'task_mention', 'Fixture uncited claim',
                        0.88, %s, '{}')
                ON CONFLICT (signal_id) DO NOTHING""",
                (UNCITED_SIGNAL, BATCH, json.dumps([
                    {"person_ref": OWNER, "basis": "inferred",
                     "confidence": 0.6}])))
            cur.execute("""
                INSERT INTO evidence_refs (signal_id, ordinal, source_type,
                  resource_id, content_hash)
                VALUES (%s, 1, 'assistant_claim', %s, NULL)
                ON CONFLICT DO NOTHING""",
                (UNCITED_SIGNAL, "claim-" + UNCITED_SIGNAL))
            cur.execute("""
                INSERT INTO proposal_signals (proposal_id, signal_id)
                VALUES (%s,%s)
                ON CONFLICT DO NOTHING""", (UNCITED_PROP, UNCITED_SIGNAL))
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
            cur.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type, title,
                  confidence, author_ref, raw)
                VALUES (%s, %s, 'proposal', 'Fixture claim that became work',
                        0.85, 'dash-speaker', '{}')
                ON CONFLICT (signal_id) DO NOTHING""", (SIGNAL_CREATE, BATCH))
            cur.execute("""
                INSERT INTO evidence_refs (signal_id, ordinal, source_type,
                  resource_id, content_hash)
                VALUES (%s, 1, 'transcript_utterance', 'utt-dash-2', 'hash-3')
                ON CONFLICT DO NOTHING""", (SIGNAL_CREATE,))
            for sid in (SIGNAL_ACCEPTED, SIGNAL_CREATE):
                cur.execute("SELECT 1 FROM signal_pool_events "
                            "WHERE signal_id = %s", (sid,))
                if cur.fetchone() is None:
                    cur.execute("""
                        INSERT INTO signal_pool_events (signal_id, user_ref,
                          event, run_id) VALUES (%s,%s,'taken','dash-fixture')""",
                        (sid, OWNER))
            cur.execute("""
                INSERT INTO proposals (proposal_id, packet_id, op,
                  target_task_id, rationale, confidence, conflict_type,
                  conflict_detail, payload)
                VALUES ('prop-dash-create',%s,'create_task',NULL,
                        'fixture create',0.88,NULL,NULL,%s)
                ON CONFLICT (proposal_id) DO NOTHING""",
                (PACKET, json.dumps({"kind": "create_task",
                                     "title": "Work from the fixture meeting",
                                     "owner_ref": OWNER})))
            cur.execute("""
                INSERT INTO proposal_signals (proposal_id, signal_id)
                VALUES ('prop-dash-create', %s)
                ON CONFLICT DO NOTHING""", (SIGNAL_CREATE,))
            cur.execute("SELECT 1 FROM tasks_current WHERE task_id = %s",
                        (CREATED_TASK,))
            if cur.fetchone() is None:
                cur.execute("""
                    INSERT INTO decisions (proposal_id, packet_id, decision,
                      decided_by, decided_at)
                    VALUES ('prop-dash-create',%s,'accept',%s,now())
                    ON CONFLICT (proposal_id, decided_by) DO NOTHING
                    RETURNING decision_id""", (PACKET, OWNER))
            got = cur.fetchone()
            if got:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'created','user_decision',"
                    "%s,%s::jsonb)",
                    (CREATED_TASK, OWNER, got[0], json.dumps({
                        "title": "Work from the fixture meeting",
                        "status": "in_progress", "owner_ref": OWNER})))
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
        "host=/tmp/pgsock port=5433 dbname=taskintel user=postgres")
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
    ok(code == 200 and pend["undecided"] == 3
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
    creates = [d for d in cal["drift"] if d["op"] == "create_task"]
    ok(creates and creates[0]["n"] == 2
       and abs(creates[0]["accept_rate"] - 0.5) < 1e-9,
       "acceptance rate counts both decisions on create_task "
       "(one accepted, one rejected)")
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

    print("work card fields")
    code, board = as_viewer(client, "/api/board/" + OWNER, OWNER)
    row = [t for t in board if t["task_id"] == TASK][0]
    ok(row["last_noted"] is not None
       and row["last_noted"]["event_type"] == "deadline_changed",
       "a task carries the last thing the ledger noted about it")
    ok(row["last_noted"]["on_whose_word"] == "dash-speaker",
       "and on whose word, from the signals behind that decision")
    ok(any(w["ref"] == BLOCKED and w["relation"] == "waiting on this"
           for w in row["with"]),
       "collaborators are on the card: who is waiting on this work")
    code, board = as_viewer(client, "/api/board/" + OWNER, STAKE)
    row = [t for t in board if t["task_id"] == TASK][0]
    ok(row["last_noted"] is not None
       and row["last_noted"]["on_whose_word"] is None,
       "progress level keeps WHAT changed and drops on whose word: the "
       "speaker is provenance")

    print("evidence integrity")
    code, prov = as_viewer(client, "/api/event/{0}/provenance".format(EVENT[0]),
                           OWNER)
    ok(code == 200 and prov and prov[0]["integrity"] == "unverified"
       and "no message fetcher" in prov[0]["integrity_note"],
       "an unchecked hash says so rather than looking checked")

    print("confirmed-by-you")
    code, mine = as_viewer(client, "/api/recent", OWNER)
    ok(code == 200 and any(r["task_id"] == TASK
                           and r["decision"] == "accept" for r in mine),
       "your own confirmations come back to you as an accountability record")
    code, theirs = as_viewer(client, "/api/recent", MATE)
    ok(code == 200 and theirs == [],
       "a full_trace grantee sees none of the owner's decisions: it filters "
       "on who decided, not on who can read the board")

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

    print("meeting follow-through")
    code, mtgs = as_viewer(client, "/api/meetings", OWNER)
    mine = [m for m in mtgs if m["source_ref"] == "dash-fixture"]
    ok(code == 200 and mine, "the viewer's own conversation is reported")
    m = mine[0]
    ok(m["claims"] == 3 and m["on the board"] >= 1 and m["waiting"] >= 1,
       "a meeting summarises what its claims became "
       "({0} claims, {1} on the board, {2} waiting)".format(
           m["claims"], m["on the board"], m["waiting"]))
    code, det = as_viewer(client, "/api/meeting/dash-fixture", OWNER)
    ids = [c["signal_id"] for c in det["claims"]]
    ok(code == 200 and len(ids) == len(set(ids)) == m["claims"],
       "the detail is ONE row per claim, and agrees with the summary")
    ok(sum(1 for c in det["claims"]
           if c["outcome"] in ("on the board", "done")) == m["on the board"],
       "outcome counts in the summary come from the same rows as the detail")
    code, none = as_viewer(client, "/api/meetings", STRANGER)
    ok(code == 200 and not [x for x in none
                            if x["source_ref"] == "dash-fixture"],
       "a stranger learns nothing about someone else's conversations")

    print("timeline")
    code, tl = as_viewer(client, "/api/timeline", OWNER)
    mine = [i for i in tl["items"] if i["id"] == TASK]
    ok(code == 200 and mine and mine[0]["kind"] == "task"
       and mine[0]["events"],
       "a task appears as a life with its changes on it")
    ok(mine[0]["first_noted"] is not None and mine[0]["said_traced"] is True,
       "first-noted traces back to the conversation, not the row's "
       "created_at")
    claims = [i for i in tl["items"] if i["kind"] == "claim"]
    ok(any(c["id"] == SIGNAL for c in claims),
       "an unconfirmed claim is on the same axis, marked as heard")
    code, theirs = as_viewer(client, "/api/timeline", STRANGER)
    ok(code == 200 and not [i for i in theirs["items"] if i["id"] == TASK]
       and not theirs["items"],
       "a stranger's timeline is empty: it is scoped through visible_tasks "
       "like everything else")
    code, mate = as_viewer(client, "/api/timeline", MATE)
    ok(any(i["id"] == TASK and i["mine"] is False for i in mate["items"]),
       "a grantee sees the shared work on the same axis, flagged as not "
       "theirs")
    # Named against the owner's signals rather than asserting the grantee
    # has no claims at all: an owner correction re-pools a claim FOR this
    # person, so claims of their own are expected here and are not a leak.
    ok(not [i for i in mate["items"] if i["kind"] == "claim"
            and i["id"] in (SIGNAL, SIGNAL_ACCEPTED)],
       "but not the owner's unconfirmed claims: those stay self-only")

    print("measures")
    code, mx = as_viewer(client, "/api/metrics", OWNER)
    ok(code == 200 and mx["tiles"]["traced_tasks"] >= 1
       and mx["tiles"]["median_lag_hours"] is not None,
       "talk-to-tracking latency is computed from the provenance chain "
       "({0}h median over {1} task(s))".format(
           mx["tiles"]["median_lag_hours"], mx["tiles"]["traced_tasks"]))
    ok(any(w["created"] >= 1 for w in mx["confirmed"]),
       "confirmed-per-week counts the board events behind those decisions")
    code, empty = as_viewer(client, "/api/metrics", STRANGER)
    ok(code == 200 and empty["tiles"]["traced_tasks"] == 0
       and empty["confirmed"] == [],
       "measures are scoped through visible_tasks: a stranger charts nothing")

    print("demo picker")
    code, _ = as_viewer(client, "/api/viewers", OWNER)
    ok(code == 404, "identity picker is off by default")
    os.environ["TL_VIEWER_PICKER"] = "1"
    code, v = as_viewer(client, "/api/viewers", OWNER)
    ok(code == 200 and OWNER in v["viewers"],
       "identity picker answers only when explicitly enabled")
    os.environ.pop("TL_VIEWER_PICKER", None)

    print("review popup")
    code, rv = as_viewer(client, "/api/review", OWNER)
    ok(code == 200 and "proposals" in rv and "quiet" in rv,
       "the popup asks both questions: unruled claims and quiet work")
    ok(rv["writable"] is False,
       "on the real ledger it is read-only, so nothing can be decided")
    code, _ = as_viewer(client, "/api/review")
    ok(code == 401, "and it needs an identity like every other endpoint")

    # SELF ONLY: unreviewed claims about a person are not board state, so a
    # full_trace grant over their whole board must not reach them.
    code, mate_rv = as_viewer(client, "/api/review", MATE)
    ok(code == 200 and mate_rv["proposals"] == [],
       "a full_trace grantee sees none of the owner's unruled claims")

    print("write guards")
    r = client.post("/api/decide", json={"proposal_id": "x",
                                         "decision": "accept"},
                    headers={"X-Viewer": OWNER})
    ok(r.status_code == 403,
       "the real ledger refuses writes from the browser outright")
    body = r.get_json()
    ok("read-only" in json.dumps(body),
       "and says why, rather than failing opaquely")
    r = client.post("/api/nudge", json={"task_id": TASK,
                                        "status": "completed"},
                    headers={"X-Viewer": OWNER})
    ok(r.status_code == 403, "same for a status nudge")
    r = client.post("/api/decide", json={"proposal_id": "x",
                                         "decision": "accept"})
    ok(r.status_code == 401, "anonymous writes are refused before anything")

    # With the ledger marked synthetic, the guards that remain are the ones
    # that matter: your own packet, a real decision word, a reason on every
    # rejection.
    dashboard_api.PROFILES["synthetic"] = {
        "dsn": dsn, "synthetic": True, "writable": True,
        "label": "Synthetic demo", "blurb": "invented work", "banner": None}
    try:
        def post(path, payload, who=OWNER):
            r = client.post(path + "?profile=synthetic", json=payload,
                            headers={"X-Viewer": who})
            return r.status_code, r.get_json()

        code, _ = post("/api/decide", {"proposal_id": "nope",
                                       "decision": "accept"})
        ok(code == 404, "an unknown proposal is a 404, not a write")
        code, _ = post("/api/decide", {"proposal_id": "nope",
                                       "decision": "sideways"})
        ok(code == 400, "only accept, reject or defer are decisions")
        code, b = post("/api/decide", {"proposal_id": "prop-dash-open",
                                       "decision": "reject"})
        ok(code == 400 and "reason" in json.dumps(b),
           "a rejection without a reason is refused -- the reason IS the "
           "signal the eval set is built from")
        code, _ = post("/api/decide", {"proposal_id": "prop-dash-open",
                                       "decision": "accept"}, who=MATE)
        ok(code == 403,
           "a full_trace grantee cannot decide on the owner's packet")
        code, _ = post("/api/nudge", {"task_id": TASK,
                                      "status": "completed"}, who=STRANGER)
        ok(code in (403, 404),
           "and cannot type a status onto someone else's task")
        code, _ = post("/api/nudge", {"task_id": TASK, "status": "vibes"})
        ok(code == 400, "a nudge status is a closed set")

        print("auto-applied and undo")
        auto = psycopg2.connect(dsn)
        with auto, auto.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as a1:
            a1.execute("SELECT 1 FROM tasks_current WHERE task_id = %s",
                       (AUTO_TASK,))
            if a1.fetchone() is None:
                a1.execute(
                    "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                    "NULL,%s::jsonb)",
                    (AUTO_TASK, OWNER, json.dumps({
                        "title": "Auto-apply fixture task",
                        "status": "in_progress", "owner_ref": OWNER})))
            a1.execute(
                "SELECT apply_task_event(%s,%s,'status_changed',"
                "'auto_applied',NULL,%s::jsonb)",
                (AUTO_TASK, OWNER, json.dumps({"from_status": "in_progress",
                                               "to_status": "blocked",
                                               "auto_applied": True})))
            a1.execute("""
                SELECT event_id FROM task_events
                WHERE task_id = %s AND actor_type = 'auto_applied'
                ORDER BY event_id DESC LIMIT 1""", (AUTO_TASK,))
            auto_event = a1.fetchone()["event_id"]
        auto.close()
        code, rev3 = as_viewer(client, "/api/review?profile=synthetic", OWNER)
        listed = [a for a in rev3.get("auto_applied", [])
                  if a["event_id"] == auto_event]
        ok(len(listed) == 1 and listed[0]["to_status"] == "blocked",
           "a change nobody approved is shown back to the person it "
           "happened to -- invisible would make the gate indefensible")
        code, _ = post("/api/undo-auto", {"event_id": auto_event}, who=MATE)
        ok(code == 403, "and only they can put it back")
        code, b = post("/api/undo-auto", {"event_id": auto_event})
        ok(code == 200 and b.get("status") == "in_progress",
           "undo restores the status it moved from")
        code, rev4 = as_viewer(client, "/api/review?profile=synthetic", OWNER)
        ok(not [a for a in rev4.get("auto_applied", [])
                if a["event_id"] == auto_event],
           "and it stops being offered once reverted")
        undone = psycopg2.connect(dsn)
        with undone, undone.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as a2:
            a2.execute("""
                SELECT actor_type FROM task_events
                WHERE task_id = %s ORDER BY event_id DESC LIMIT 1""",
                (AUTO_TASK,))
            ok(a2.fetchone()["actor_type"] == "manual_edit",
               "the revert is recorded as a person typing, not as a "
               "deletion -- the automatic change stays in the log")
        undone.close()

        print("candidate register")
        cand = psycopg2.connect(dsn)
        with cand, cand.cursor() as c2:
            for ev, who in (("sighted", "dash-speaker"),
                            ("sighted", "dash-other")):
                c2.execute("""
                    INSERT INTO task_candidate_events
                      (candidate_id, user_ref, event, title, speaker_ref,
                       run_id)
                    VALUES (%s,%s,%s,'Fixture candidate work',%s,
                            'dash-fixture')""", (CAND_ID, OWNER, ev, who))
        cand.close()
        code, rev2 = as_viewer(client, "/api/review?profile=synthetic", OWNER)
        mine = [c for c in rev2.get("candidates", [])
                if c["candidate_id"] == CAND_ID]
        ok(len(mine) == 1 and mine[0]["sightings"] == 2
           and mine[0]["speakers"] == 2,
           "work heard about but tracked nowhere is offered in the poke, "
           "with what earned it a place there")
        code, cand_other = as_viewer(client, "/api/review?profile=synthetic",
                                     MATE)
        ok(not [c for c in cand_other.get("candidates", [])
                if c["candidate_id"] == CAND_ID],
           "and stays self-only -- an unreviewed sighting is not board state")
        code, _ = post("/api/candidate", {"candidate_id": CAND_ID,
                                          "action": "sideways"})
        ok(code == 400, "a candidate ruling is track or dismiss, nothing else")
        code, b = post("/api/candidate", {"candidate_id": CAND_ID,
                                          "action": "dismiss"})
        ok(code == 200 and b.get("action") == "dismiss",
           "dismissing records the answer and changes no board")
        code, _ = post("/api/candidate", {"candidate_id": CAND_ID,
                                          "action": "dismiss"})
        ok(code == 409, "and it is not asked again once ruled on")
        check2 = psycopg2.connect(dsn)
        with check2, check2.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as c3:
            c3.execute("""
                SELECT count(*) AS n FROM tasks_current
                WHERE title = 'Fixture candidate work'""")
            ok(c3.fetchone()["n"] == 0,
               "a dismissal writes no task -- the register is not the board")
        check2.close()

        print("owner correction")
        code, rev = as_viewer(client, "/api/review?profile=synthetic", OWNER)
        attrib = [p for p in rev["proposals"]
                  if p["proposal_id"] == ATTRIB_PROP]
        ok(len(attrib) == 1 and MATE in attrib[0]["owner_options"],
           "the other person the extractor considered is offered as a "
           "correction, so the commonest fix is one click")
        ok(STRANGER not in attrib[0]["owner_options"],
           "and nobody outside the conversation is -- a review control is "
           "not a directory of colleagues")
        code, _ = post("/api/decide", {"proposal_id": ATTRIB_PROP,
                                       "decision": "reject",
                                       "reject_reason": "not_a_task",
                                       "reassign_to": MATE})
        ok(code == 400,
           "a reassignment is a wrong_owner rejection, not any rejection")

        print("uncited claims")
        uncited = [p for p in rev["proposals"]
                   if p["proposal_id"] == UNCITED_PROP]
        ok(len(uncited) == 1 and uncited[0]["traceable"] is False,
           "a card whose only evidence is an assistant's word is marked "
           "untraceable, so the buttons can be dead before they are "
           "clicked rather than after")
        ok(attrib[0]["traceable"] is True,
           "and an anchored card beside it is not -- the distinction is "
           "per card, because an anchored ledger is a MIXED one")
        code, body = post("/api/decide", {"proposal_id": UNCITED_PROP,
                                          "decision": "accept"})
        ok(code == 403 and "trace" in json.dumps(body),
           "accepting it is refused with the reason, not silently allowed "
           "because the profile happened to be writable")
        code, body = post("/api/decide", {"proposal_id": UNCITED_PROP,
                                          "decision": "reject",
                                          "reject_reason": "not_a_task"})
        ok(code == 403,
           "and so is rejecting it -- 'no' is a judgement too, and it "
           "trains the eval, so it needs something real to be about")
        check3 = psycopg2.connect(dsn)
        with check3.cursor() as c4:
            c4.execute("SELECT count(*) FROM decisions WHERE proposal_id = %s",
                       (UNCITED_PROP,))
            ok(c4.fetchone()[0] == 0,
               "and nothing was recorded either way: a refused card leaves "
               "no trace of a decision nobody was allowed to make")
        check3.close()
        code, _ = post("/api/decide", {"proposal_id": ATTRIB_PROP,
                                       "decision": "reject",
                                       "reject_reason": "wrong_owner",
                                       "reassign_to": STRANGER})
        ok(code == 400,
           "and it cannot put work on someone who was never in the room")
        code, _ = post("/api/decide", {"proposal_id": ATTRIB_PROP,
                                       "decision": "reject",
                                       "reject_reason": "wrong_owner",
                                       "reassign_to": OWNER})
        ok(code == 400, "reassigning to yourself is an accept, not a miss")
        code, b = post("/api/decide", {"proposal_id": ATTRIB_PROP,
                                       "decision": "reject",
                                       "reject_reason": "wrong_owner",
                                       "reassign_to": MATE})
        ok(code == 200 and b.get("reassigned_to") == MATE
           and b.get("signals_repooled") == 1,
           "the correction lands: rejected as wrong_owner, and the claim "
           "re-pooled for the person it belonged to")
        check = psycopg2.connect(dsn)
        with check, check.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT decision, reject_reason FROM decisions
                WHERE proposal_id = %s""", (ATTRIB_PROP,))
            d = cur.fetchone()
            ok(d and d["decision"] == "reject"
               and d["reject_reason"] == "wrong_owner",
               "recorded as the same reject/wrong_owner row the attribution "
               "eval selects on, so a correction counts as a miss")
            cur.execute("""
                SELECT event FROM signal_pool_events
                WHERE signal_id = %s AND user_ref = %s
                ORDER BY event_id DESC LIMIT 1""", (ATTRIB_SIGNAL, MATE))
            pooled = cur.fetchone()
            ok(pooled and pooled["event"] == "pooled",
               "and the signal is pooled FOR them, so it reaches them as a "
               "proposal in their own packet")
            cur.execute("""
                SELECT count(*) AS n FROM task_events
                WHERE user_ref = %s""", (MATE,))
            ok(cur.fetchone()["n"] == 0,
               "while their board stays untouched -- one person's review "
               "never writes to another person's board")
        check.close()
    finally:
        dashboard_api.PROFILES.pop("synthetic", None)

    print("unverifiable ledger")
    # A third profile that is NOT synthetic and NOT writable: real content
    # from an assistant, with no retrievable source behind it.
    dashboard_api.PROFILES["copilot"] = {
        "dsn": dsn, "synthetic": False, "writable": False,
        "label": "Copilot claims", "blurb": "asserted, not verifiable",
        "banner": {"tag": "Unverified claims", "text": "no source released"}}
    try:
        code, prof = as_viewer(client, "/api/profiles", OWNER)
        names = [p["name"] for p in prof["profiles"]]
        ok(names == ["real", "copilot"],
           "a third ledger is offered alongside the others")
        cop = [p for p in prof["profiles"] if p["name"] == "copilot"][0]
        ok(cop["banner"]["tag"] == "Unverified claims",
           "each ledger carries its own caution, not a shared one")
        ok(cop["synthetic"] is False and cop["writable"] is False,
           "real-but-unverifiable is a different thing from invented: it "
           "gets a warning AND stays read-only")
        r = client.post("/api/decide?profile=copilot",
                        json={"proposal_id": "x", "decision": "accept"},
                        headers={"X-Viewer": OWNER})
        ok(r.status_code == 403,
           "writability follows the flag, not the word 'synthetic' -- a "
           "ledger of real claims is not writable from a browser")
    finally:
        dashboard_api.PROFILES.pop("copilot", None)

    print("identity picker")
    # Someone with a packet and NO board: the shape a ledger imported from
    # an assistant's claims has for every person in it until a human agrees
    # to something.
    import psycopg2 as _pg
    _c = _pg.connect(dsn)
    with _c.cursor() as _cur:
        _cur.execute("""
            INSERT INTO review_packets (packet_id, user_ref, cadence_run_id,
              board_version, generated_at, expires_at)
            VALUES ('packet-dash-boardless',%s,'dash-fixture',1, now(),
                    now() + interval '2 days')
            ON CONFLICT (packet_id) DO NOTHING""", (PACKET_USER,))
    _c.commit()
    _c.close()
    os.environ["TL_VIEWER_PICKER"] = "1"
    code, v = as_viewer(client, "/api/viewers", OWNER)
    ok(code == 200 and OWNER in v["viewers"],
       "board owners are listed")
    ok(PACKET_USER in v["viewers"],
       "so is anyone with a packet waiting but no board yet -- a ledger "
       "imported from an assistant has no board until someone agrees, and "
       "an owners-only list would make its review queue unreachable")
    os.environ.pop("TL_VIEWER_PICKER", None)

    print("profile switch")
    code, prof = as_viewer(client, "/api/profiles", OWNER)
    ok(code == 200 and prof["switchable"] is False
       and [p["name"] for p in prof["profiles"]] == ["real"],
       "one ledger configured: nothing to switch, and the UI is told so")
    ok("dbname=" not in json.dumps(prof),
       "the profile list never ships a connection string")

    # Point a second profile at the SAME database: what is under test is
    # the routing and the guards, not two sets of rows.
    dashboard_api.PROFILES["synthetic"] = {
        "dsn": dsn, "synthetic": True, "writable": True,
        "label": "Synthetic demo", "blurb": "invented work", "banner": None}
    try:
        code, prof = as_viewer(client, "/api/profiles", OWNER)
        ok(code == 200 and prof["switchable"] is True
           and [p["name"] for p in prof["profiles"]] == ["real", "synthetic"],
           "with two configured, both are offered")
        ok(prof["active"] == "real",
           "and the default is active until something asks otherwise")
        code, prof = as_viewer(client, "/api/profiles?profile=synthetic",
                               OWNER)
        ok(prof["active"] == "synthetic",
           "asking for a profile by name selects it")

        # The whole risk of this feature: a client that can name a DSN can
        # point the dashboard at any database the host can reach.
        for bogus in ("host=evil dbname=secret", "../../etc", "REAL",
                      "synthetic; drop table tasks_current", ""):
            code, prof = as_viewer(
                client, "/api/profiles?profile=" + bogus, OWNER)
            ok(code == 200 and prof["active"] == "real",
               "an unrecognised profile falls back to the default: "
               + (repr(bogus)[:34]))

        code, board = as_viewer(
            client, "/api/board/" + OWNER + "?profile=synthetic", STRANGER)
        ok(code in (403, 404) or not (board or {}).get("tasks"),
           "a profile is not a way around visible_tasks: a stranger still "
           "sees nothing")
        code, pend = as_viewer(
            client, "/api/pending?profile=synthetic", MATE)
        ok(code == 200 and pend["undecided"] == 0 and pend["packets"] == [],
           "SELF-ONLY endpoints stay self-only under an explicit profile")
    finally:
        dashboard_api.PROFILES.pop("synthetic", None)


if __name__ == "__main__":
    main()
    print("\nall {0} dashboard checks passed".format(CHECKS[0]))
