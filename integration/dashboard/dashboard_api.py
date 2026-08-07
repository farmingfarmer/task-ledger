"""Dashboard API, viewer-aware.

Every request carries a viewer identity; every task-reading query joins
through visible_tasks(viewer) -- the single authorization function defined in
access_control.sql. The API never reads tasks_current for another user
directly, so a policy change in that one SQL function changes every endpoint
at once and there is no second code path to forget.

Identity: X-Viewer header here for local demo. In production this comes from
the validated AAD token on the request; nothing else changes.

Access levels and what they unlock:
  progress    board state + history skeleton. Rationales, decider identity,
              confidence, blocker text, and provenance are REDACTED -- these
              are derived from internal communications the stakeholder was
              not part of.
  full_trace  everything down to evidence pointers.
Owners are always full_trace on their own tasks.

Endpoints:
  GET /api/me                        viewer, boards visible, grants received
  GET /api/team                      Q5 rollup over VISIBLE tasks only
  GET /api/board/<user_ref>          Q1 filtered; includes project + level
  GET /api/task/<id>/history         Q2; redacted at progress level
  GET /api/event/<id>/provenance     Q3; 403 unless full_trace on the task
  GET /api/blocked-on/<user_ref>     Q4; owner or full_trace board grant only
  GET /api/grants                    grants the viewer has given (audit view)
  GET /api/pending                   Q6; SELF ONLY
  GET /api/held                      pooled, unconsumed claims; SELF ONLY
  GET /api/calibration               Q7 + Q8 on own decisions; SELF ONLY
  GET /api/viewers                   demo identity picker; off by default
  GET /api/ask?q=...                 answered from confirmed state, cited
  GET /api/meetings                  follow-through per conversation
  GET /api/meeting/<source_ref>      every claim one conversation produced
  GET /api/metrics                   the measures behind the charts

SELF ONLY is a stricter rule than the grant ladder: pending packets, held
claims, and reject reasons are unreviewed model output about a person and
what they judged wrong about it. A full_trace grant covers the confirmed
board, never this. These endpoints filter on the viewer's own ref and
consult no grant at all.

Sessions are READ ONLY: grants are written through grant_access()/
revoke_access() in SQL (wired to the bot's /share command or an admin flow),
never through this API. Python 3.8 compatible.
"""

import datetime
import decimal
import os
import sys

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

DSN = os.environ.get(
    "DB_DSN", "host=/home/claude/pgsock port=5433 dbname=taskintel user=postgres")

REDACTED = "[shared at progress level - detail hidden]"


def q(sql, params=()):
    conn = psycopg2.connect(DSN)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [_clean(dict(r)) for r in rows]
    finally:
        conn.close()


def _clean(row):
    for k, v in row.items():
        if isinstance(v, (datetime.datetime, datetime.date)):
            row[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            row[k] = float(v)
    return row


def viewer():
    v = request.headers.get("X-Viewer", "").strip()
    return v or None


def task_level(v, task_id):
    rows = q("SELECT task_access(%s, %s) AS lvl", (v, task_id))
    return rows[0]["lvl"] if rows else None


def _need_viewer():
    return jsonify({"error": "no viewer identity on request"}), 401


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)),
                               "dashboard.html")


@app.route("/assets/<path:filename>")
def assets(filename):
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    return send_from_directory(base, filename)


@app.route("/api/me")
def me():
    v = viewer()
    if not v:
        return _need_viewer()
    boards = q("""
        SELECT t.user_ref, count(*) AS visible_tasks,
               bool_or(vt.access_level = 'full_trace') AS any_full_trace
        FROM visible_tasks(%s) vt JOIN tasks_current t USING (task_id)
        WHERE t.merged_into IS NULL
        GROUP BY t.user_ref ORDER BY t.user_ref""", (v,))
    received = q("""
        SELECT subject_type, subject_id, level, granted_by, occurred_at
        FROM v_current_grants WHERE grantee_ref = %s
        ORDER BY occurred_at""", (v,))
    return jsonify({"viewer": v, "boards": boards,
                    "grants_received": received})


@app.route("/api/viewers")
def viewers():
    """Demo-only identity picker, OFF unless TL_VIEWER_PICKER=1.

    Every other task-reading endpoint joins through visible_tasks(); this
    one cannot, because it answers "who could I sign in as" before there
    is a viewer to scope by. Enumerating everyone who owns a task leaks
    the existence of people and their work -- the same no-count-leakage
    rule the team rollup obeys. In production the viewer is the AAD
    identity on the request and this endpoint does not exist at all.
    """
    if os.environ.get("TL_VIEWER_PICKER") != "1":
        return jsonify({"error": "viewer picker disabled; set "
                                 "TL_VIEWER_PICKER=1 for local demos"}), 404
    rows = q("SELECT DISTINCT owner_ref AS r FROM tasks_current "
             "WHERE owner_ref IS NOT NULL AND owner_ref <> '' "
             "ORDER BY 1")
    return jsonify({"viewers": [x["r"] for x in rows]})


