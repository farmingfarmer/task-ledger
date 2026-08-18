-- =========================================================================
-- Migration: signals.author_ref -- who SAID a claim.
-- Apply to any database created before this column existed:
--     psql -d taskintel -f storage/migrate_author_ref.sql
-- Fresh databases get it from schema.sql and need nothing here.
--
-- Why it is separate from owner_candidates: owner is who the work is FOR,
-- author is who reported it. "Priya said Dev finished the docs" has author
-- Priya and owner Dev, and the corroboration rule in matching_policy.yaml
-- has always been written in terms of "different evidence resource_ids AND
-- different speakers/authors" -- the speaker half was unenforceable until
-- now, because the data was not stored.
--
-- ON BACKFILL, and why there isn't one: `signals` is one of the ten
-- append-only tables, guarded by forbid_mutation() on UPDATE and DELETE.
-- Backfilling historical rows would mean UPDATE, which the trigger refuses
-- BY DESIGN -- an extraction record is what the extractor said at the time,
-- and quietly enriching it later would make the ledger's own history a
-- thing that changes. Pre-migration signals therefore keep author_ref NULL
-- forever and the UI reports them as "speaker not recorded".
--
-- CORRECTION. This comment used to end: "Re-extracting a window is the
-- supported way to get the field populated: content-hash ids make that an
-- upsert-shaped no-op everywhere else." That was wrong about this repo's
-- own code, and it cost a day of chasing. signal_id deliberately excludes
-- author_ref, so a replay yields identical ids -- and write_batch inserts
-- ON CONFLICT (signal_id) DO NOTHING, then skips the row on rowcount 0, so
-- the newly extracted author is thrown away. Switching that to DO UPDATE
-- does not help either: ON CONFLICT DO UPDATE fires this very trigger.
-- There was no route from a null author to a filled one, which left the
-- breadth half of candidate_promotion unreachable for every signal written
-- before the column existed -- dead data rather than dead code, and 149 of
-- 174 signals on the first real corpus.
--
-- The route now is storage/migrate_author_backfill.sql, which narrows
-- forbid_mutation to permit exactly one thing (NULL -> value on this
-- column, every other column byte-identical), plus
-- scripts/backfill_author.py, which reads the authors back out of the
-- transcripts in the processed inbox. No re-extraction, and no model:
-- authorship comes from the window item and never from the LLM, so
-- replaying a window through Ollama could only reproduce what the
-- transcript already says.
-- =========================================================================

BEGIN;

ALTER TABLE signals ADD COLUMN IF NOT EXISTS author_ref text;

COMMENT ON COLUMN signals.author_ref IS
  'Speaker of the primary cited evidence, resolved through the directory at '
  'extraction time. NULL for signals extracted before this column existed '
  '(append-only: no backfill). Not part of signal_id.';

-- Signals whose author is known, per batch: the quickest way to see how
-- much of the pool predates the migration.
CREATE INDEX IF NOT EXISTS signals_author_idx ON signals(author_ref)
  WHERE author_ref IS NOT NULL;

-- v_unconsumed_signals is defined as `SELECT s.*`, and Postgres expands
-- that star ONCE, when the view is created. A column added to signals
-- afterwards therefore never appears in the view: a fresh database built
-- from schema.sql has author_ref there, a migrated one silently does not,
-- and the first consumer to select it fails with UndefinedColumn. The view
-- has to be rebuilt, and CREATE OR REPLACE cannot do it -- the new column
-- lands mid-list, and replace may only append. Dropping is safe: a view
-- holds no data, and nothing else depends on this one.
DROP VIEW IF EXISTS v_unconsumed_signals;
CREATE VIEW v_unconsumed_signals AS
SELECT s.*, le.user_ref
FROM (
  SELECT DISTINCT ON (signal_id, user_ref) signal_id, user_ref, event
  FROM signal_pool_events
  ORDER BY signal_id, user_ref, event_id DESC
) le
JOIN signals s USING (signal_id)
WHERE le.event IN ('pooled','returned');

COMMIT;
