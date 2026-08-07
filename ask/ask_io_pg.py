"""Retrieval for ask-the-ledger. Every query is scoped to the asker.

The retrieval set IS the access decision. Rather than answering broadly
and filtering the sentences afterwards -- which is how question-answering
systems leak -- every query here joins through visible_tasks(viewer), so a
row the asker may not see never enters the candidate set and therefore can
never reach an answer.

Three levels of scope, matching the rest of the system:

  board rows      visible_tasks(viewer), excluding status_only, which is a
                  blocker's narrow sight of one task and not something to
                  narrate answers from
  evidence        full_trace only. At progress level an answer still cites
                  the board event -- what changed and when -- but not the
                  messages behind it
  held claims     the asker's own pool, self-only like everywhere else

Python 3.8 compatible. Requires psycopg2.
"""

import datetime
import decimal

import psycopg2
import psycopg2.extras

VIS = ("SELECT task_id, access_level FROM visible_tasks(%s) "
       "WHERE access_level <> 'status_only'")


def _clean(rows):
    for r in rows:
        for k, v in r.items():
            if isinstance(v, decimal.Decimal):
                r[k] = float(v)
            elif isinstance(v, (datetime.datetime, datetime.date)):
                r[k] = v.isoformat()
    return rows


class Retriever(object):
    """Read-only, per-asker. One connection per call keeps this safe to use
    from the Flask app, which is already READ ONLY at the session level."""

    def __init__(self, dsn, viewer):
        self.dsn = dsn
        self.viewer = viewer
        self._tasks = None

    def _q(self, sql, params=()):
        conn = psycopg2.connect(self.dsn)
        try:
            conn.set_session(readonly=True, autocommit=True)
            with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return _clean([dict(r) for r in cur.fetchall()])
        finally:
            conn.close()

    def tasks(self):
        """Every task the asker may narrate, with its alias set so a
        question can match the words people actually used."""
        if self._tasks is None:
            self._tasks = self._q("""
                WITH vis AS ({0})
                SELECT t.task_id, t.title, t.status, t.owner_ref, t.deadline,
                       t.user_ref, vis.access_level,
                       COALESCE(array_agg(a.alias)
                         FILTER (WHERE a.alias IS NOT NULL), '{{}}') AS aliases,
                       COALESCE(b.open_blockers, 0) AS open_blockers
                FROM vis JOIN tasks_current t USING (task_id)
                LEFT JOIN task_aliases a USING (task_id)
                LEFT JOIN (SELECT task_id, count(*) AS open_blockers
                           FROM task_blockers WHERE resolved_event_id IS NULL
                           GROUP BY task_id) b USING (task_id)
                WHERE t.merged_into IS NULL
                GROUP BY t.task_id, t.title, t.status, t.owner_ref,
                         t.deadline, t.user_ref, vis.access_level,
                         b.open_blockers""".format(VIS), (self.viewer,))
            for t in self._tasks:
                t["aliases"] = list(t["aliases"] or [])
        return self._tasks

    def blocked(self):
        return self._q("""
            WITH vis AS ({0})
            SELECT t.task_id, t.title, blk.title AS blocking_title,
                   blk.user_ref AS blocking_owner,
                   b.opened_event_id, oe.occurred_at AS opened_at
            FROM vis JOIN tasks_current t USING (task_id)
            JOIN task_blockers b ON b.task_id = t.task_id
                                AND b.resolved_event_id IS NULL
            JOIN task_events oe ON oe.event_id = b.opened_event_id
            LEFT JOIN tasks_current blk ON blk.task_id = b.blocking_task_id
            ORDER BY oe.occurred_at""".format(VIS), (self.viewer,))

    def overdue(self):
        return self._q("""
            WITH vis AS ({0})
            SELECT t.task_id, t.title, t.status, t.deadline, t.owner_ref
            FROM vis JOIN tasks_current t USING (task_id)
            WHERE t.merged_into IS NULL AND t.deadline < CURRENT_DATE
              AND t.status NOT IN ('completed','cancelled')
            ORDER BY t.deadline""".format(VIS), (self.viewer,))

    def held(self):
        """The asker's own pooled claims: heard, not confirmed. Self-only,
        and the reason this system can say "somebody said so" without
        saying "so"."""
        return self._q("""
            SELECT u.signal_id, u.title, u.signal_type, u.author_ref,
                   (SELECT count(*) FROM evidence_refs e
                     WHERE e.signal_id = u.signal_id) AS sources
            FROM v_unconsumed_signals u
            WHERE u.user_ref = %s
            ORDER BY u.signal_id""", (self.viewer,))

    def pending(self):
        """The asker's own review queue. Self-only, like held claims: an
        undecided proposal is unreviewed model output about a person."""
        rows = self._q("""
            WITH latest AS (
              SELECT DISTINCT ON (packet_id) packet_id, state
              FROM packet_state_events ORDER BY packet_id, event_id DESC)
            SELECT count(p.proposal_id) - count(d.decision_id) AS undecided,
                   min(rp.generated_at) AS oldest_at
            FROM review_packets rp
            JOIN latest l USING (packet_id)
            JOIN proposals p USING (packet_id)
            LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
            WHERE rp.user_ref = %s
              AND l.state IN ('draft','delivered','partially_decided')""",
            (self.viewer,))
        return rows[0] if rows else {}

    def meetings(self, terms):
        rows = self._q("""
            WITH mine AS (
              SELECT DISTINCT s.signal_id, s.batch_id
              FROM signals s
              JOIN signal_pool_events pe ON pe.signal_id = s.signal_id
              WHERE pe.user_ref = %s),
            claims AS (
              SELECT regexp_replace(b.source_ref, '-p[0-9]+$', '')
                       AS source_ref,
                     b.window_start, m.signal_id,
                     bool_or(t.task_id IS NOT NULL)     AS landed,
                     bool_or(u.signal_id IS NOT NULL)   AS waiting
              FROM mine m
              JOIN extraction_batches b ON b.batch_id = m.batch_id
              LEFT JOIN proposal_signals ps ON ps.signal_id = m.signal_id
              LEFT JOIN decisions d ON d.proposal_id = ps.proposal_id
                                   AND d.decision IN ('accept','accept_edited')
              LEFT JOIN task_events e ON e.decision_id = d.decision_id
              LEFT JOIN tasks_current t ON t.task_id = e.task_id
              LEFT JOIN v_unconsumed_signals u
                     ON u.signal_id = m.signal_id AND u.user_ref = %s
              GROUP BY 1, 2, 3)
            SELECT source_ref, min(window_start) AS met_at,
                   count(*)                        AS claims,
                   count(*) FILTER (WHERE landed)  AS landed,
                   count(*) FILTER (WHERE waiting) AS waiting
            FROM claims GROUP BY source_ref
            ORDER BY min(window_start) DESC LIMIT 12""",
            (self.viewer, self.viewer))
        if not terms:
            return rows[:5]
        keep = []
        for r in rows:
            hay = (r["source_ref"] or "").lower().replace("_", " ")
            if any(t in hay for t in terms):
                keep.append(r)
        return keep or rows[:3]

    def history(self, task_id):
        return self._q("""
            SELECT e.event_id, e.event_type, e.actor_type, e.occurred_at
            FROM task_events e
            WHERE e.task_id = %s
            ORDER BY e.board_version DESC LIMIT 3""", (task_id,))

    def evidence(self, task_id):
        """Evidence pointers behind a task's confirmed changes -- full_trace
        only. A progress-level asker still gets the board citation; what
        they do not get is the message it came from."""
        lvl = self._q("SELECT task_access(%s, %s) AS lvl",
                      (self.viewer, task_id))
        if not lvl or lvl[0]["lvl"] != "full_trace":
            return []
        return self._q("""
            SELECT DISTINCT ev.resource_id, ev.source_type, ev.content_hash,
                   s.author_ref
            FROM task_events e
            JOIN decisions d         ON d.decision_id = e.decision_id
            JOIN proposal_signals ps ON ps.proposal_id = d.proposal_id
            JOIN signals s           ON s.signal_id = ps.signal_id
            JOIN evidence_refs ev    ON ev.signal_id = s.signal_id
            WHERE e.task_id = %s
            ORDER BY ev.resource_id LIMIT 3""", (task_id,))
