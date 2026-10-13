-- =========================================================================
-- Migration: 'worked' -- hours actually spent, stated per subject per week.
-- Apply with:  psql -d taskintel -f storage/migrate_worked.sql
--
-- The sheet planned but never recorded: "I worked 5 hours on this" (the
-- pilot's first reviewer, verbatim) had nowhere to land, so the end-of-
-- week column was always a projection and never a fact. 'worked' is a
-- stated number like every other stated number -- typed by the person,
-- append-only, latest wins -- and when it exists the end-of-week total
-- is arithmetic on what HAPPENED rather than what was intended.
-- =========================================================================
BEGIN;
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'workload_events'::regclass
             AND pg_get_constraintdef(oid) LIKE '%kind%' LOOP
    EXECUTE format('ALTER TABLE workload_events DROP CONSTRAINT %I',
                   c.conname);
  END LOOP;
  ALTER TABLE workload_events ADD CONSTRAINT workload_events_kind_check
    CHECK (kind IN ('capacity','opening_total','planned','priority',
                    'meeting_time','worked'));
  ALTER TABLE workload_events ADD CONSTRAINT workload_events_prio_chk
    CHECK ((kind = 'priority') = (priority IS NOT NULL));
  ALTER TABLE workload_events ADD CONSTRAINT workload_events_hours_chk
    CHECK ((kind <> 'priority') = (hours IS NOT NULL));
END $$;
COMMIT;
