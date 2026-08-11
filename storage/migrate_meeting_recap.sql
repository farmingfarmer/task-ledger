-- =========================================================================
-- Migration: allow source_type 'meeting_recap' on extraction batches.
--     psql -d taskintel -f storage/migrate_meeting_recap.sql
-- Fresh databases get it from schema.sql and need nothing here.
--
-- WHY A SIXTH SOURCE TYPE rather than reusing 'document'. Some tenants will
-- not release meeting transcripts, and what Copilot will hand over instead
-- is a recap: subjects grouped under the people who raised them, with no
-- utterances, no timestamps and no commitment language. That is a genuinely
-- different kind of input and it has to be labelled as one, for two
-- reasons.
--
-- The first is provenance. Evidence in this system is a pointer to the
-- thing that was said; a recap bullet is a pointer to a paraphrase of
-- several things somebody said. evidence_refs already carries
-- 'meeting_insight' for exactly this, and keeping recaps on their own
-- source type is what routes them there instead of to
-- 'transcript_utterance', which would claim a sentence-level receipt the
-- data cannot support.
--
-- The second is that 'document' is an AUTHORED source in the extraction
-- prompt, where a sender describing work impersonally is taken to own it.
-- A recap has no sender -- it is machine-written about other people -- so
-- that rule would attribute everyone's work to nobody in particular, or
-- worse, consistently to whoever the summariser named first.
--
-- Nothing is backfilled: batches already stored under another source type
-- keep it. `extraction_batches` is append-only and re-extracting a window
-- is an upsert-shaped no-op, so relabelling history is neither possible
-- nor wanted.
-- =========================================================================

-- RE-RUNNABLE ON A NEWER DATABASE. The list below deliberately includes
-- copilot_claims, which arrived in a later migration. Without it, applying
-- this file to a database that already holds copilot_claims batches fails
-- with "constraint is violated by some row" -- a migration that corrupts a
-- database which has moved past it is a trap, and ordering discipline is a
-- weaker guarantee than simply being a superset.

BEGIN;

ALTER TABLE extraction_batches
  DROP CONSTRAINT IF EXISTS extraction_batches_source_type_check;

ALTER TABLE extraction_batches
  ADD CONSTRAINT extraction_batches_source_type_check
  CHECK (source_type IN
    ('teams_channel','teams_chat','email_thread',
     'meeting_transcript','meeting_recap','copilot_claims','document'));

COMMENT ON COLUMN extraction_batches.source_type IS
  'Channel the window came from. meeting_recap is a summary ABOUT a '
  'conversation rather than the conversation: its evidence is '
  'meeting_insight, never transcript_utterance, and the extraction prompt '
  'forbids it from producing state changes it cannot have witnessed.';

COMMIT;