@app.route("/api/pending")
def pending():
    """Q6 for the viewer's own packets: what is waiting on them.

    Owner-only, and not weakened by a full_trace board grant: a pending
    packet is unreviewed model output about someone, which is a strictly
    more sensitive thing than the confirmed board a grant covers.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    rows = q("""
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT rp.packet_id, l.state, rp.generated_at, rp.expires_at,
               count(p.proposal_id)                        AS proposals,
               count(p.proposal_id) - count(d.decision_id) AS undecided,
               count(*) FILTER (WHERE p.conflict_type IS NOT NULL
                                  AND d.decision_id IS NULL) AS conflicts
        FROM review_packets rp
        JOIN latest l USING (packet_id)
        JOIN proposals p USING (packet_id)
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')
        GROUP BY rp.packet_id, l.state, rp.generated_at, rp.expires_at
        HAVING count(p.proposal_id) - count(d.decision_id) > 0
        ORDER BY rp.generated_at""", (v,))
    return jsonify({"packets": rows,
                    "undecided": sum(r["undecided"] for r in rows),
                    "conflicts": sum(r["conflicts"] for r in rows)})


@app.route("/api/held")
def held():
    """Claims pooled for the viewer and not yet consumed by a packet.

    The gates that hold a signal (corroboration, closure strictness) leave
    it in the pool rather than guessing, and until now that state was
    invisible outside psql -- so work the system knows about but cannot
    yet propose looked identical to work it never heard. Owner-only: these
    are unreviewed claims, the same sensitivity as a pending packet.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT u.signal_id, u.signal_type, u.title, u.confidence,
               u.deadline_raw, u.status_signal,
               (SELECT count(*) FROM evidence_refs e
                 WHERE e.signal_id = u.signal_id)          AS sources,
               (SELECT min(pe.occurred_at) FROM signal_pool_events pe
                 WHERE pe.signal_id = u.signal_id
                   AND pe.user_ref = u.user_ref)           AS pooled_at
        FROM v_unconsumed_signals u
        WHERE u.user_ref = %s
        ORDER BY pooled_at DESC NULLS LAST, u.signal_id""", (v,)))


@app.route("/api/calibration")
def calibration():
    """Q7 + Q8 over the viewer's OWN decisions: the feedback loop the
    matching policy calibrates on (MATCHING_POLICY section 5).

    Reject reasons are a record of what a person judged wrong about claims
    made in their communications, so this is owner-only and never widened
    by a grant. Drift is the current week against the trailing four-week
    mean; the histogram routes fixes -- duplicate to thresholds,
    bad_date_resolution to extraction, wrong_task_match to auto-link.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    drift = q("""
        WITH weekly AS (
          SELECT date_trunc('week', d.decided_at) AS wk, p.op,
                 count(*) FILTER (WHERE d.decision IN ('accept',
                                                       'accept_edited'))::numeric
                   / count(*) AS accept_rate,
                 count(*) AS n
          FROM decisions d
          JOIN proposals p USING (proposal_id)
          JOIN review_packets rp ON rp.packet_id = p.packet_id
          WHERE d.decision <> 'defer' AND rp.user_ref = %s
          GROUP BY 1, 2)
        SELECT wk, op, round(accept_rate, 3) AS accept_rate, n,
               round(avg(accept_rate) OVER (PARTITION BY op ORDER BY wk
                     ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING), 3)
                 AS trailing_4wk,
               COALESCE((accept_rate - avg(accept_rate) OVER (
                     PARTITION BY op ORDER BY wk
                     ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING))
                 < -0.15, false) AS drift_alarm
        FROM weekly ORDER BY op, wk""", (v,))
    rejects = q("""
        SELECT p.op, d.reject_reason, count(*) AS n
        FROM decisions d
        JOIN proposals p USING (proposal_id)
        JOIN review_packets rp ON rp.packet_id = p.packet_id
        WHERE d.decision = 'reject' AND rp.user_ref = %s
        GROUP BY p.op, d.reject_reason
        ORDER BY n DESC, p.op""", (v,))
    decided = q("""
        SELECT count(*) AS n FROM decisions d
        JOIN proposals p USING (proposal_id)
        JOIN review_packets rp ON rp.packet_id = p.packet_id
        WHERE rp.user_ref = %s""", (v,))
    return jsonify({"drift": drift, "rejects": rejects,
                    "decisions_total": decided[0]["n"] if decided else 0})


@app.route("/api/team")
def team():
    v = viewer()
    if not v:
        return _need_viewer()
    # Rollup over visible tasks only: a viewer's counts never reveal the
    # existence of work they were not granted.
    return jsonify(q("""
        SELECT t.user_ref,
               count(*) FILTER (WHERE t.status = 'in_progress') AS in_progress,
               count(*) FILTER (WHERE t.status = 'blocked')     AS blocked,
               count(*) FILTER (WHERE t.status = 'proposed')    AS proposed,
               count(*) FILTER (WHERE t.status = 'completed'
                                AND t.updated_at > now() - interval '14 days')
                                                                AS completed_recent,
               count(*) FILTER (WHERE t.deadline < CURRENT_DATE
                                AND t.status NOT IN ('completed','cancelled'))
                                                                AS overdue
        FROM visible_tasks(%s) vt
        JOIN tasks_current t USING (task_id)
        WHERE t.merged_into IS NULL
        GROUP BY t.user_ref ORDER BY t.user_ref""", (v,)))


