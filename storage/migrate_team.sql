-- =========================================================================
-- Migration: the consented team layer.
-- Apply with:  psql -d taskintel -f storage/migrate_team.sql
--
-- Two additions, one principle: nothing about a person becomes visible
-- to anyone except through a grant they made themselves, or a fact that
-- already crosses boards (a dependency edge both sides can see).
--
-- 1. A third grant level, 'workload'. 'progress' and 'full_trace' share
--    a BOARD; neither should quietly imply "and my hours too". Hours
--    are the most scorecard-shaped data this system holds, so sharing
--    them is its own explicit act, grantable and revocable exactly like
--    the others, through the same grant_access path with the same
--    only-you-may-share-your-own rule.
--
-- 2. crossboard_rulings: two open tasks on different boards that look
--    like the same work get a card asking both owners "same work, or
--    genuinely two?" -- and the answer has to be REMEMBERED, or the
--    card re-asks forever. Append-only like everything else; the latest
--    ruling on a pair wins; either owner's ruling settles it for both.
-- =========================================================================

BEGIN;

DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'access_grant_events'::regclass
             AND pg_get_constraintdef(oid) LIKE '%level%' LOOP
    EXECUTE format('ALTER TABLE access_grant_events DROP CONSTRAINT %I',
                   c.conname);
  END LOOP;
  ALTER TABLE access_grant_events
    ADD CONSTRAINT access_grant_events_level_check
    CHECK (level IN ('progress','full_trace','workload'));
END $$;

CREATE TABLE IF NOT EXISTS crossboard_rulings (
  ruling_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_a     text NOT NULL,     -- least(task ids): one row per pair
  task_b     text NOT NULL,     -- greatest(task ids)
  ruling     text NOT NULL CHECK (ruling IN ('same','distinct')),
  ruled_by   text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (task_a < task_b)
);
CREATE INDEX IF NOT EXISTS crossboard_pair_idx
  ON crossboard_rulings (task_a, task_b, ruling_id DESC);

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
                 WHERE tgname = 'crossboard_rulings_append_only') THEN
    CREATE TRIGGER crossboard_rulings_append_only
      BEFORE UPDATE OR DELETE ON crossboard_rulings
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
  END IF;
END $$;

COMMIT;
