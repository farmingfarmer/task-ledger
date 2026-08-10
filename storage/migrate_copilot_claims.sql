-- =========================================================================
-- Migration: ingest Copilot's own structured claims as a source.
--     psql -d taskintel -f storage/migrate_copilot_claims.sql
-- Fresh databases get it from schema.sql and need nothing here.
--
-- Adds:
--   extraction_batches.source_type  'copilot_claims'
--   evidence_refs.source_type       'assistant_claim'
--
-- WHY A NEW EVIDENCE TYPE, and why this is the important half.
--
-- Copilot will hand over per-person task claims already structured --
-- person, task, status, progress, blockers, next_action -- and each record
-- carries "evidence_strength": "transcript_supported" plus a list of
-- MEETING NAMES. That is an assertion that evidence exists. It is not
-- evidence: there is no message id, no utterance, no span, and often two
-- meetings named for one claim with no way to know which sentence in which.
-- The assistant that produced it declines to release the transcript, so the
-- citation cannot be checked by the person reading it or by anyone they
-- show it to.
--
-- Every other evidence type in this table points at something a person can
-- open and re-read. Reusing one of them would let a claim inherit that
-- promise without earning it, and "trace to source" would land on a
-- paraphrase whose source is unavailable. 'assistant_claim' exists so the
-- provenance rail can say the true thing: an assistant asserted this, the
-- assertion itself is hashed and tamper-evident, and the sentence behind it
-- was never released.
--
-- The content hash on these rows is over the exported record, not over an
-- utterance. It makes re-ingestion idempotent and makes a silently edited
-- export detectable -- which is what a hash can honestly do here.
-- =========================================================================

BEGIN;

ALTER TABLE extraction_batches
  DROP CONSTRAINT IF EXISTS extraction_batches_source_type_check;

ALTER TABLE extraction_batches
  ADD CONSTRAINT extraction_batches_source_type_check
  CHECK (source_type IN
    ('teams_channel','teams_chat','email_thread',
     'meeting_transcript','meeting_recap','copilot_claims','document'));

ALTER TABLE evidence_refs
  DROP CONSTRAINT IF EXISTS evidence_refs_source_type_check;

ALTER TABLE evidence_refs
  ADD CONSTRAINT evidence_refs_source_type_check
  CHECK (source_type IN
    ('teams_message','email_message','transcript_utterance',
     'document_span','meeting_insight','assistant_claim'));

COMMENT ON COLUMN evidence_refs.source_type IS
  'What the pointer points AT. Every value but assistant_claim resolves to '
  'something a viewer can open under their own permissions. assistant_claim '
  'is an assertion by an assistant that evidence exists, hashed so the '
  'assertion cannot be edited unnoticed, but with no retrievable source: '
  'the UI must never offer it as a trace-to-source.';

COMMIT;