@app.route("/api/board/<user_ref>")
def board(user_ref):
    v = viewer()
    if not v:
        return _need_viewer()
    rows = q("""
        SELECT t.task_id, t.title, t.status, t.owner_ref, t.deadline,
               t.project_id, pr.name AS project_name,
               vt.access_level,
               (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
                AND t.status NOT IN ('completed','cancelled')) AS overdue,
               COALESCE(b.open_blockers, 0)  AS open_blockers,
               COALESCE(c.subtasks, 0)       AS subtasks,
               t.updated_at,
               floor(EXTRACT(epoch FROM now() - t.updated_at) / 86400)::int
                 AS quiet_days,
               dl.deadline_raw,
               COALESCE(src.n, 0) AS sources
        FROM visible_tasks(%s) vt
        JOIN tasks_current t USING (task_id)
        LEFT JOIN projects pr USING (project_id)
        LEFT JOIN (SELECT task_id, count(*) AS open_blockers FROM task_blockers
                   WHERE resolved_event_id IS NULL GROUP BY task_id) b
               USING (task_id)
        LEFT JOIN (SELECT parent_task_id AS task_id, count(*) AS subtasks
                   FROM tasks_current WHERE parent_task_id IS NOT NULL
                   GROUP BY parent_task_id) c USING (task_id)
        -- The phrase someone actually said ("end of next sprint"), carried
        -- from the deadline event. Relative-date resolution is the top
        -- failure mode, and the resolved date alone hides it.
        LEFT JOIN LATERAL (
            SELECT e.payload->>'raw_text' AS deadline_raw
            FROM task_events e
            WHERE e.task_id = t.task_id AND e.event_type = 'deadline_changed'
              AND e.payload->>'raw_text' IS NOT NULL
            ORDER BY e.board_version DESC LIMIT 1) dl ON true
        -- How many distinct claims stand behind the confirmed state: one
        -- mention and five mentions are not the same kind of fact.
        LEFT JOIN LATERAL (
            SELECT count(DISTINCT ps.signal_id) AS n
            FROM task_events e
            JOIN decisions d         ON d.decision_id = e.decision_id
            JOIN proposal_signals ps ON ps.proposal_id = d.proposal_id
            WHERE e.task_id = t.task_id) src ON true
        WHERE t.user_ref = %s AND t.merged_into IS NULL
        ORDER BY overdue DESC, t.status, t.deadline NULLS LAST""",
        (v, user_ref))
    _add_last_noted(rows, v)
    _add_collaborators(rows, user_ref)
    out = []
    for r in rows:
        # A raw deadline phrase is a quote, and a source count is a fact
        # about someone's communications -- both belong to the provenance
        # rail, not the progress skeleton.
        if r["access_level"] != "full_trace":
            r["deadline_raw"] = None
            r["sources"] = None
            # Who said it is provenance; THAT it changed is progress.
            if r.get("last_noted"):
                r["last_noted"]["on_whose_word"] = None
        if r["access_level"] == "status_only":
            # Visible because it blocks the viewer's own work. They get the
            # title and whether it is moving -- not the deadline, project,
            # owner, blocker counts, or how quiet it has gone.
            r = {"task_id": r["task_id"], "title": r["title"],
                 "status": r["status"], "access_level": "status_only",
                 "blocking_you": True}
        out.append(r)
    return jsonify(out)


def _add_last_noted(rows, viewer_ref):
    """When each task was last moved, by what, and on whose word.

    The single most useful line on a task card and the one thing only this
    system can say: not "updated 3 days ago" but "reported blocked, on
    Priya's word, in Tuesday's standup". Speaker comes from the signals
    behind the decision, so it is absent for anything confirmed before the
    author_ref migration -- the card degrades to the event alone rather
    than inventing a name.
    """
    ids = [r["task_id"] for r in rows]
    if not ids:
        return
    events = q("""
        SELECT DISTINCT ON (e.task_id)
               e.task_id, e.event_id, e.event_type, e.actor_type,
               e.occurred_at, e.payload,
               d.decided_by,
               (SELECT string_agg(DISTINCT s.author_ref, ', ')
                  FROM proposal_signals ps
                  JOIN signals s ON s.signal_id = ps.signal_id
                 WHERE ps.proposal_id = d.proposal_id
                   AND s.author_ref IS NOT NULL) AS on_whose_word
        FROM task_events e
        LEFT JOIN decisions d ON d.decision_id = e.decision_id
        WHERE e.task_id = ANY(%s)
        ORDER BY e.task_id, e.board_version DESC""", (ids,))
    by_task = {e["task_id"]: e for e in events}
    for r in rows:
        e = by_task.get(r["task_id"])
        r["last_noted"] = None if not e else {
            "event_type": e["event_type"], "occurred_at": e["occurred_at"],
            "actor_type": e["actor_type"], "event_id": e["event_id"],
            "payload": e["payload"], "decided_by": e["decided_by"],
            "on_whose_word": e["on_whose_word"]}


