-- =========================================================================
-- Migration: the workload sheet -- hours and priority, per person per week.
-- Apply with:  psql -d taskintel -f storage/migrate_workload.sql
--
-- This backs the panel that answers "what am I spending my week on":
-- per project, a priority, cumulative hours, this week's plan, and the
-- projected end-of-week total. The design constraint the whole table
-- exists to enforce: THREE KINDS OF NUMBER, NEVER BLENDED.
--
--   stated    the person typed it: weekly capacity, an opening total,
--             a priority they chose, a plan they edited.
--   measured  the system computed it from evidence it can show: meeting
--             minutes summed from transcript window timestamps.
--   proposed  the system suggested it and NOBODY has agreed yet.
--
-- Proposals are never written here at all -- they are recomputed on
-- every read and die on the screen unless accepted, exactly like review
-- cards. Only stated and measured numbers are worth persisting, and a
-- measured number records what it was measured FROM.
--
-- Append-only like everything else: a correction is a new row, the
-- latest row per (person, subject, week, kind) wins, and how a number
-- changed over the weeks is itself a record worth keeping.
-- =========================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS workload_events (
  event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_ref    text NOT NULL,
  -- What the row is about: a project_id, a task_id (top-level parents
  -- act as projects), '_capacity' for the person's whole week, or
  -- '_other' for the unassigned remainder.
  subject     text NOT NULL,
  week_start  date NOT NULL,               -- always a Monday
  kind        text NOT NULL CHECK (kind IN
                ('capacity',        -- stated: hours available this week
                 'opening_total',   -- stated: cumulative hours before this week
                 'planned',         -- stated: hours the person will spend
                 'priority',        -- stated: 1 = most important
                 'meeting_time')),  -- measured: minutes, from transcripts
  hours       numeric(6,2),
  priority    smallint CHECK (priority BETWEEN 1 AND 99),
  -- 'stated' rows come from the person; 'measured' rows carry the
  -- source_refs they were computed from, so the number can be audited.
  source      text NOT NULL CHECK (source IN ('stated','measured')),
  basis       text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  CHECK ((kind = 'priority') = (priority IS NOT NULL)),
  CHECK ((kind <> 'priority') = (hours IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS workload_latest_idx
  ON workload_events (user_ref, subject, week_start, kind, event_id DESC);

-- Latest row per (person, subject, week, kind): the current sheet.
CREATE OR REPLACE VIEW v_workload_current AS
SELECT DISTINCT ON (user_ref, subject, week_start, kind)
       user_ref, subject, week_start, kind, hours, priority, source,
       basis, created_at
FROM workload_events
ORDER BY user_ref, subject, week_start, kind, event_id DESC;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
                 WHERE tgname = 'workload_events_append_only') THEN
    CREATE TRIGGER workload_events_append_only
      BEFORE UPDATE OR DELETE ON workload_events
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
  END IF;
END $$;

COMMIT;
