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

Sessions are READ ONLY: grants are written through grant_access()/
revoke_access() in SQL (wired to the bot's /share command or an admin flow),
never through this API. Python 3.8 compatible.
"""

import datetime
import decimal
import os

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
    return jsonify(q("""
        SELECT t.task_id, t.title, t.status, t.owner_ref, t.deadline,
               t.project_id, pr.name AS project_name,
               vt.access_level,
               (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
                AND t.status NOT IN ('completed','cancelled')) AS overdue,
               COALESCE(b.open_blockers, 0)  AS open_blockers,
               COALESCE(c.subtasks, 0)       AS subtasks,
               t.updated_at
        FROM visible_tasks(%s) vt
        JOIN tasks_current t USING (task_id)
        LEFT JOIN projects pr USING (project_id)
        LEFT JOIN (SELECT task_id, count(*) AS open_blockers FROM task_blockers
                   WHERE resolved_event_id IS NULL GROUP BY task_id) b
               USING (task_id)
        LEFT JOIN (SELECT parent_task_id AS task_id, count(*) AS subtasks
                   FROM tasks_current WHERE parent_task_id IS NOT NULL
                   GROUP BY parent_task_id) c USING (task_id)
        WHERE t.user_ref = %s AND t.merged_into IS NULL
        ORDER BY overdue DESC, t.status, t.deadline NULLS LAST""",
        (v, user_ref)))


@app.route("/api/task/<task_id>/history")
def history(task_id):
    v = viewer()
    if not v:
        return _need_viewer()
    level = task_level(v, task_id)
    if level is None:
        return jsonify({"error": "task not shared with you"}), 403
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
    return jsonify(q("""
        SELECT p.proposal_id, p.op, p.rationale, p.confidence,
               s.signal_id, s.signal_type, s.title AS signal_title,
               s.confidence AS signal_confidence,
               ev.ordinal, ev.source_type, ev.resource_id,
               ev.span_start, ev.span_end, ev.content_hash
        FROM task_events e
        JOIN decisions d         ON d.decision_id = e.decision_id
        JOIN proposals p         ON p.proposal_id = d.proposal_id
        JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
        JOIN signals s           ON s.signal_id = ps.signal_id
        JOIN evidence_refs ev    ON ev.signal_id = s.signal_id
        WHERE e.event_id = %s
        ORDER BY s.signal_id, ev.ordinal""", (event_id,)))


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


@app.route("/api/grants")
def grants_given():
    v = viewer()
    if not v:
        return _need_viewer()
    return jsonify(q("""
        SELECT grantee_ref, subject_type, subject_id, level, occurred_at
        FROM v_current_grants WHERE granted_by = %s
        ORDER BY subject_type, subject_id, grantee_ref""", (v,)))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8090)