def _add_collaborators(rows, board_ref):
    """Who else is attached to each task, and how.

    Three relations, in the order a person cares about them: someone whose
    work this is blocking, someone this is waiting on, and someone sharing
    the project. All of it is already visible on the board -- this only
    says it on the card instead of three panels away.
    """
    ids = [r["task_id"] for r in rows]
    if not ids:
        return
    linked = {}

    def add(task_id, ref, relation):
        if not ref or ref == board_ref:
            return
        bucket = linked.setdefault(task_id, [])
        if not any(x["ref"] == ref and x["relation"] == relation
                   for x in bucket):
            bucket.append({"ref": ref, "relation": relation})

    for b in q("""
            SELECT b.blocking_task_id AS on_task, w.user_ref AS waiter
            FROM task_blockers b
            JOIN tasks_current w ON w.task_id = b.task_id
            WHERE b.blocking_task_id = ANY(%s)
              AND b.resolved_event_id IS NULL""", (ids,)):
        # Keyed to the task being waited ON, not the one doing the waiting.
        add(b["on_task"], b["waiter"], "waiting on this")
    for b in q("""
            SELECT b.task_id, blk.user_ref AS blocker_owner,
                   b.blocking_party
            FROM task_blockers b
            LEFT JOIN tasks_current blk ON blk.task_id = b.blocking_task_id
            WHERE b.task_id = ANY(%s) AND b.resolved_event_id IS NULL""",
            (ids,)):
        add(b["task_id"], b["blocker_owner"] or b["blocking_party"],
            "you are blocked on them")
    for p in q("""
            SELECT mine.task_id, peer.user_ref
            FROM tasks_current mine
            JOIN tasks_current peer ON peer.project_id = mine.project_id
                                   AND peer.user_ref <> mine.user_ref
            WHERE mine.task_id = ANY(%s) AND mine.project_id IS NOT NULL
              AND peer.merged_into IS NULL""", (ids,)):
        add(p["task_id"], p["user_ref"], "shares the project")

    for r in rows:
        r["with"] = linked.get(r["task_id"], [])


@app.route("/api/task/<task_id>/history")
def history(task_id):
    v = viewer()
    if not v:
        return _need_viewer()
    level = task_level(v, task_id)
    if level is None:
        return jsonify({"error": "task not shared with you"}), 403
    if level == "status_only":
        # A blocker edge earns the current status, not the story of how the
        # task got there -- that is the whole point of the narrow level.
        return jsonify({"error": "this task is visible to you only because "
                                 "it blocks your work: status only, no "
                                 "history"}), 403
    rows = q("""
        SELECT e.event_id, e.board_version, e.occurred_at, e.event_type,
               e.actor_type, e.payload,
               d.decision, d.decided_by, d.decided_at,
               p.proposal_id, p.rationale, p.confidence,
               (d.decision_id IS NOT NULL) AS has_provenance
        FROM task_events e
        LEFT JOIN decisions d ON d.decision_id = e.decision_id
        LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
        WHERE e.task_id = %s
        ORDER BY e.board_version""", (task_id,))
    if level == "progress":
        rows = [_redact_event(r) for r in rows]
    return jsonify({"access_level": level, "events": rows})


def _redact_event(r):
    # The history skeleton (what changed, when, by which class of actor)
    # is progress-level information. Everything derived from internal
    # communications -- rationale, who decided, model confidence, blocker
    # wording -- is not.
    r["rationale"] = REDACTED if r.get("rationale") else None
    r["decided_by"] = None
    r["confidence"] = None
    r["proposal_id"] = None
    r["has_provenance"] = False
    payload = dict(r.get("payload") or {})
    if r["event_type"] == "blocker_added":
        payload["description"] = REDACTED
        payload.pop("blocking_party", None)
        payload.pop("blocking_task_id", None)
    if r["event_type"] == "alias_added":
        payload["alias"] = REDACTED
        payload.pop("source_signal_id", None)
    r["payload"] = payload
    return r


@app.route("/api/event/<int:event_id>/provenance")
def provenance(event_id):
    v = viewer()
    if not v:
        return _need_viewer()
    owner_rows = q("SELECT task_id FROM task_events WHERE event_id = %s",
                   (event_id,))
    if not owner_rows:
        return jsonify({"error": "no such event"}), 404
    if task_level(v, owner_rows[0]["task_id"]) != "full_trace":
        return jsonify({"error":
            "provenance requires full-trace access to this task"}), 403
    rows = q("""
        SELECT p.proposal_id, p.op, p.rationale, p.confidence,
               s.signal_id, s.signal_type, s.title AS signal_title,
               s.confidence AS signal_confidence, s.author_ref,
               ev.ordinal, ev.source_type, ev.resource_id,
               ev.span_start, ev.span_end, ev.content_hash
        FROM task_events e
        JOIN decisions d         ON d.decision_id = e.decision_id
        JOIN proposals p         ON p.proposal_id = d.proposal_id
        JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
        JOIN signals s           ON s.signal_id = ps.signal_id
        JOIN evidence_refs ev    ON ev.signal_id = s.signal_id
        WHERE e.event_id = %s
        ORDER BY s.signal_id, ev.ordinal""", (event_id,))
    for r in rows:
        r.update(_evidence_state(r))
    return jsonify(rows)


def _evidence_state(row):
    """The integrity state of one evidence pointer.

    CONTRACTS.md promises the UI flags a hash mismatch rather than quietly
    showing different text -- but that check needs a fetcher to re-read the
    message, and locally there is none. Saying so explicitly is the honest
    version: an unchecked hash must not look like a checked one. When the
    Graph fetcher lands it fills in verified / changed / unavailable here,
    and the UI already renders all four.
    """
    if not row.get("content_hash"):
        return {"integrity": "unhashed",
                "integrity_note": "no content hash was recorded"}
    if not os.environ.get("TL_EVIDENCE_FETCH"):
        return {"integrity": "unverified",
                "integrity_note": "hash not checked - no message fetcher "
                                  "configured"}
    return {"integrity": "unverified",
            "integrity_note": "fetcher configured but verification is not "
                              "wired yet"}


