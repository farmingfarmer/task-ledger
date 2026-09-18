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
SERIES_PROP = "prop-dash-series-{0}".format(int(time.time() * 1000))
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
                       OR p.proposal_id LIKE 'prop-dash-uncited-%%'
                       OR p.proposal_id LIKE 'prop-dash-series-%%')
                  AND p.proposal_id NOT IN (%s, %s, %s)
                  AND d.decision_id IS NULL
                ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                (OWNER, ATTRIB_PROP, UNCITED_PROP, SERIES_PROP))
            # Restore the packet landscape. The capture test reconciles
            # inline, which SUPERSEDES the fixture packet and leaves its
            # own as the owner's newest open one -- so the next run's
            # pending assertions would count the wrong packet. Append-
            # only surgery, same semantics the orchestrator uses: every
            # stray open packet is superseded, and the fixture packet is
            # re-delivered (latest state event wins).
            cur.execute("""
                INSERT INTO packet_state_events (packet_id, state)
                SELECT rp.packet_id, 'superseded'
                FROM review_packets rp
                JOIN (SELECT DISTINCT ON (packet_id) packet_id, state
                      FROM packet_state_events
                      ORDER BY packet_id, event_id DESC) le
                  USING (packet_id)
                WHERE rp.user_ref = %s AND rp.packet_id <> %s
                  AND le.state IN ('draft','delivered',
                                   'partially_decided')""",
                (OWNER, PACKET))
            cur.execute("""
                INSERT INTO packet_state_events (packet_id, state)
                SELECT %s, 'delivered'
                WHERE (SELECT state FROM packet_state_events
                       WHERE packet_id = %s
                       ORDER BY event_id DESC LIMIT 1)
                      NOT IN ('draft','delivered','partially_decided')""",
                (PACKET, PACKET))
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
    # The suite asserts DEFAULTS -- picker off, real ledger read-only, no
    # blind, no kit, no second ledger -- and dashboard_api reads all of
    # those from the environment at import. An operator's shell is the
    # wrong place for a test's assumptions to come from: the first
    # Windows run inherited TL_HANDS_ON=1 from the session that had just
    # been driving the dashboard, and the read-only assertion failed on a
    # machine where the code was fine. Every mode flag is cleared here,
    # not in the launcher, so a direct `python test_dashboard.py` is
    # deterministic too. Tests that want a mode set module state
    # explicitly and restore it.
    for flag in ("TL_VIEWER_PICKER", "TL_HANDS_ON", "TL_BLIND",
                 "TL_BLIND_SALT", "TL_KIT_CONFIG", "TL_COPILOT_DSN",
                 "TL_SYNTH_DSN", "TL_SURVEY_DIR"):
        os.environ.pop(flag, None)
    # ...and the copilot autodiscovery, which would otherwise adopt any
    # taskintel_copilot living on the developer's server and quietly
    # change which profiles these assertions run against.
    os.environ["TL_DISABLE_COPILOT_PROBE"] = "1"
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
    # Deliberately NOT an equality on n. Decisions are append-only and
    # the drift buckets by week, so "exactly 2 at rate 0.5" only ever
    # held on a database whose first week was already frozen -- on the
    # Mac the assertion spent weeks reading a stale early bucket while
    # every later run's leftovers piled into rows it never looked at,
    # and the first FRESH database (the ThinkPad's, day one) put
    # everything in one bucket and failed. What the endpoint must
    # guarantee is the maths, not the history: the seeded accept and the
    # seeded reject are both counted, and every weekly rate is the
    # accept fraction of that week's n.
    total = sum(d["n"] for d in creates)
    accepted = sum(round(float(d["accept_rate"]) * d["n"]) for d in creates)
    ok(creates and total >= 2 and accepted >= 1 and total - accepted >= 1,
       "acceptance rate counts both kinds of decision on create_task: "
       "the seeded accept and the seeded reject are both in the "
       "histogram, whatever else history has accumulated")
    ok(all(0.0 <= float(d["accept_rate"]) <= 1.0 and d["n"] >= 1
           for d in creates),
       "and every weekly rate is a fraction of that week's own n")
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
    # >= 3, not == 3: a claim is "this meeting's" once it has ever been
    # pooled to the viewer, and superseding a packet legitimately
    # RETURNS its undecided signals to the pool -- so any test that
    # triggers a supersede (the capture test does, by design) adds real
    # pool events to this meeting forever. The invariant is that the
    # three canonical fixture claims are among them, not that history
    # stopped.
    ok(m["claims"] >= 3 and m["on the board"] >= 1 and m["waiting"] >= 1,
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
        # The uncited claim names OWNER only and carries no speaker --
        # but it shares a batch with signals attributing work to MATE,
        # and batch-mates are in the same conversation. On an assistant
        # ledger only anchored claims carry speakers, so without this
        # widening the one person whose quotes happened to match was the
        # only correction offered on every card in the export.
        uncited_probe = [p for p in rev["proposals"]
                         if p["proposal_id"] == UNCITED_PROP]
        ok(uncited_probe and MATE in uncited_probe[0]["owner_options"],
           "a claim with no recorded speaker still offers the people its "
           "own batch attributed work to -- the export's conversation, "
           "not one lucky anchor")
        # The room is the MEETING, not the batch. Extraction splits a
        # meeting into 8-utterance sub-windows (…-p1, …-p2) for model
        # quality, and that split silently became the room boundary: a
        # correction dropdown answered "who was in this conversation"
        # from a three-minute slice. Someone who spoke only in another
        # slice of the same meeting must still be offered.
        conn_s = psycopg2.connect(dsn)
        with conn_s, conn_s.cursor() as cs:
            cs.execute("""
                INSERT INTO extraction_batches (batch_id, extracted_at,
                  extractor_model, prompt_version, source_type, source_ref,
                  window_start, window_end)
                VALUES ('b-ser-p1', now(), 't', 'v1', 'meeting_transcript',
                        'teams-2026-09-02-series-mtg-p1', now(), now()),
                       ('b-ser-p2', now(), 't', 'v1', 'meeting_transcript',
                        'teams-2026-09-02-series-mtg-p2', now(), now())
                ON CONFLICT DO NOTHING""")
            cs.execute("""
                INSERT INTO signals (signal_id, batch_id, signal_type,
                  title, confidence, author_ref, owner_candidates, raw)
                VALUES ('sig-ser-1', 'b-ser-p1', 'assignment',
                        'Series room fixture', 0.7, %s, '[]', '{}'),
                       ('sig-ser-2', 'b-ser-p2', 'status_update',
                        'Other slice of the meeting', 0.7,
                        'aad-other-slice-speaker', '[]', '{}')
                ON CONFLICT DO NOTHING""", (OWNER,))
            cs.execute("""
                INSERT INTO proposals (proposal_id, packet_id, op,
                  rationale, confidence, payload)
                VALUES (%s, %s, 'create_task', 'r', 0.7,
                        '{"kind":"create_task",
                          "title":"Series room fixture",
                          "status":"proposed"}')
                ON CONFLICT DO NOTHING""", (SERIES_PROP, PACKET,))
            cs.execute("""
                INSERT INTO proposal_signals (proposal_id, signal_id)
                VALUES (%s, 'sig-ser-1')
                ON CONFLICT DO NOTHING""", (SERIES_PROP,))
        conn_s.close()
        code, rev_s = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        series_probe = [p for p in rev_s["proposals"]
                        if p["proposal_id"] == SERIES_PROP]
        ok(series_probe and "aad-other-slice-speaker"
           in series_probe[0]["owner_options"],
           "someone who spoke only in ANOTHER sub-window of the same "
           "meeting is offered: the room is the meeting, not the "
           "8-utterance slice the extractor happened to cut")
        ok(STRANGER not in series_probe[0]["owner_options"],
           "and the meeting boundary still holds against strangers")
        conn_s = psycopg2.connect(dsn)
        with conn_s, conn_s.cursor() as cs:
            cs.execute("""
                INSERT INTO decisions (proposal_id, packet_id, decision,
                  reject_reason, decided_by, decided_at)
                VALUES (%s, %s, 'reject', 'stale', %s, now())
                ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                (SERIES_PROP, PACKET, OWNER))
        conn_s.close()
        code, _ = post("/api/decide", {"proposal_id": ATTRIB_PROP,
                                       "decision": "reject",
                                       "reject_reason": "not_a_task",
                                       "reassign_to": MATE})
        ok(code == 400,
           "a reassignment is a wrong_owner rejection, not any rejection")

        print("blind comparison")
        saved_profiles = dict(dashboard_api.PROFILES)
        saved_blind = dashboard_api.BLIND
        dashboard_api.PROFILES["copilot"] = dict(
            dashboard_api.PROFILES["real"], label="Copilot claims",
            blurb="b", banner={"tag": "Unverified", "text": "cannot be traced"})
        dashboard_api.BLIND = True
        code, prof = as_viewer(client, "/api/profiles", OWNER)
        names = [p["name"] for p in prof["profiles"]]
        labels = [p["label"] for p in prof["profiles"]]
        ok(code == 200 and names == ["arm-a", "arm-b"]
           and labels == ["Ledger A", "Ledger B"],
           "the ledgers are two identically-described arms -- 'Real ledger' "
           "and 'Copilot claims' are answers, and a comparison whose arms "
           "carry the answer is not one")
        blob = json.dumps(prof)
        ok("Copilot" not in blob and "cannot be traced" not in blob,
           "and neither the names nor the banners leak: the banner that "
           "explained what was wrong with one arm was the loudest tell of all")
        ok(all(not p["synthetic"] for p in prof["profiles"])
           and len(prof["profiles"]) == 2,
           "the synthetic demo is not an arm and is dropped entirely -- a "
           "third option that obviously differs is a hint about the other two")

        mapping = dashboard_api.blind_map(OWNER)
        ok(sorted(mapping.keys()) == ["arm-a", "arm-b"]
           and sorted(mapping.values()) == ["copilot", "real"],
           "each arm resolves to exactly one real ledger")
        ok(dashboard_api.blind_map(OWNER) == mapping,
           "stable for one person across requests, or the ledger would change "
           "under them mid-review")
        flips = set()
        for who in ("a", "b", "c", "d", "e", "f", "g", "h"):
            flips.add(dashboard_api.blind_map(who)["arm-a"])
        ok(flips == {"real", "copilot"},
           "and it is not the same assignment for everybody, so order effects "
           "do not all land on the same ledger")

        with dashboard_api.app.test_request_context(
                "/api/x?profile=copilot", headers={"X-Viewer": OWNER}):
            ok(dashboard_api.profile_name() == mapping["arm-a"],
               "asking for a ledger BY NAME while blinded does not get it -- "
               "it resolves to that person's arm A, because falling back to "
               "the default would quietly mean 'real' and hand the answer to "
               "anyone who guessed")
        with dashboard_api.app.test_request_context(
                "/api/x?profile=arm-b", headers={"X-Viewer": OWNER}):
            ok(dashboard_api.profile_name() == mapping["arm-b"],
               "and the arm name resolves to the ledger behind it")

        print("survey")
        import tempfile
        saved_dir = dashboard_api.SURVEY_DIR
        dashboard_api.SURVEY_DIR = tempfile.mkdtemp()
        code, spec = as_viewer(client, "/api/survey?profile=arm-a", OWNER)
        ids = [q["id"] for q in spec["questions"]]
        ok(code == 200 and "worst" in ids and "which_better" not in ids,
           "the first arm is asked about itself; the comparison question is "
           "not asked of somebody who has only seen one side")
        def spost(path, payload):
            r = client.post(path, json=payload, headers={"X-Viewer": OWNER})
            return r.status_code, r.get_json()

        code, body = spost("/api/survey?profile=arm-a", {"answers": {}})
        ok(code == 400, "an empty form is not a submission")
        code, body = spost("/api/survey?profile=arm-a",
                           {"answers": {"recognised": "4",
                                        "worst": "it thought sharing a screen "
                                                 "was a task"}})
        ok(code == 200 and body.get("file", "").endswith(".txt"),
           "answers are written to a file the person can send on")
        written = os.listdir(dashboard_api.SURVEY_DIR)
        ok(any(f.endswith(".json") for f in written)
           and any(f.endswith(".txt") for f in written),
           "as JSON to read with a script and text to read with your eyes")
        with open(os.path.join(dashboard_api.SURVEY_DIR,
                               [f for f in written if f.endswith(".json")][0])) as f:
            doc = json.load(f)
        ok(doc["ledger"] == mapping["arm-a"] and doc["arm"] == "arm-a",
           "and the file records WHICH ledger it was: the participant stays "
           "blind, the person reading the answers must not be")
        ok(isinstance(doc.get("decisions"), dict)
           and "accept" in doc["decisions"],
           "with what they actually did beside what they say about it -- "
           "self-report and behaviour disagree often enough that collecting "
           "one without the other wastes the session")
        code, spec2 = as_viewer(client, "/api/survey?profile=arm-b", OWNER)
        ok("which_better" in [q["id"] for q in spec2["questions"]],
           "the second arm asks the comparison, which is the whole reason "
           "for doing both")
        code, spec3 = as_viewer(client, "/api/survey?profile=arm-a", OWNER)
        ok(spec3["submitted"] is True,
           "and the form does not re-ambush somebody who already answered it")

        dashboard_api.SURVEY_DIR = saved_dir
        dashboard_api.BLIND = saved_blind
        dashboard_api.PROFILES.clear()
        dashboard_api.PROFILES.update(saved_profiles)

        print("personal kit")
        saved_kit = dashboard_api.KIT
        dashboard_api.KIT = {"for": OWNER, "for_name": "Dash Owner",
                             "return_to": "Alex (a@example.com)"}
        code, ver = as_viewer(client, "/api/version", OWNER)
        ok(code == 200 and ver["kit"]["for"] == OWNER
           and ver["kit"]["for_name"] == "Dash Owner"
           and ver["kit"]["return_to"] == "Alex (a@example.com)"
           and ver["kit"]["answers_dir"],
           "a personal kit tells the page who it is for, by name, where "
           "the answers land, and who they go back to -- the three facts "
           "the greeting, the identity lock and the final screen are "
           "built from")
        dashboard_api.KIT = None
        code, ver = as_viewer(client, "/api/version", OWNER)
        ok(ver["kit"] is None,
           "and without a kit config the page gets nothing, so a generic "
           "kit keeps the picker and the generic welcome")
        dashboard_api.KIT = saved_kit

        print("the blocker moved")
        # The most connective fact the ledger holds: blocking_task_id
        # can point at a task on somebody ELSE's board, and until now
        # nothing watched the edge -- the other person finished, and
        # the blocked person found out at standup, or never.
        UB = str(int(time.time() * 1000))
        # A dedicated colleague ref, not MATE: an invariant elsewhere
        # asserts the review flow writes zero events to MATE's board,
        # and this fixture legitimately seeds a board for the person
        # who owns the blocking task.
        UBMATE = "ub-partner"
        bconn = psycopg2.connect(dsn)
        with bconn, bconn.cursor() as bc:
            bc.execute("""
                SELECT apply_task_event(%s, %s, 'created', 'manual_edit',
                  NULL, %s::jsonb)""",
                ("task-ub-theirs-" + UB, UBMATE, json.dumps(
                    {"title": "Provision the shared bucket",
                     "status": "in_progress", "owner_ref": UBMATE})))
            bc.execute("""
                SELECT apply_task_event(%s, %s, 'created', 'manual_edit',
                  NULL, %s::jsonb)""",
                ("task-ub-mine-" + UB, OWNER, json.dumps(
                    {"title": "Load the cohort into the bucket",
                     "status": "in_progress", "owner_ref": OWNER})))
            bc.execute("""
                SELECT apply_task_event(%s, %s, 'blocker_added',
                  'manual_edit', NULL, %s::jsonb)""",
                ("task-ub-mine-" + UB, OWNER, json.dumps(
                    {"description": "needs the bucket to exist",
                     "blocking_task_id": "task-ub-theirs-" + UB})))
        bconn.close()
        code, rev_u = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        ok(not [u for u in rev_u.get("unblocked", [])
                if u["task_id"] == "task-ub-mine-" + UB],
           "while the other person's task is still open there is no "
           "card: nothing has moved")
        bconn = psycopg2.connect(dsn)
        with bconn, bconn.cursor() as bc:
            bc.execute("""
                SELECT apply_task_event(%s, %s, 'completed',
                  'manual_edit', NULL, '{}'::jsonb)""",
                ("task-ub-theirs-" + UB, UBMATE))
        bconn.close()
        code, rev_u = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        card = [u for u in rev_u.get("unblocked", [])
                if u["task_id"] == "task-ub-mine-" + UB]
        ok(len(card) == 1
           and card[0]["blocking_title"] == "Provision the shared bucket"
           and card[0]["closed_by"] == UBMATE,
           "the moment they finish, the blocked person gets a card "
           "naming what finished, who finished it, and when")
        code, body = spost("/api/unblocked?profile=synthetic",
                           {"blocker_id": card[0]["blocker_id"],
                            "answer": "moving"})
        ok(code == 200,
           "answering 'moving again' is accepted")
        bconn = psycopg2.connect(dsn)
        with bconn, bconn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as bc:
            bc.execute("SELECT status FROM tasks_current WHERE task_id=%s",
                       ("task-ub-mine-" + UB,))
            ok(bc.fetchone()["status"] == "in_progress",
               "and it resolves through the same blocker_resolved event "
               "as every other path, so status is recomputed rather "
               "than hand-set")
        bconn.close()
        code, rev_u = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        ok(not [u for u in rev_u.get("unblocked", [])
                if u["task_id"] == "task-ub-mine-" + UB],
           "and the card retires itself once answered")
        code, _ = spost("/api/unblocked?profile=synthetic",
                        {"blocker_id": card[0]["blocker_id"],
                         "answer": "moving"})
        ok(code == 404,
           "a resolved blocker cannot be answered twice")
        code, _ = spost("/api/unblocked?profile=synthetic",
                        {"blocker_id": 999999999, "answer": "still"})
        ok(code == 404, "and an unknown blocker is a 404, not a write")

        print("workload sheet")
        # The sheet the operator keeps by hand, generated: priority,
        # cumulative hours, this week's plan, end-of-week projection.
        # The rule under test: three kinds of number -- stated, measured,
        # proposed -- never blended, and nothing becomes fact unsaved.
        wconn = psycopg2.connect(dsn)
        with wconn, wconn.cursor() as wc:
            wc.execute("SELECT 1 FROM tasks_current WHERE task_id = "
                       "'task-wk-blocked'")
            if wc.fetchone() is None:
                wc.execute("""
                    SELECT apply_task_event('task-wk-blocked', %s,
                      'created', 'manual_edit', NULL,
                      '{"title":"Blocked project",
                        "status":"in_progress",
                        "owner_ref":"dash-owner"}')""", (OWNER,))
            wc.execute("""
                INSERT INTO task_blockers (task_id, description,
                  opened_event_id)
                SELECT 'task-wk-blocked', 'Waiting on business feedback',
                       min(event_id) FROM task_events
                WHERE task_id = 'task-wk-blocked'
                HAVING min(event_id) IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM task_blockers
                                   WHERE task_id = 'task-wk-blocked')""")
        wconn.close()
        # A run-unique week for every stated-number assertion. Stated
        # rows persist per (subject, week) forever, and priority drift
        # across runs changes which row a run's save lands on -- so on
        # an aged database the "untouched rows stay proposals" check
        # kept meeting statements left by earlier generations. A week
        # nobody has ever stated into is a clean sheet by construction.
        import datetime as _dt
        WKN = (_dt.date(2030, 1, 7)
               + _dt.timedelta(weeks=int(time.time()) % 4000))
        WKN = str(WKN - _dt.timedelta(days=WKN.weekday()))
        code, sheet = as_viewer(client,
                                "/api/workload?profile=synthetic&week="
                                + WKN, OWNER)
        ok(code == 200 and sheet["rows"],
           "the sheet exists, one row per project or parent")
        blocked = [r for r in sheet["rows"]
                   if r["subject"] == "task-wk-blocked"]
        ok(blocked and blocked[0]["blocker"]
           and blocked[0]["planned"]["value"] == 0.0
           and blocked[0]["planned"]["source"] == "proposed",
           "a blocked row is proposed ZERO hours -- planning time into "
           "work that cannot move is how a sheet starts lying -- and the "
           "zero is labeled a proposal, not a fact")
        ok(all(r["planned"]["source"] in ("proposed", "stated")
               for r in sheet["rows"]),
           "every planned cell declares which kind of number it is")

        sid = sheet["rows"][0]["subject"]
        code, body = spost("/api/workload?profile=synthetic",
                           {"week_start": WKN,
                            "capacity": 38,
                            "rows": [{"subject": sid, "opening_total": 20,
                                      "planned": 9.5, "priority": 1}]})
        ok(code == 200 and body["saved"] == 4,
           "stating capacity, an opening total, a plan and a priority "
           "writes four events")
        code, sheet2 = as_viewer(client,
                                 "/api/workload?profile=synthetic&week="
                                 + WKN, OWNER)
        row = [r for r in sheet2["rows"] if r["subject"] == sid][0]
        ok(row["opening_total"]["source"] == "stated"
           and row["planned"]["source"] == "stated"
           and row["priority"]["source"] == "stated"
           and abs(row["eow_total"] - 29.5) < 1e-9,
           "saved numbers come back as STATED and the projection is "
           "arithmetic on them: 20 opening + 9.5 planned = 29.5 by "
           "end of week, exactly the hand-kept sheet's math")
        ok(sheet2["capacity"]["value"] == 38.0,
           "and capacity is the person's own number")
        untouched = [r for r in sheet2["rows"] if r["subject"] != sid
                     and not r["blocker"]]
        ok(all(r["planned"]["source"] == "proposed" for r in untouched),
           "rows the person did not touch stay proposals -- saving one "
           "row does not silently adopt the rest")
        code, _ = spost("/api/workload?profile=synthetic",
                        {"week_start": WKN,
                         "rows": [{"subject": "task-of-somebody-else",
                                   "planned": 4}]})
        ok(code == 400,
           "a subject not on your sheet is refused -- the sheet is "
           "self-only in both directions")
        code, _ = as_viewer(client, "/api/workload?profile=synthetic")
        ok(code == 401, "and anonymous gets nothing")
        # One CURRENT-week statement too: the team-week test asserts the
        # grantee sees a 9.5 h plan, and the shared view reads the
        # current week by design.
        code, _ = spost("/api/workload?profile=synthetic",
                        {"week_start": sheet["week_start"],
                         "rows": [{"subject": sid, "planned": 9.5}]})
        ok(code == 200, "a current-week plan saves too")

        print("friction killers")
        # standup: the board turned into words, receipts attached
        code, su = as_viewer(client, "/api/standup?profile=synthetic&"
                             "days=7", OWNER)
        ok(code == 200 and "Finished" in su["text"]
           or code == 200 and su["text"],
           "the standup drafts from confirmed board events only")
        ok(code == 200 and ("typed by you" in su["text"]
                            or "accepted by you" in su["text"]
                            or "Nothing recorded" in su["text"]),
           "and every line says how it was recorded -- accepted, typed, "
           "or auto-applied -- because a draft with unmarked provenance "
           "would launder guesses into a meeting")
        code, _ = as_viewer(client, "/api/standup?profile=synthetic")
        ok(code == 401, "standup is self-only: it is YOUR words")

        # quick capture: one line into the same pool as a transcript
        code, body = spost("/api/note?profile=synthetic",
                           {"text": "waiting on IT for VPN access",
                            "kind": "blocker"})
        ok(code == 200, "a typed line is captured")
        nconn = psycopg2.connect(dsn)
        with nconn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as nc:
            nc.execute("""
                SELECT s.signal_type, s.author_ref, s.confidence,
                       b.source_type, b.extractor_model, ev.source_type
                         AS ev_type, pe.user_ref AS pooled_for
                FROM signals s
                JOIN extraction_batches b USING (batch_id)
                JOIN evidence_refs ev USING (signal_id)
                JOIN signal_pool_events pe USING (signal_id)
                WHERE s.title = 'waiting on IT for VPN access'
                ORDER BY pe.event_id DESC LIMIT 1""")
            row = nc.fetchone()
        nconn.close()
        ok(row and row["signal_type"] == "blocker"
           and row["author_ref"] == OWNER
           and row["pooled_for"] == OWNER,
           "it lands in the POOL, typed by and pooled for its author -- "
           "a review card at the next tick, never a board row: the "
           "person still answers it like anything else")
        ok(row["source_type"] == "manual_note"
           and row["ev_type"] == "manual_note"
           and row["extractor_model"] == "human:typed",
           "and the provenance says exactly what it is -- a typed note, "
           "extracted by nobody -- because labeling it a document would "
           "be a lie in the one rail that must never lie")
        code, _ = spost("/api/note?profile=synthetic",
                        {"text": "x" * 301, "kind": "work"})
        ok(code == 400, "a note is one line, not a pasted transcript")

        # radar: deadlines ranked by worry
        rconn = psycopg2.connect(dsn)
        with rconn, rconn.cursor() as rc:
            rc.execute("SELECT status FROM tasks_current WHERE "
                       "task_id = 'task-radar-over'")
            rrow = rc.fetchone()
            if rrow is None:
                rc.execute("""
                    SELECT apply_task_event('task-radar-over', %s,
                      'created', 'manual_edit', NULL, %s::jsonb)""",
                    (OWNER, json.dumps(
                        {"title": "Radar overdue fixture",
                         "status": "in_progress", "owner_ref": OWNER,
                         "deadline": "2026-01-05"})))
            elif rrow[0] in ("completed", "cancelled"):
                # an earlier generation of the cancel test closed it;
                # the radar only watches OPEN work, so reopen -- the
                # fixture owes the state, not merely the row
                rc.execute("""
                    SELECT apply_task_event('task-radar-over', %s,
                      'status_changed', 'manual_edit', NULL,
                      %s::jsonb)""",
                    (OWNER, json.dumps({"to_status": "in_progress"})))
        rconn.close()
        code, rad = as_viewer(client, "/api/radar?profile=synthetic",
                              OWNER)
        over = [x for x in rad["items"]
                if x["task_id"] == "task-radar-over"]
        ok(code == 200 and over and over[0]["bucket"] == "overdue",
           "a past deadline on open work is OVERDUE, front of the strip")

        print("also on another board")
        DPX = str(int(time.time() * 1000))
        dconn = psycopg2.connect(dsn)
        with dconn, dconn.cursor() as dc:
            # Defensive revoke FIRST: a prior run that died mid-block
            # leaves its grant active (grants are append-only events),
            # and the no-grant assertion below needs the world it
            # asserts about. End-of-block cleanup alone is not cleanup
            # -- the run that needs it most is the one that never got
            # there.
            dc.execute("SELECT revoke_access(%s,%s,'user',%s)",
                       ("dup-partner", OWNER, "dup-partner"))
            # DPX in the title: leaked pairs from a run that died
            # mid-block stay open forever (append-only), and identical
            # titles across generations would cross-match. Differing
            # case and spacing between the two spellings is the point:
            # the normalization must bridge them.
            for tid, who, title in (
                    ("task-dup-a-" + DPX, OWNER,
                     "Refresh the  Intake DASHBOARDS " + DPX),
                    ("task-dup-b-" + DPX, "dup-partner",
                     "refresh the intake dashboards " + DPX)):
                dc.execute("""
                    SELECT apply_task_event(%s, %s, 'created',
                      'manual_edit', NULL, %s::jsonb)""",
                    (tid, who, json.dumps(
                        {"title": title, "status": "in_progress",
                         "owner_ref": who})))
        dconn.close()
        code, rev_d = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        ok(not [x for x in rev_d.get("duplicates", [])
                if x["task_id"] == "task-dup-a-" + DPX],
           "with no grant, the pair is invisible: discovering a "
           "duplicate must not become a way to discover a board you "
           "were never shown")
        dconn = psycopg2.connect(dsn)
        with dconn, dconn.cursor() as dc:
            dc.execute("SELECT grant_access(%s,%s,'user',%s,'progress',"
                       "'test',NULL)", ("dup-partner", OWNER,
                                        "dup-partner"))
        dconn.close()
        code, rev_d = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        pair = [x for x in rev_d.get("duplicates", [])
                if x["task_id"] == "task-dup-a-" + DPX]
        ok(len(pair) == 1
           and pair[0]["other_owner"] == "dup-partner",
           "once the other board is shared, both same-titled open tasks "
           "surface as one question -- whitespace and case differences "
           "do not hide the match")
        code, _ = spost("/api/duplicate?profile=synthetic",
                        {"task_id": "task-dup-a-" + DPX,
                         "other_task_id": "task-dup-b-" + DPX,
                         "ruling": "distinct"})
        ok(code == 200, "either owner may rule")
        code, rev_d = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        ok(not [x for x in rev_d.get("duplicates", [])
                if x["task_id"] == "task-dup-a-" + DPX],
           "and one ruling settles it for both, permanently -- the "
           "card never re-asks")
        code, _ = spost("/api/duplicate?profile=synthetic",
                        {"task_id": "task-ub-theirs-" + UB,
                         "other_task_id": "task-dup-b-" + DPX,
                         "ruling": "same"})
        ok(code == 403,
           "but ruling on a pair where neither task is yours is "
           "refused: bystanders do not settle other people's "
           "duplicates")
        # Revoke the board grant: grants persist (append-only events),
        # and the next run's "no grant, invisible" assertion needs the
        # world it asserts about.
        dconn = psycopg2.connect(dsn)
        with dconn, dconn.cursor() as dc:
            dc.execute("SELECT revoke_access(%s,%s,'user',%s)",
                       ("dup-partner", OWNER, "dup-partner"))
        dconn.close()

        print("the team week")
        code, tw = as_viewer(client, "/api/teamweek?profile=synthetic",
                             OWNER)
        ok(code == 200 and tw["shared"] == [],
           "no workload grants, no shared sheets -- and a 'progress' "
           "board grant did NOT leak hours, because hours are their own "
           "level")
        code, body = spost("/api/workload/share?profile=synthetic",
                           {"to": "dup-partner"})
        ok(code == 200,
           "sharing my week is one call, and grant_access enforces "
           "that only I can share mine")
        # the OTHER person now sees mine:
        r = client.get("/api/teamweek?profile=synthetic",
                       headers={"X-Viewer": "dup-partner"})
        tw2 = r.get_json()
        ok(any(s["person"] == OWNER for s in tw2["shared"]),
           "and the grantee sees my sheet in their team view")
        mine_shared = [s for s in tw2["shared"] if s["person"] == OWNER]
        ok(any(abs((x.get("planned") or 0) - 9.5) < 1e-9
               for x in mine_shared[0]["rows"]),
           "with the plan I actually stated -- 9.5 h, the number saved "
           "in the workload test above")
        code, wk_after = as_viewer(client,
                                   "/api/workload?profile=synthetic",
                                   OWNER)
        ok("dup-partner" in (wk_after.get("shared_with") or []),
           "my own sheet says who I am sharing it with")

        print("story points")
        # "3 points" spoken in a meeting -> 10.5 h on the sheet, and
        # the derived hour names the phrase it came from. The rate
        # lives in policy, NOT in any prompt: a rate baked into a
        # model's output is frozen at export time; a rate in config
        # re-derives everything when the team changes it.
        ok(abs(dashboard_api.parse_estimate(
               "Estimate: 3 points. prototype tested")[0] - 10.5) < 1e-9
           and dashboard_api.parse_estimate("about half a day")[0] == 3.5
           and dashboard_api.parse_estimate("roughly 2sp")[0] == 7.0
           and dashboard_api.parse_estimate("no numbers")[0] is None,
           "the parser speaks the team's dialect -- points, sp, "
           "half a day -- at the policy rate, and stays silent when "
           "nothing was said")
        spconn = psycopg2.connect(dsn)
        with spconn, spconn.cursor() as spc:
            spc.execute("SELECT 1 FROM tasks_current WHERE task_id = "
                        "'task-sp-est'")
            if spc.fetchone() is None:
                spc.execute("""
                    SELECT apply_task_event('task-sp-est', %s, 'created',
                      'manual_edit', NULL, %s::jsonb)""",
                    (OWNER, json.dumps(
                        {"title": "Wire the exporter",
                         "description": "Estimate: 2 points. Agreed in "
                                        "sprint planning.",
                         "status": "in_progress", "owner_ref": OWNER})))
        spconn.close()
        code, wsheet = as_viewer(client,
                                 "/api/workload?profile=synthetic", OWNER)
        est_row = [r for r in wsheet["rows"]
                   if r["subject"] == "task-sp-est"]
        ok(est_row and est_row[0]["estimate"]
           and abs(est_row[0]["estimate"]["hours"] - 7.0) < 1e-9
           and "2 points" in est_row[0]["estimate"]["phrases"],
           "a task whose description carries a spoken estimate shows "
           "the derived hours AND the phrase they came from")
        ok(wsheet["story_point_hours"] == 3.5,
           "and the sheet declares the rate it used, from policy")

        print("receipt pages")
        r = client.get("/receipt/" + "task-dup-a-" + DPX
                       + "?as=" + OWNER + "&profile=synthetic")
        page = r.get_data(as_text=True)
        ok(r.status_code == 200 and "full_trace access" in page
           and "typed by hand" in page,
           "the owner's receipt shows the whole chain: every event, "
           "who acted, and how each fact was recorded")
        ok("append-only" in page,
           "and the page says what makes it a receipt: the record can "
           "only be added to, never edited")
        r = client.get("/receipt/" + "task-dup-a-" + DPX
                       + "?as=total-stranger&profile=synthetic")
        ok(r.status_code == 403,
           "a stranger gets no page at all -- a permalink is not a "
           "leak, the same task_level gate rules it")
        r = client.get("/receipt/" + "task-dup-b-" + DPX
                       + "?as=" + OWNER + "&profile=synthetic")
        ok(r.status_code == 403,
           "and after the grant was revoked, the other board's task is "
           "closed to me again -- receipts follow grants, live")
        r = client.get("/receipt/nope-" + DPX + "?as=" + OWNER
                       + "&profile=synthetic")
        ok(r.status_code in (403, 404),
           "an unknown task id yields nothing")

        print("the system's own scorecard")
        SCX = str(int(time.time() * 1000))
        sconn = psycopg2.connect(dsn)
        with sconn, sconn.cursor() as sc:
            # A prior-month ruling, inserted with a backdated
            # decided_at: decisions are append-only but INSERT sets its
            # own timestamp, which is exactly what a fixture needs.
            sc.execute("""
                INSERT INTO proposals (proposal_id, packet_id, op,
                  rationale, confidence, payload)
                VALUES (%s, %s, 'add_subtask', 'r', 0.6,
                        '{"kind":"add_subtask","parent_task_id":"x",
                          "title":"scorecard prior fixture"}')
                ON CONFLICT DO NOTHING""",
                ("prop-sc-prior-" + SCX, PACKET))
            sc.execute("""
                INSERT INTO decisions (proposal_id, packet_id, decision,
                  reject_reason, decided_by, decided_at)
                VALUES (%s, %s, 'reject', 'wrong_owner', %s,
                        now() - interval '40 days')""",
                ("prop-sc-prior-" + SCX, PACKET, OWNER))
        sconn.close()
        code, scd = as_viewer(client, "/api/scorecard?profile=synthetic",
                              OWNER)
        wo = [l for l in scd["lines"] if "wrong owner" in l["line"]]
        ok(code == 200 and wo,
           "a reject reason with any history gets a trend line")
        ok(all("scorecard of people" in scd["note"]
               for _ in [1]),
           "and the panel restates the founding promise in its own "
           "words: claims the system made, ruled by you -- never a "
           "scorecard of people")
        code, _ = as_viewer(client, "/api/scorecard?profile=synthetic")
        ok(code == 401, "self-only, like every judgement surface")

        print("jason's pilot findings")
        # Runs LAST in the writable section: the instant
        # reconcile SUPERSEDES the fixture packet (the tick's own
        # semantics), so every assertion that expects the fixture
        # packet open must already have run.
        # 1. capture closes its own loop: the note becomes a card in
        #    the SAME request, because kits never run ticks and a
        #    pooled-forever note reads as words vanishing.
        code, body = spost("/api/note?profile=synthetic",
                           {"text": "Create a PowerPoint agent for "
                                    "copilot ability spread",
                            "kind": "work"})
        ok(code == 200 and body.get("reconciled") is True,
           "a captured note is reconciled on the spot, offline, "
           "through the same gates")
        code, rev_n = as_viewer(client, "/api/review?profile=synthetic",
                                OWNER)
        found = [p for p in rev_n["proposals"]
                 if "PowerPoint agent" in (p["payload"].get("title")
                                           or "")]
        ok(found,
           "and it is a review card before the response lands -- "
           "nobody watches their own words disappear again")
        # 2. a candidate accepted from a heard claim stops being
        #    labeled "typed by hand"
        cconn = psycopg2.connect(dsn)
        with cconn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cc:
            cc.execute("""
                SELECT payload FROM task_events
                WHERE actor_type = 'manual_edit'
                  AND payload->>'accepted_from_signal' IS NOT NULL
                LIMIT 1""")
            heard_row = cc.fetchone()
        cconn.close()
        # (the candidate flow is exercised earlier in this suite; if a
        # row exists it must carry its heard_in)
        if heard_row:
            ok(heard_row["payload"].get("heard_in"),
               "candidate accepts carry the meeting they were heard "
               "in, so no surface can call them hand-typed")
        # And the PRE-FIX history: an old accept has no heard_in in its
        # payload and never will, but the alias its accept taught still
        # chains to the meeting. Seed one shaped exactly like the old
        # events and demand the history rescues it.
        OLD = "task-oldaccept-" + str(int(time.time() * 1000))
        oconn = psycopg2.connect(dsn)
        with oconn, oconn.cursor() as oc:
            oc.execute("""
                SELECT apply_task_event(%s, %s, 'created', 'manual_edit',
                  NULL, %s::jsonb)""",
                (OLD, OWNER, json.dumps(
                    {"title": "Old-style accepted work",
                     "status": "proposed", "owner_ref": OWNER})))
            oc.execute("""
                SELECT apply_task_event(%s, %s, 'alias_added', 'system',
                  NULL, %s::jsonb)""",
                (OLD, OWNER, json.dumps(
                    {"alias": "old style accepted work heard",
                     "source_signal_id": SIGNAL})))
        oconn.close()
        code, hist = as_viewer(client,
                               "/api/task/" + OLD
                               + "/history?profile=synthetic", OWNER)
        created = [e for e in hist["events"]
                   if e["event_type"] == "created"]
        ok(code == 200 and created and created[0]["heard_in"],
           "a PRE-FIX accept -- no heard_in in its payload, ever -- "
           "still names its meeting through the alias its accept "
           "taught: the history is rescued, not rewritten")
        # 3. the honest exit for stale work -- its own fixture task,
        #    because cancelling a shared one breaks its owners on rerun
        CXL = "task-cxl-" + str(int(time.time() * 1000))
        xconn = psycopg2.connect(dsn)
        with xconn, xconn.cursor() as xc:
            xc.execute("""
                SELECT apply_task_event(%s, %s, 'created', 'manual_edit',
                  NULL, %s::jsonb)""",
                (CXL, OWNER, json.dumps(
                    {"title": "Word agent testing (two months old)",
                     "status": "in_progress", "owner_ref": OWNER})))
        xconn.close()
        code, _ = spost("/api/nudge?profile=synthetic",
                        {"task_id": CXL, "status": "cancelled",
                         "note": "not relevant anymore"})
        ok(code == 200,
           "'not relevant anymore' is one tap: cancelled, typed by "
           "you, not a false Done")
        nconn2 = psycopg2.connect(dsn)
        with nconn2.cursor() as nc2:
            nc2.execute("SELECT status FROM tasks_current WHERE "
                        "task_id = %s", (CXL,))
            ok(nc2.fetchone()[0] == 'cancelled',
               "and the board says cancelled, with the note in the "
               "event record")
        nconn2.close()
        # Put the packet landscape back. The instant reconcile
        # SUPERSEDED the fixture packet (by design); every fixture-
        # packet assertion after this point -- wherever the file's
        # section order drifts to next -- needs it open again. The
        # restoration is the same append-only surgery the seed does,
        # so this block is position-independent instead of a landmine
        # for whoever inserts a test after it.
        rconn2 = psycopg2.connect(dsn)
        with rconn2, rconn2.cursor() as rr:
            rr.execute("""
                INSERT INTO packet_state_events (packet_id, state)
                SELECT rp.packet_id, 'superseded'
                FROM review_packets rp
                JOIN (SELECT DISTINCT ON (packet_id) packet_id, state
                      FROM packet_state_events
                      ORDER BY packet_id, event_id DESC) le
                  USING (packet_id)
                WHERE rp.user_ref = %s AND rp.packet_id <> %s
                  AND le.state IN ('draft','delivered',
                                   'partially_decided')""",
                (OWNER, PACKET))
            rr.execute("""
                INSERT INTO packet_state_events (packet_id, state)
                SELECT %s, 'delivered'
                WHERE (SELECT state FROM packet_state_events
                       WHERE packet_id = %s
                       ORDER BY event_id DESC LIMIT 1)
                      NOT IN ('draft','delivered','partially_decided')""",
                (PACKET, PACKET))
        rconn2.close()

        print("unreachable ledger")
        saved = dict(dashboard_api.PROFILES.get("copilot") or {})
        dashboard_api.PROFILES["copilot"] = {
            "dsn": "host=localhost port=5432 dbname=tl_no_such_db_xyz "
                   "user=tl_no_such_role_xyz",
            "synthetic": False, "writable": False, "label": "Copilot claims",
            "blurb": "b", "banner": {"tag": "t", "text": "t"}}
        code, body = as_viewer(client, "/api/review?profile=copilot", OWNER)
        ok(code == 503,
           "a ledger the server cannot reach is 503, not 500 -- the request "
           "was fine, the database is not there")
        ok("not reachable" in json.dumps(body)
           and "TL_COPILOT_DSN" in json.dumps(body),
           "and it names the variable to fix, because every panel carries the "
           "profile so one bad DSN fails all of them at once -- fourteen "
           "boxes reading '-> 500' say nothing fourteen times")
        ok("tl_no_such_db_xyz" in json.dumps(body)
           and "tl_no_such_role_xyz" in json.dumps(body),
           "and it quotes the database and role it actually tried, which is "
           "the pair that is usually wrong")
        ok("password" not in json.dumps(body).lower(),
           "without echoing a password back into a browser")
        code, _ = as_viewer(client, "/api/review", OWNER)
        ok(code == 200,
           "and the other ledger is untouched: one unreachable profile must "
           "not take the working one down with it")
        if saved:
            dashboard_api.PROFILES["copilot"] = saved
        else:
            dashboard_api.PROFILES.pop("copilot", None)

        print("uncited claims")
        # The fixture profile is an INSTRUMENT, where the rule does not
        # apply; say otherwise explicitly for the assertions that are
        # about the rule, rather than testing it on a profile exempt
        # from it.
        dashboard_api.PROFILES["synthetic"]["system_of_record"] = True
        code, rev_rec0 = as_viewer(client, "/api/review?profile=synthetic",
                                   OWNER)
        uncited = [p for p in rev_rec0["proposals"]
                   if p["proposal_id"] == UNCITED_PROP]
        anchored = [p for p in rev_rec0["proposals"]
                    if p["proposal_id"] == ATTRIB_PROP]
        ok(len(uncited) == 1 and uncited[0]["traceable"] is False,
           "a card whose only evidence is an assistant's word is marked "
           "untraceable, so 'yes' can be dead before it is clicked "
           "rather than after")
        ok(anchored and anchored[0]["traceable"] is True,
           "and an anchored card beside it is not -- the distinction is "
           "per card, because an anchored ledger is a MIXED one")
        code, body = post("/api/decide", {"proposal_id": UNCITED_PROP,
                                          "decision": "accept"})
        ok(code == 403 and "trace" in json.dumps(body),
           "on a system of record, accepting it is refused with the "
           "reason: a board carrying items that trace to nothing is the "
           "failure this whole design is against")
        check3 = psycopg2.connect(dsn)
        with check3.cursor() as c4:
            c4.execute("SELECT count(*) FROM decisions WHERE proposal_id = %s",
                       (UNCITED_PROP,))
            ok(c4.fetchone()[0] == 0,
               "and nothing was recorded: a refused accept leaves no trace "
               "of a decision nobody was allowed to make")
        check3.close()
        dashboard_api.PROFILES["synthetic"]["system_of_record"] = False
        code, rev_inst = as_viewer(client, "/api/review?profile=synthetic",
                                   OWNER)
        flagged = [p for p in rev_inst["proposals"]
                   if p["proposal_id"] == UNCITED_PROP]
        ok(flagged and flagged[0]["traceable"] is True,
           "on an INSTRUMENT the same card is answerable both ways -- its "
           "board is disposable and its whole purpose is to record what a "
           "person thought, so blocking accept protects nothing and "
           "destroys the measurement")
        dashboard_api.PROFILES["synthetic"]["system_of_record"] = True
        code, body = post("/api/decide", {"proposal_id": UNCITED_PROP,
                                          "decision": "reject",
                                          "reject_reason": "not_a_task"})
        ok(code == 200,
           "and even on a system of record, REJECTING it is allowed -- "
           "declining an unverifiable claim is safe, it is the most "
           "informative thing a reviewer can say about one, and "
           "forbidding it made every such card a dead end for somebody "
           "whose whole job was to have an opinion")
        dashboard_api.PROFILES["synthetic"]["system_of_record"] = False
        check4 = psycopg2.connect(dsn)
        with check4.cursor() as c5:
            c5.execute("SELECT decision, reject_reason FROM decisions "
                       "WHERE proposal_id = %s", (UNCITED_PROP,))
            row = c5.fetchone()
            ok(row and row[0] == "reject" and row[1] == "not_a_task",
               "and it is recorded with its reason, which is the row the "
               "eval set is built from")
        check4.close()
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
            # task-ub-% excluded: one generation of the blocker-moved
            # fixture seeded MATE's board directly before growing its
            # own colleague ref, and this database is append-only, so
            # those rows are permanent residue. The invariant under
            # test is about the REVIEW flow, which never writes
            # cross-board; scope the count to what it can produce.
            cur.execute("""
                SELECT count(*) AS n FROM task_events
                WHERE user_ref = %s
                  AND task_id NOT LIKE 'task-ub-%%'""", (MATE,))
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
