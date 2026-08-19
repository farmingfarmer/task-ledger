-- =========================================================================
-- Migration: a decision about a candidate outlives the next mention of it.
--     psql -d taskintel -f storage/migrate_candidate_state_sticky.sql
-- Fresh databases get this from schema.sql.
--
-- WHY. v_task_candidates projected state as "the latest event", which is
-- right for an append-only log until the events stop being equivalent.
-- They are not: 'sighted' is an observation, while 'promoted', 'dismissed'
-- and 'accepted' are decisions. Latest-wins let an observation overwrite a
-- decision, and mentioning something again is not a reason to reopen it.
--
-- Observed, on a live register:
--
--   20  sighted   the work is mentioned
--   21  sighted   and again -- clears the bar
--   31  promoted  a proposal goes to somebody's review queue
--   35  sighted   the work is mentioned a third time
--
-- and state fell back to 'sighted'. load_candidates offers only 'sighted'
-- rows precisely so that "once a candidate has been promoted into a packet,
-- or dismissed by someone saying it is not a task, it must stop coming back
-- every time the word is said again" -- and this handed it straight back,
-- now carrying three sightings. The next tick would raise a second,
-- identical create proposal for work already sitting in a queue, and would
-- keep doing so on every further mention.
--
-- So a decision sticks: state is the most recent DECISION if one exists,
-- and only otherwise the latest observation. Sightings keep accumulating
-- underneath either way -- the counts are evidence and stay true -- but
-- they no longer reopen a question somebody has already answered.
--
-- Reopening deliberately is still possible and still has to be deliberate:
-- append a decision, which is what the review path does when it rejects.
-- Nothing here deletes or rewrites an event.
-- =========================================================================

BEGIN;

CREATE OR REPLACE VIEW v_task_candidates AS
WITH latest AS (
  SELECT DISTINCT ON (candidate_id, user_ref)
         candidate_id, user_ref, event, title
  FROM task_candidate_events
  ORDER BY candidate_id, user_ref, event_id DESC
), decided AS (
  -- The last thing a person (or the promotion rule) concluded, if anything.
  SELECT DISTINCT ON (candidate_id, user_ref)
         candidate_id, user_ref, event
  FROM task_candidate_events
  WHERE event IN ('promoted', 'dismissed', 'accepted')
  ORDER BY candidate_id, user_ref, event_id DESC
), counts AS (
  SELECT candidate_id, user_ref,
         count(*) FILTER (WHERE event = 'sighted')            AS sightings,
         count(DISTINCT speaker_ref)
           FILTER (WHERE event = 'sighted'
                     AND speaker_ref IS NOT NULL)             AS speakers,
         min(occurred_at) FILTER (WHERE event = 'sighted')    AS first_seen,
         max(occurred_at) FILTER (WHERE event = 'sighted')    AS last_seen
  FROM task_candidate_events
  GROUP BY candidate_id, user_ref
)
SELECT l.candidate_id, l.user_ref, l.title,
       COALESCE(d.event, l.event) AS state,
       c.sightings, c.speakers, c.first_seen, c.last_seen
FROM latest l
JOIN counts c USING (candidate_id, user_ref)
LEFT JOIN decided d USING (candidate_id, user_ref);

COMMENT ON VIEW v_task_candidates IS
  'Current state per candidate. A decision (promoted/dismissed/accepted) '
  'outranks a later sighting: mentioning work again is not a reason to '
  'reopen a question already answered. Counts are all-time; the promotion '
  'window is applied by the reader (board_io_pg.load_candidates).';

COMMIT;