@app.route("/api/blocked-on/<user_ref>")
def blocked_on(user_ref):
    v = viewer()
    if not v:
        return _need_viewer()
    if v != user_ref:
        grants = q("""
            SELECT 1 FROM v_current_grants
            WHERE grantee_ref = %s AND subject_type = 'user'
              AND subject_id = %s AND level = 'full_trace'""", (v, user_ref))
        if not grants:
            return jsonify({"error":
                "the dependency view is owner-only unless the board is "
                "shared at full trace"}), 403
    return jsonify(q("""
        SELECT waiter.user_ref AS blocked_user,
               waiter.task_id  AS blocked_task,
               waiter.title    AS blocked_title,
               b.description   AS blocker_desc,
               mine.task_id    AS my_blocking_task,
               mine.title      AS my_blocking_title,
               mine.status     AS my_task_status
        FROM task_blockers b
        JOIN tasks_current waiter ON waiter.task_id = b.task_id
        LEFT JOIN tasks_current mine ON mine.task_id = b.blocking_task_id
        WHERE b.resolved_event_id IS NULL
          AND (mine.owner_ref = %s OR b.blocking_party = %s)
        ORDER BY waiter.user_ref""", (user_ref, user_ref)))


# Where a claim ended up, most conclusive first. A signal can be linked to
# several proposals across packets -- proposed, deferred, re-reconciled
# after a supersede -- so the raw join fans out and one claim would read as
# four. The follow-through report is per CLAIM, so the rows collapse to the
# outcome that actually describes it: landing on the board outranks the
# rejections that preceded it, and a claim back in the pool is "waiting"
# however it got there.
OUTCOME_RANK = ["done", "cancelled", "on the board", "waiting",
                "proposed, undecided", "deferred", "rejected", "no proposal"]


def _claim_rows(v, source_ref=None):
    """Per-claim follow-through for the viewer's own conversations."""
    params = [v, v]
    where = ""
    if source_ref is not None:
        where = "AND regexp_replace(b.source_ref, '-p[0-9]+$', '') = %s"
        params.append(source_ref)
    rows = q("""
        SELECT s.signal_id, s.signal_type, s.title, s.confidence,
               s.author_ref, s.deadline_raw,
               regexp_replace(b.source_ref, '-p[0-9]+$', '') AS source_ref,
               b.source_type, b.batch_id, b.window_start,
               d.decision, d.reject_reason, p.op,
               t.task_id, t.title AS task_title, t.status AS task_status,
               (u.signal_id IS NOT NULL) AS still_pooled
        FROM signals s
        JOIN extraction_batches b ON b.batch_id = s.batch_id
        JOIN signal_pool_events pe ON pe.signal_id = s.signal_id
                                  AND pe.user_ref = %s
        LEFT JOIN proposal_signals ps ON ps.signal_id = s.signal_id
        LEFT JOIN proposals p         ON p.proposal_id = ps.proposal_id
        LEFT JOIN decisions d         ON d.proposal_id = ps.proposal_id
        LEFT JOIN task_events e       ON e.decision_id = d.decision_id
        LEFT JOIN tasks_current t     ON t.task_id = e.task_id
        LEFT JOIN v_unconsumed_signals u
               ON u.signal_id = s.signal_id AND u.user_ref = %s
        WHERE true {0}""".format(where), tuple(params))
    best = {}
    for r in rows:
        r["outcome"] = _outcome(r)
        prev = best.get(r["signal_id"])
        if prev is None or (OUTCOME_RANK.index(r["outcome"])
                            < OUTCOME_RANK.index(prev["outcome"])):
            best[r["signal_id"]] = r
    return sorted(best.values(),
                  key=lambda r: (r["source_ref"], r["signal_id"]))


