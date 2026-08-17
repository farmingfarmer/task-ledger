-- =========================================================================
-- Migration: the candidate register -- work heard about, not yet a task.
--     psql -d taskintel -f storage/migrate_task_candidates.sql
-- Fresh databases get this from schema.sql.
--
-- WHY. A task_mention that matched an existing task contributed an alias.
-- A task_mention that matched NOTHING returned None and was consumed --
-- discarded with no record it was ever heard. On the first real corpus that
-- was 87 of 134 signals: every reference to work that was not already on
-- somebody's board, deleted. The system could only ever learn about work
-- phrased as an assignment on a day someone happened to be reviewing.
--
-- A mention is deliberately not a task: one passing reference is noise, and
-- turning each into a card is how a board becomes unusable. So mentions
-- accumulate here instead, and a candidate becomes a PROPOSAL -- never a
-- board fact -- once corroborated the same way closures are: seen more than
-- once, by more than one person, inside a window.
--
-- Event-sourced for the same reason the signal pool is: "we heard this
-- again" and "somebody said it is not a task" are different facts and both
-- belong in the record. v_task_candidates projects current state, exactly
-- as v_unconsumed_signals does over signal_pool_events.
-- =========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS task_candidate_events (
  event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  candidate_id text NOT NULL,          -- content hash of the normalised title
  user_ref     text NOT NULL,          -- whose pool the sighting arrived in
  event        text NOT NULL CHECK (event IN
                 ('sighted','promoted','dismissed','accepted')),
  title        text NOT NULL,
  signal_id    text REFERENCES signals(signal_id),   -- null for state events
  speaker_ref  text,                   -- who said it; corroboration counts these
  run_id       text,
  occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS candidate_events_user_idx
  ON task_candidate_events(user_ref, candidate_id);

COMMENT ON TABLE task_candidate_events IS
  'Work referenced in conversation that is not on any board yet. Append-only; '
  'v_task_candidates is the projection. A candidate is NOT a task and never '
  'becomes one without a human decision -- promotion only puts it in a packet.';

-- Current state per candidate, plus the corroboration counts the promotion
-- rule reads. Distinct SPEAKERS, not distinct sightings: one person saying
-- the same thing in four meetings is one person's opinion, and that is the
-- same standard the closure gates already hold.
CREATE OR REPLACE VIEW v_task_candidates AS
WITH latest AS (
  SELECT DISTINCT ON (candidate_id, user_ref)
         candidate_id, user_ref, event, title
  FROM task_candidate_events
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
SELECT l.candidate_id, l.user_ref, l.title, l.event AS state,
       c.sightings, c.speakers, c.first_seen, c.last_seen
FROM latest l
JOIN counts c USING (candidate_id, user_ref);

COMMENT ON VIEW v_task_candidates IS
  'Latest-event-wins projection of task_candidate_events, with the sighting '
  'and distinct-speaker counts the promotion rule in matching_policy.yaml '
  'reads.';

-- Append-only, like every other evidence table here.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
                 WHERE tgname = 'task_candidate_events_append_only') THEN
    CREATE TRIGGER task_candidate_events_append_only
      BEFORE UPDATE OR DELETE ON task_candidate_events
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
  END IF;
END $$;

COMMIT;
