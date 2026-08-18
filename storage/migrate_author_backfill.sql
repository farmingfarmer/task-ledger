-- =========================================================================
-- Migration: let a null author be filled in, once, and nothing else.
--     psql -d taskintel -f storage/migrate_author_backfill.sql
-- Fresh databases get this from schema.sql.
--
-- WHY. migrate_author_ref.sql added signals.author_ref and deliberately
-- shipped no backfill: signals is append-only, "an extraction record is what
-- the extractor said at the time, and quietly enriching it later would make
-- the ledger's own history a thing that changes." It named re-extraction as
-- the supported route, on the grounds that content-hash ids make a replay
-- "upsert-shaped".
--
-- They are not upsert-shaped. write_batch inserts ON CONFLICT (signal_id)
-- DO NOTHING and skips the row on rowcount 0, so replaying a window fills
-- nothing -- and switching it to DO UPDATE would fire this very trigger.
-- The documented escape hatch has no exit. On the first real corpus that
-- left 149 of 174 signals with no author FOREVER, and with them the breadth
-- half of candidate_promotion: v_task_candidates counts DISTINCT
-- speaker_ref, so min_distinct_speakers could never be met by any signal
-- extracted before the column existed. Half the promotion rule was dead
-- data, not dead code, and no amount of re-running would revive it.
--
-- So the invariant is narrowed rather than removed, to the smallest shape
-- that fixes it:
--
--   * one column per table -- signals.author_ref, and speaker_ref on
--     task_candidate_events, which copies it and is otherwise stranded the
--     same way;
--   * NULL -> NOT NULL only. A recorded author can never be changed and
--     never be erased. The fill is monotonic, so it is order-independent
--     and cannot be walked backwards;
--   * every other column must be byte-identical, compared as jsonb so a
--     column added later is covered without touching this function.
--
-- What that protects is the thing the original comment cared about: no
-- claim, title, confidence or piece of evidence can be rewritten after the
-- fact. What it permits is filling a hole that existed only because the
-- field had not been invented yet -- from the transcripts still on disk,
-- which are the same immutable source the extractor read. The ledger's
-- history does not change; a gap in it closes.
--
-- Comparing through to_jsonb also means this function stays safe on the
-- eight tables that have neither column: they match no CASE arm, so the
-- exemption is never even evaluated for them.
-- =========================================================================

BEGIN;

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
DECLARE
  fillable text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    fillable := CASE TG_TABLE_NAME
                  WHEN 'signals'              THEN 'author_ref'
                  WHEN 'task_candidate_events' THEN 'speaker_ref'
                  ELSE NULL
                END;
    IF fillable IS NOT NULL
       AND to_jsonb(OLD) ->> fillable IS NULL
       AND to_jsonb(NEW) ->> fillable IS NOT NULL
       AND (to_jsonb(NEW) - fillable) = (to_jsonb(OLD) - fillable)
    THEN
      RETURN NEW;
    END IF;
  END IF;
  RAISE EXCEPTION '% is append-only; % not permitted', TG_TABLE_NAME, TG_OP;
END $$ LANGUAGE plpgsql;

COMMENT ON FUNCTION forbid_mutation() IS
  'Append-only guard for the evidence tables. Permits exactly one mutation: '
  'filling signals.author_ref or task_candidate_events.speaker_ref from NULL, '
  'with every other column unchanged. DELETE is never permitted, a recorded '
  'value is never overwritten, and no other column may move.';

COMMIT;