@app.route("/api/ask")
def ask_endpoint():
    """Answer a question from the ledger, scoped to the asker.

    The retrieval set is what visible_tasks(viewer) returns, so an answer
    cannot mention work the asker could not already see, and the sentences
    are assembled from rows rather than written by a model -- see ask.py.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    question = (request.args.get("q") or "").strip()
    if not question:
        return jsonify({"error": "ask something with ?q="}), 400
    if len(question) > 300:
        question = question[:300]
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("../../ask", "../../reconciler"):
        p = os.path.normpath(os.path.join(here, rel))
        if p not in sys.path:
            sys.path.insert(0, p)
    import ask as ask_mod
    import ask_io_pg
    return jsonify(ask_mod.answer(question, ask_io_pg.Retriever(DSN, v)))


@app.route("/api/meetings")
def meetings_v2():
    """Follow-through per conversation, summarised from the same per-claim
    outcomes the detail view shows -- so the summary and the drill-down can
    never disagree."""
    v = viewer()
    if not v:
        return _need_viewer()
    out = {}
    for r in _claim_rows(v):
        m = out.setdefault(r["source_ref"], {
            "source_ref": r["source_ref"], "source_type": r["source_type"],
            "met_at": r["window_start"], "windows": set(), "claims": 0,
            "done": 0, "on the board": 0, "waiting": 0, "rejected": 0,
            "other": 0})
        m["windows"].add(r["batch_id"])
        m["claims"] += 1
        if r["window_start"] < m["met_at"]:
            m["met_at"] = r["window_start"]
        if r["outcome"] in ("done", "on the board", "waiting", "rejected"):
            m[r["outcome"]] += 1
        else:
            m["other"] += 1
    meetings = []
    for m in out.values():
        m["windows"] = len(m["windows"])
        meetings.append(m)
    meetings.sort(key=lambda m: m["met_at"], reverse=True)
    return jsonify(meetings[:25])


@app.route("/api/meeting/<path:source_ref>")
def meeting_detail_v2(source_ref):
    """Every claim one conversation produced, and what became of it."""
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify({"source_ref": source_ref,
                    "claims": _claim_rows(v, source_ref)})


def _outcome(row):
    """One word for where a claim ended up. The order matters: a claim can
    be both proposed and back in the pool (deferred), and the pool is the
    more useful answer because it means the work is still pending."""
    if row["still_pooled"]:
        return "waiting"
    if row["decision"] in ("accept", "accept_edited"):
        if row["task_status"] == "completed":
            return "done"
        if row["task_status"] in ("cancelled",):
            return "cancelled"
        return "on the board"
    if row["decision"] == "reject":
        return "rejected"
    if row["decision"] == "defer":
        return "deferred"
    if row["op"]:
        return "proposed, undecided"
    return "no proposal"


@app.route("/api/timeline")
def timeline():
    """Every item you can see, as a life on a time axis.

    Three counts tell you how much sits at each stage; they cannot show
    movement, which is the thing this ledger is uniquely able to record.
    One row per item — a claim you have heard, a proposal awaiting you, a
    task on a board — running from the moment it was first SAID to where
    it stands now, with a marker at every change along the way.

    Scope is the usual: visible_tasks for board rows (status_only excluded,
    since one blocked-on task is not a life to narrate), plus the viewer's
    own pooled claims, which are self-only everywhere else and stay so
    here. So the chart shows your work and the work of people who have
    shared with you, and nothing else.
    """
    v = viewer()
    if not v:
        return _need_viewer()

    tasks = q("""
        WITH vis AS (SELECT task_id, access_level FROM visible_tasks(%s)
                     WHERE access_level <> 'status_only')
        SELECT t.task_id, t.title, t.user_ref, t.owner_ref, t.status,
               t.created_at, t.deadline, vis.access_level,
               (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
                AND t.status NOT IN ('completed','cancelled')) AS overdue
        FROM vis JOIN tasks_current t USING (task_id)
        WHERE t.merged_into IS NULL
        ORDER BY t.created_at""", (v,))
    ids = [t["task_id"] for t in tasks]

    events, said = {}, {}
    if ids:
        for e in q("""
                SELECT e.task_id, e.event_id, e.event_type, e.occurred_at,
                       e.actor_type
                FROM task_events e WHERE e.task_id = ANY(%s)
                ORDER BY e.task_id, e.board_version""", (ids,)):
            events.setdefault(e["task_id"], []).append(
                {"at": e["occurred_at"], "type": e["event_type"],
                 "actor": e["actor_type"], "event_id": e["event_id"]})
        # When the work was first SAID, not when it was typed in: the
        # extraction window behind whatever decision created the task.
        for r in q("""
                SELECT e.task_id, min(b.window_start) AS said_at
                FROM task_events e
                JOIN decisions d          ON d.decision_id = e.decision_id
                JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
                JOIN signals s            ON s.signal_id = ps.signal_id
                JOIN extraction_batches b ON b.batch_id = s.batch_id
                WHERE e.task_id = ANY(%s)
                GROUP BY e.task_id""", (ids,)):
            said[r["task_id"]] = r["said_at"]

    items = []
    for t in tasks:
        evs = events.get(t["task_id"], [])
        done = [e for e in evs
                if e["type"] in ("completed", "cancelled")]
        items.append({
            "kind": "task", "id": t["task_id"], "title": t["title"],
            "owner": t["owner_ref"] or t["user_ref"],
            "board": t["user_ref"], "mine": t["user_ref"] == v,
            "status": t["status"], "overdue": t["overdue"],
            "deadline": t["deadline"],
            "first_noted": said.get(t["task_id"]) or t["created_at"],
            "said_traced": t["task_id"] in said,
            "ended_at": done[0]["at"] if done else None,
            "stage": ("done" if t["status"] in ("completed", "cancelled")
                      else "board"),
            "events": evs})

    # Claims that never became tasks: they have a beginning and no line
    # yet, which is exactly what the reader should see.
    for c in q("""
            SELECT u.signal_id, u.title, u.signal_type, u.author_ref,
                   (SELECT min(pe.occurred_at) FROM signal_pool_events pe
                     WHERE pe.signal_id = u.signal_id
                       AND pe.user_ref = u.user_ref) AS pooled_at,
                   (SELECT b.window_start FROM extraction_batches b
                     WHERE b.batch_id = u.batch_id)  AS said_at
            FROM v_unconsumed_signals u
            WHERE u.user_ref = %s""", (v,)):
        items.append({
            "kind": "claim", "id": c["signal_id"], "title": c["title"],
            "owner": c["author_ref"] or v, "board": v, "mine": True,
            "status": c["signal_type"], "overdue": False, "deadline": None,
            "first_noted": c["said_at"] or c["pooled_at"],
            "said_traced": c["said_at"] is not None,
            "ended_at": None, "stage": "heard", "events": []})

    pend = q("""
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT count(p.proposal_id) - count(d.decision_id) AS n
        FROM review_packets rp
        JOIN latest l USING (packet_id)
        JOIN proposals p USING (packet_id)
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')""", (v,))
    items.sort(key=lambda i: (i["first_noted"] or ""))
    return jsonify({"items": items,
                    "awaiting": (pend[0]["n"] if pend else 0) or 0,
                    "now": datetime.datetime.now(
                        datetime.timezone.utc).isoformat()})


