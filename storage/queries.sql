-- =========================================================================
-- Drill-down and calibration query paths
-- Each query is named Q1..Q9 and referenced from STORAGE.md.
-- Parameters use psql-style :'param' placeholders; swap for your driver.
-- =========================================================================

-- Q1. BOARD VIEW (dashboard top level)
-- Current tasks for a user: status, deadline, open blocker count, staleness.
-- Excludes merged-away tasks; subtask counts power the drill-down affordance.
SELECT t.task_id, t.title, t.status, t.owner_ref, t.deadline,
       (t.deadline IS NOT NULL AND t.deadline < CURRENT_DATE
        AND t.status NOT IN ('completed','cancelled'))     AS overdue,
       COALESCE(b.open_blockers, 0)                         AS open_blockers,
       COALESCE(c.subtasks, 0)                              AS subtasks,
       t.updated_at
FROM tasks_current t
LEFT JOIN (SELECT task_id, count(*) AS open_blockers FROM task_blockers
           WHERE resolved_event_id IS NULL GROUP BY task_id) b USING (task_id)
LEFT JOIN (SELECT parent_task_id AS task_id, count(*) AS subtasks
           FROM tasks_current WHERE parent_task_id IS NOT NULL
           GROUP BY parent_task_id) c USING (task_id)
WHERE t.user_ref = :'user_ref'
  AND t.merged_into IS NULL
ORDER BY overdue DESC, t.status, t.deadline NULLS LAST;

-- Q2. TASK HISTORY (drill-down level 1)
-- Every change to one task, who/what caused it, in board order. actor tells
-- the reader instantly whether a change was human-typed, human-confirmed AI,
-- or system (e.g. merge side-effects).
SELECT e.board_version, e.occurred_at, e.event_type, e.actor_type,
       e.payload,
       d.decision, d.decided_by, d.decided_at,
       p.proposal_id, p.rationale, p.confidence
FROM task_events e
LEFT JOIN decisions d ON d.decision_id = e.decision_id
LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
WHERE e.task_id = :'task_id'
ORDER BY e.board_version;

-- Q3. PROVENANCE WALK (drill-down level 2)
-- One board change -> proposal -> signals -> evidence pointers. The UI
-- resolves resource_id to a deep link at render time (pointer, not copy).
SELECT p.proposal_id, p.op, p.rationale, p.confidence,
       s.signal_id, s.signal_type, s.title AS signal_title, s.confidence AS signal_conf,
       ev.ordinal, ev.source_type, ev.resource_id, ev.span_start, ev.span_end,
       ev.content_hash
FROM task_events e
JOIN decisions d        ON d.decision_id = e.decision_id
JOIN proposals p        ON p.proposal_id = d.proposal_id
JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
JOIN signals s          ON s.signal_id = ps.signal_id
JOIN evidence_refs ev   ON ev.signal_id = s.signal_id
WHERE e.event_id = :'event_id'
ORDER BY s.signal_id, ev.ordinal;

-- Q4. WHO IS BLOCKED ON ME
-- Cross-user dependency view: open blockers whose blocking edge points at a
-- task I own (or names me as blocking party). This is what blocking_task_id
-- was kept for.
SELECT waiter.user_ref  AS blocked_user,
       waiter.task_id   AS blocked_task,
       waiter.title     AS blocked_title,
       b.description    AS blocker_desc,
       mine.task_id     AS my_blocking_task,
       mine.title       AS my_blocking_title,
       mine.status      AS my_task_status
FROM task_blockers b
JOIN tasks_current waiter ON waiter.task_id = b.task_id
LEFT JOIN tasks_current mine ON mine.task_id = b.blocking_task_id
WHERE b.resolved_event_id IS NULL
  AND (mine.owner_ref = :'user_ref' OR b.blocking_party = :'user_ref')
ORDER BY waiter.user_ref;

-- Q5. TEAM ROLLUP
-- Tasks by status per user plus overdue count; the manager-facing tiles.
SELECT user_ref,
       count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
       count(*) FILTER (WHERE status = 'blocked')     AS blocked,
       count(*) FILTER (WHERE status = 'proposed')    AS proposed,
       count(*) FILTER (WHERE status = 'completed'
                        AND updated_at > now() - interval '14 days')
                                                      AS completed_recent,
       count(*) FILTER (WHERE deadline < CURRENT_DATE
                        AND status NOT IN ('completed','cancelled'))
                                                      AS overdue
FROM tasks_current
WHERE merged_into IS NULL
GROUP BY user_ref
ORDER BY user_ref;

-- Q6. PENDING REVIEW STATE
-- Open packets and their undecided counts; powers the "you have items
-- waiting" nudge and the ops view of review latency.
WITH latest_state AS (
  SELECT DISTINCT ON (packet_id) packet_id, state, occurred_at
  FROM packet_state_events ORDER BY packet_id, event_id DESC
)
SELECT rp.packet_id, rp.user_ref, ls.state, rp.generated_at, rp.expires_at,
       count(p.proposal_id)                                    AS proposals,
       count(p.proposal_id) - count(d.decision_id)             AS undecided
FROM review_packets rp
JOIN latest_state ls USING (packet_id)
JOIN proposals p USING (packet_id)
LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
WHERE ls.state IN ('delivered','partially_decided')
GROUP BY rp.packet_id, rp.user_ref, ls.state, rp.generated_at, rp.expires_at;

-- Q7. CALIBRATION: weekly acceptance rate per op
-- The drift-alarm input: current week vs trailing 4-week mean (matching
-- policy section 5).
WITH weekly AS (
  SELECT date_trunc('week', d.decided_at) AS wk, p.op,
         count(*) FILTER (WHERE d.decision IN ('accept','accept_edited'))::numeric
           / count(*) AS accept_rate,
         count(*) AS n
  FROM decisions d JOIN proposals p USING (proposal_id)
  WHERE d.decision <> 'defer'
  GROUP BY 1, 2
)
SELECT wk, op, round(accept_rate, 3) AS accept_rate, n,
       round(avg(accept_rate) OVER (PARTITION BY op ORDER BY wk
             ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING), 3) AS trailing_4wk,
       (accept_rate - avg(accept_rate) OVER (PARTITION BY op ORDER BY wk
             ROWS BETWEEN 4 PRECEDING AND 1 PRECEDING)) < -0.15 AS drift_alarm
FROM weekly
ORDER BY op, wk;

-- Q8. CALIBRATION: reject-reason histogram per op
-- Routes fixes to the right stage: duplicate -> matching thresholds;
-- bad_date_resolution -> extraction; wrong_task_match -> auto_link too low.
SELECT p.op, d.reject_reason, count(*) AS n
FROM decisions d JOIN proposals p USING (proposal_id)
WHERE d.decision = 'reject'
GROUP BY p.op, d.reject_reason
ORDER BY p.op, n DESC;

-- Q9. ALIAS SET FETCH (reconciler support)
-- Candidate alias sets for matching: canonical title + confirmed aliases for
-- a user's non-terminal tasks plus recently closed ones (14-day reopen window).
SELECT t.task_id, t.status, t.owner_ref,
       array_prepend(t.title, COALESCE(array_agg(a.alias)
         FILTER (WHERE a.alias IS NOT NULL), '{}')) AS alias_set
FROM tasks_current t
LEFT JOIN task_aliases a USING (task_id)
WHERE t.user_ref = :'user_ref'
  AND t.merged_into IS NULL
  AND (t.status NOT IN ('completed','cancelled')
       OR t.updated_at > now() - interval '14 days')
GROUP BY t.task_id, t.status, t.owner_ref, t.title;
