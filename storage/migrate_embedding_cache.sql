-- =========================================================================
-- Migration: remember what a sentence embeds to.
--     psql -d taskintel -f storage/migrate_embedding_cache.sql
-- Fresh databases get this from schema.sql.
--
-- WHY. An embedding is a pure function of (model, text), and the texts
-- this system embeds barely change: every tick re-embeds every task
-- title, every alias, and every standing candidate for every user --
-- six embed call sites per reconcile, times eleven users, times a tick
-- an hour, on a CPU. The vectors were identical every time and thrown
-- away every time. The schema even reserved a column for this
-- ('embedding vector(3072)  -- enable with pgvector') that nothing ever
-- enabled.
--
-- Keyed by (model, sha) because an embedding is only reusable under the
-- model that made it -- a swapped embed model must miss the whole cache,
-- not poison it. Plain jsonb, no pgvector dependency: this is a
-- memoisation table, not a similarity index; cosine still happens in
-- Python where it always did.
-- =========================================================================
BEGIN;
CREATE TABLE IF NOT EXISTS embedding_cache (
  model      text NOT NULL,
  text_sha   text NOT NULL,
  vec        jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (model, text_sha)
);
COMMENT ON TABLE embedding_cache IS
  'Memoised embeddings: pure function of (model, text), so computing one '
  'twice is pure waste. Safe to TRUNCATE at any time; it only costs the '
  'next tick some model calls.';
COMMIT;