@app.route("/api/metrics")
def metrics():
    """The measures that need this ledger to exist.

    talk-to-tracking latency is the thesis as a number: every other tracker
    starts its clock when someone types a task in; this one knows when the
    work was first SAID, because the batch window is on the record. The
    rest -- what got confirmed, what generated friction, where the queue
    forms -- are event-log aggregates over the same visible-task scope the
    board uses, so a chart can never show more work than the board would.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    vis = ("WITH vis AS (SELECT task_id FROM visible_tasks(%s) "
           "WHERE access_level <> 'status_only')")

    confirmed = q(vis + """
        SELECT to_char(date_trunc('week', e.occurred_at), 'YYYY-MM-DD') AS wk,
               count(*) FILTER (WHERE e.event_type = 'created')   AS created,
               count(*) FILTER (WHERE e.event_type = 'completed') AS completed
        FROM task_events e JOIN vis USING (task_id)
        WHERE e.occurred_at > now() - interval '12 weeks'
        GROUP BY 1 ORDER BY 1""", (v,))

    friction = q(vis + """
        SELECT to_char(date_trunc('week', e.occurred_at), 'YYYY-MM-DD') AS wk,
               count(*) FILTER (WHERE e.event_type = 'blocker_added')
                 AS blocked,
               count(*) FILTER (WHERE e.event_type = 'deadline_changed')
                 AS moved,
               count(*) FILTER (WHERE e.event_type = 'cancelled')
                 AS cancelled
        FROM task_events e JOIN vis USING (task_id)
        WHERE e.occurred_at > now() - interval '12 weeks'
        GROUP BY 1 ORDER BY 1""", (v,))

    # First mention -> confirmed board state. The join walks the whole
    # provenance chain backwards to the extraction window.
    latency = q(vis + """,
        chain AS (
          SELECT e.task_id, e.occurred_at AS confirmed_at,
                 min(b.window_start)      AS said_at
          FROM task_events e
          JOIN vis USING (task_id)
          JOIN decisions d          ON d.decision_id = e.decision_id
          JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
          JOIN signals s            ON s.signal_id = ps.signal_id
          JOIN extraction_batches b ON b.batch_id = s.batch_id
          WHERE e.event_type = 'created'
          GROUP BY e.task_id, e.occurred_at)
        SELECT to_char(date_trunc('week', confirmed_at), 'YYYY-MM-DD') AS wk,
               round((percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY EXTRACT(epoch FROM confirmed_at - said_at)
                          / 3600.0))::numeric, 1) AS median_hours,
               count(*) AS n
        FROM chain GROUP BY 1 ORDER BY 1""", (v,))

    queue = q(vis + """
        SELECT COALESCE(bt.user_ref, b.blocking_party, 'unattributed')
                 AS party,
               round(sum(EXTRACT(epoch FROM
                 COALESCE(re.occurred_at, now()) - oe.occurred_at)
                 / 86400.0)::numeric, 1)             AS days,
               count(*)                              AS blockers,
               count(*) FILTER (WHERE b.resolved_event_id IS NULL) AS open
        FROM task_blockers b
        JOIN vis ON vis.task_id = b.task_id
        JOIN task_events oe       ON oe.event_id = b.opened_event_id
        LEFT JOIN task_events re  ON re.event_id = b.resolved_event_id
        LEFT JOIN tasks_current bt ON bt.task_id = b.blocking_task_id
        GROUP BY 1 ORDER BY days DESC LIMIT 8""", (v,))

    tiles = q(vis + """,
        chain AS (
          SELECT e.occurred_at - min(b.window_start) AS lag
          FROM task_events e
          JOIN vis USING (task_id)
          JOIN decisions d          ON d.decision_id = e.decision_id
          JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
          JOIN signals s            ON s.signal_id = ps.signal_id
          JOIN extraction_batches b ON b.batch_id = s.batch_id
          WHERE e.event_type = 'created'
          GROUP BY e.event_id, e.occurred_at)
        SELECT
          (SELECT round((percentile_cont(0.5) WITHIN GROUP (
             ORDER BY EXTRACT(epoch FROM lag) / 3600.0))::numeric, 1)
           FROM chain)                                   AS median_lag_hours,
          (SELECT count(*) FROM chain)                   AS traced_tasks,
          (SELECT count(*) FROM task_events e JOIN vis USING (task_id)
            WHERE e.event_type = 'deadline_changed')     AS deadline_moves,
          (SELECT count(DISTINCT t.task_id) FROM tasks_current t
             JOIN vis USING (task_id) WHERE t.deadline IS NOT NULL)
                                                         AS dated_tasks,
          (SELECT round(COALESCE(sum(EXTRACT(epoch FROM
             now() - oe.occurred_at) / 86400.0), 0)::numeric, 1)
           FROM task_blockers b JOIN vis ON vis.task_id = b.task_id
           JOIN task_events oe ON oe.event_id = b.opened_event_id
           WHERE b.resolved_event_id IS NULL)            AS blocked_days_open,
          (SELECT count(*) FROM v_unconsumed_signals u
            WHERE u.user_ref = %s
              AND u.signal_type IN ('completion_claim','cancellation'))
                                                         AS unconfirmed_closures
        """, (v, v))

    return jsonify({"confirmed": confirmed, "friction": friction,
                    "latency": latency, "queue": queue,
                    "tiles": tiles[0] if tiles else {}})


@app.route("/api/recent")
def recent():
    """What the viewer has personally confirmed lately.

    Every accepted proposal is a decision with their name on it, and the
    ledger is careful to record that -- but nothing showed it back to them.
    Two uses: an accountability record, and the fastest way to notice your
    own rubber-stamping, since a run of accepts on things you did not
    really read looks exactly like what it is. Self-only by construction:
    it filters on decided_by.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT e.event_id, e.task_id, t.title, e.event_type, e.occurred_at,
               d.decision, p.op, p.confidence
        FROM task_events e
        JOIN decisions d      ON d.decision_id = e.decision_id
        LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
        JOIN tasks_current t  ON t.task_id = e.task_id
        WHERE d.decided_by = %s
        ORDER BY e.event_id DESC LIMIT 8""", (v,)))


@app.route("/api/waiting-on/<user_ref>")
def waiting_on(user_ref):
    """The reciprocal of blocked-on: what this person is waiting for, and
    whether it is moving.

    Being blocked on someone's task is the one situation where the model
    hands out visibility automatically -- status_only, resolved inside
    visible_tasks -- so this endpoint reads that level rather than
    inventing its own rule. Same gate as the dependency view: yourself, or
    a full_trace grant on the whole board.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    if v != user_ref:
        grants = q("""
            SELECT 1 FROM v_current_grants
            WHERE grantee_ref = %s AND subject_type = 'user'
              AND subject_id = %s AND level = 'full_trace'""", (v, user_ref))
        if not grants:
            return jsonify({"error":
                "the dependency view is owner-only unless the board is "
                "shared at full trace"}), 403
    return jsonify(q("""
        SELECT mine.task_id      AS my_task,
               mine.title        AS my_title,
               b.description     AS blocker_desc,
               b.blocking_party  AS blocking_party,
               blk.task_id       AS blocking_task,
               blk.title         AS blocking_title,
               blk.status        AS blocking_status,
               blk.user_ref      AS blocking_owner,
               vt.access_level   AS blocking_access
        FROM task_blockers b
        JOIN tasks_current mine ON mine.task_id = b.task_id
        LEFT JOIN tasks_current blk ON blk.task_id = b.blocking_task_id
        LEFT JOIN visible_tasks(%s) vt ON vt.task_id = blk.task_id
        WHERE b.resolved_event_id IS NULL AND mine.user_ref = %s
        ORDER BY blk.status NULLS LAST, mine.task_id""", (v, user_ref)))


