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
-- forever and the UI reports them as "speaker not recorded". Re-extracting
-- a window is the supported way to get the field populated: content-hash
-- ids make that an upsert-shaped no-op everywhere else.
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

COMMIT;
