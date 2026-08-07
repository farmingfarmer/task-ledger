"""Postgres loader for digests.

load_personal(dsn, user_ref, days)   one person's own week
load_project(dsn, project_id, days)  a project's week, progress level
recipients(dsn, project_id)          who the project has been shared with

The access rules are the same ones the dashboard obeys, applied at the
point the data is READ rather than at the point it is rendered:

  - the personal digest filters on the viewer's own ref throughout. It
    carries pending packets, held claims and their own decisions, which
    are self-only however many grants exist.
  - the project digest never SELECTS a rationale, a decider, a confidence
    or an evidence pointer. A stakeholder digest cannot leak the
    discussion behind a change because the query does not fetch it -- the
    redaction is structural, not a filter that has to be maintained.
  - recipients come from v_current_grants, so a lapsed or revoked grant
    stops the mail with no separate list to prune.

Python 3.8 compatible. Requires psycopg2.
"""

import datetime
import decimal

import psycopg2
import psycopg2.extras


def _rows(dsn, sql, params=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _clean(rows, *_ignored):
    """Hand the builder plain JSON types. psycopg2 returns numeric as
    Decimal and dates as date objects; the builder is pure and should not
    have to know that its input came from Postgres."""
    for r in rows:
        for k, v in r.items():
            if isinstance(v, decimal.Decimal):
                r[k] = float(v)
            elif isinstance(v, (datetime.datetime, datetime.date)):
                r[k] = v.isoformat()
    return rows


def load_personal(dsn, user_ref, days=7):
    win = "%s days" % int(days)
    out = {}

    out["confirmed"] = _clean(_rows(dsn, """
        SELECT t.title, e.event_type, d.decision, e.occurred_at
        FROM task_events e
        JOIN decisions d ON d.decision_id = e.decision_id
        JOIN tasks_current t ON t.task_id = e.task_id
        WHERE d.decided_by = %s AND e.occurred_at > now() - %s::interval
        ORDER BY e.event_id DESC""", (user_ref, win)), "occurred_at")

    pend = _rows(dsn, """
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT count(p.proposal_id) - count(d.decision_id) AS undecided,
               count(*) FILTER (WHERE p.conflict_type IS NOT NULL
                                  AND d.decision_id IS NULL) AS conflicts,
               min(rp.generated_at) AS oldest_at
        FROM review_packets rp
        JOIN latest l USING (packet_id)
        JOIN proposals p USING (packet_id)
        LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')""",
        (user_ref,))
    out["pending"] = _clean(pend, "oldest_at")[0] if pend else {}

    out["held"] = _clean(_rows(dsn, """
        SELECT u.title, u.signal_type,
               (SELECT count(*) FROM evidence_refs e
                 WHERE e.signal_id = u.signal_id) AS sources
        FROM v_unconsumed_signals u
        WHERE u.user_ref = %s
        ORDER BY u.signal_id""", (user_ref,)))

    out["quiet"] = _rows(dsn, """
        SELECT t.task_id, t.title, t.status,
               floor(EXTRACT(epoch FROM now() - t.updated_at)/86400)::int
                 AS quiet_days
        FROM tasks_current t
        WHERE t.user_ref = %s AND t.merged_into IS NULL
          AND t.status NOT IN ('completed','cancelled')
          AND t.updated_at < now() - interval '14 days'
        ORDER BY t.updated_at""", (user_ref,))

    out["overdue"] = _clean(_rows(dsn, """
        SELECT t.task_id, t.title, t.deadline
        FROM tasks_current t
        WHERE t.user_ref = %s AND t.merged_into IS NULL
          AND t.deadline < CURRENT_DATE
          AND t.status NOT IN ('completed','cancelled')
        ORDER BY t.deadline""", (user_ref,)), "deadline")

    out["blocked_on"] = _rows(dsn, """
        SELECT mine.title AS my_title, blk.title AS blocking_title,
               blk.user_ref AS blocking_owner, blk.status AS blocking_status
        FROM task_blockers b
        JOIN tasks_current mine ON mine.task_id = b.task_id
        LEFT JOIN tasks_current blk ON blk.task_id = b.blocking_task_id
        WHERE b.resolved_event_id IS NULL AND mine.user_ref = %s
        ORDER BY mine.task_id""", (user_ref,))

    out["blocking"] = _rows(dsn, """
        SELECT waiter.user_ref AS blocked_user,
               waiter.title    AS blocked_title
        FROM task_blockers b
        JOIN tasks_current waiter ON waiter.task_id = b.task_id
        LEFT JOIN tasks_current mine ON mine.task_id = b.blocking_task_id
        WHERE b.resolved_event_id IS NULL
          AND waiter.user_ref <> %s
          AND (mine.user_ref = %s OR b.blocking_party = %s)
        ORDER BY waiter.user_ref""", (user_ref, user_ref, user_ref))

    # Meetings of the period, and how much of each became work. The -pN
    # suffix is stripped so a split meeting reports once, exactly as the
    # follow-through view does.
    out["meetings"] = _clean(_rows(dsn, """
        WITH mine AS (
          SELECT DISTINCT s.signal_id, s.batch_id
          FROM signals s
          JOIN signal_pool_events pe ON pe.signal_id = s.signal_id
          WHERE pe.user_ref = %s),
        claims AS (
          SELECT regexp_replace(b.source_ref, '-p[0-9]+$', '') AS source_ref,
                 b.window_start, m.signal_id,
                 bool_or(t.task_id IS NOT NULL) AS landed,
                 bool_or(u.signal_id IS NOT NULL) AS waiting
          FROM mine m
          JOIN extraction_batches b ON b.batch_id = m.batch_id
          LEFT JOIN proposal_signals ps ON ps.signal_id = m.signal_id
          LEFT JOIN decisions d ON d.proposal_id = ps.proposal_id
                               AND d.decision IN ('accept','accept_edited')
          LEFT JOIN task_events e ON e.decision_id = d.decision_id
          LEFT JOIN tasks_current t ON t.task_id = e.task_id
          LEFT JOIN v_unconsumed_signals u
                 ON u.signal_id = m.signal_id AND u.user_ref = %s
          WHERE b.window_start > now() - %s::interval
          GROUP BY 1, 2, 3)
        SELECT source_ref, min(window_start) AS met_at,
               count(*)                        AS claims,
               count(*) FILTER (WHERE landed)  AS landed,
               count(*) FILTER (WHERE waiting) AS waiting
        FROM claims GROUP BY source_ref
        ORDER BY min(window_start) DESC""",
        (user_ref, user_ref, win)), "met_at")

    lat = _rows(dsn, """
        WITH chain AS (
          SELECT e.event_id,
                 EXTRACT(epoch FROM e.occurred_at - min(b.window_start))
                   / 3600.0 AS hours
          FROM task_events e
          JOIN tasks_current t      ON t.task_id = e.task_id
          JOIN decisions d          ON d.decision_id = e.decision_id
          JOIN proposal_signals ps  ON ps.proposal_id = d.proposal_id
          JOIN signals s            ON s.signal_id = ps.signal_id
          JOIN extraction_batches b ON b.batch_id = s.batch_id
          WHERE e.event_type = 'created' AND t.user_ref = %s
            AND e.occurred_at > now() - %s::interval
          GROUP BY e.event_id, e.occurred_at)
        SELECT round((percentile_cont(0.5) WITHIN GROUP (
                 ORDER BY hours))::numeric, 1) AS median_hours,
               count(*) AS n
        FROM chain""", (user_ref, win))
    out["latency"] = lat[0] if lat else {}
    return out


def load_project(dsn, project_id, days=7):
    """Progress level by construction: no rationale, decider, confidence or
    evidence appears in any of these selects."""
    win = "%s days" % int(days)
    project = _rows(dsn, """
        SELECT project_id, name, owner_ref FROM projects
        WHERE project_id = %s""", (project_id,))
    if not project:
        return None, {}

    changes = _clean(_rows(dsn, """
        SELECT t.title, e.event_type, e.occurred_at, e.actor_type
        FROM task_events e
        JOIN tasks_current t ON t.task_id = e.task_id
        WHERE t.project_id = %s AND e.occurred_at > now() - %s::interval
        ORDER BY e.event_id DESC""", (project_id, win)), "occurred_at")

    open_rows = _clean(_rows(dsn, """
        SELECT t.title, t.status, t.owner_ref, t.deadline,
               (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
                AND t.status NOT IN ('completed','cancelled')) AS overdue
        FROM tasks_current t
        WHERE t.project_id = %s AND t.merged_into IS NULL
          AND t.status NOT IN ('completed','cancelled')
        ORDER BY t.deadline NULLS LAST""", (project_id,)), "deadline")

    counts = _rows(dsn, """
        SELECT count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
               count(*) FILTER (WHERE status = 'blocked')     AS blocked,
               count(*) FILTER (WHERE status = 'proposed')    AS proposed,
               count(*) FILTER (WHERE status = 'completed')   AS completed
        FROM tasks_current
        WHERE project_id = %s AND merged_into IS NULL""", (project_id,))

    return project[0], {"changes": changes, "open": open_rows,
                        "counts": counts[0] if counts else {}}


def recipients(dsn, project_id):
    """Who currently holds a grant on this project. Expired and revoked
    grants are already gone from the view, so there is no second list to
    keep in step."""
    return _rows(dsn, """
        SELECT grantee_ref, level, expires_at
        FROM v_current_grants
        WHERE subject_type = 'project' AND subject_id = %s
        ORDER BY grantee_ref""", (project_id,))