@app.route("/api/projects")
def projects():
    """Projects the viewer may see: their own, and any granted to them.

    Project scope is the most useful idea in the access model and was
    invisible -- you could be a member of a workstream with no way to see
    that it existed. Counts run through visible_tasks so a project row
    never reveals more work than the grant behind it covers.
    """
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT pr.project_id, pr.name, pr.owner_ref, pr.created_at,
               (pr.owner_ref = %s) AS is_owner,
               g.level AS granted_level, g.expires_at,
               count(vt.task_id)                                  AS tasks,
               count(*) FILTER (WHERE t.status = 'blocked')        AS blocked,
               count(DISTINCT t.user_ref)                          AS people
        FROM projects pr
        LEFT JOIN v_current_grants g
               ON g.grantee_ref = %s AND g.subject_type = 'project'
              AND g.subject_id = pr.project_id
        LEFT JOIN tasks_current t
               ON t.project_id = pr.project_id AND t.merged_into IS NULL
        LEFT JOIN visible_tasks(%s) vt ON vt.task_id = t.task_id
              AND vt.access_level <> 'status_only'
        WHERE pr.owner_ref = %s OR g.grantee_ref IS NOT NULL
        GROUP BY pr.project_id, pr.name, pr.owner_ref, pr.created_at,
                 g.level, g.expires_at
        ORDER BY pr.name""", (v, v, v, v)))


@app.route("/api/grants")
def grants_given():
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT grantee_ref, subject_type, subject_id, level, occurred_at,
               expires_at
        FROM v_current_grants WHERE granted_by = %s
        ORDER BY subject_type, subject_id, grantee_ref""", (v,)))


if __name__ == "__main__":
    if os.environ.get("TL_VIEWER_PICKER") != "1":
        print("viewer picker off (it enumerates everyone with a task); "
              "type an identity in the header, or set TL_VIEWER_PICKER=1")
    app.run(host="127.0.0.1", port=8090)
