-- =========================================================================
-- Migration: manual_note -- work stated directly, outside any meeting.
-- Apply with:  psql -d taskintel -f storage/migrate_manual_note.sql
--
-- The biggest modeling hole was work that never passes through a
-- meeting: "waiting on IT for VPN access" gets typed nowhere and so
-- exists nowhere. Quick capture fixes that -- one line, typed by the
-- person, entering the SAME pool, gates, corroboration and review as
-- everything else. Only extraction is skipped, deliberately: there is
-- no transcript to comprehend; the typed sentence IS the claim, its
-- author IS the speaker, and running a model over it could only
-- paraphrase a person to themselves.
--
-- It needs its own source type in two CHECKs, because labeling a typed
-- note as a 'document' would be a lie in the provenance rail -- and
-- the provenance rail being incapable of lying is the product.
-- =========================================================================

BEGIN;

DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'extraction_batches'::regclass
             AND pg_get_constraintdef(oid) LIKE '%source_type%' LOOP
    EXECUTE format('ALTER TABLE extraction_batches DROP CONSTRAINT %I',
                   c.conname);
  END LOOP;
  ALTER TABLE extraction_batches
    ADD CONSTRAINT extraction_batches_source_type_check
    CHECK (source_type IN
      ('teams_channel','teams_chat','email_thread','meeting_transcript',
       'meeting_recap','copilot_claims','document','manual_note'));
  FOR c IN SELECT conname FROM pg_constraint
           WHERE conrelid = 'evidence_refs'::regclass
             AND pg_get_constraintdef(oid) LIKE '%source_type%' LOOP
    EXECUTE format('ALTER TABLE evidence_refs DROP CONSTRAINT %I',
                   c.conname);
  END LOOP;
  ALTER TABLE evidence_refs
    ADD CONSTRAINT evidence_refs_source_type_check
    CHECK (source_type IN
      ('teams_message','email_message','transcript_utterance',
       'document_span','meeting_insight','assistant_claim',
       'manual_note'));
END $$;

COMMIT;
