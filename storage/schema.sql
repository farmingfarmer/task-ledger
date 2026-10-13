-- =========================================================================
-- Task Intelligence storage schema (PostgreSQL 14+)
-- Companion to CONTRACTS.md / MATCHING_POLICY.md / REVIEW_LOOP.md
--
-- Design rules enforced structurally:
--   1. Append-only history. extraction_batches, signals, evidence_refs,
--      review_packets, proposals, decisions, task_events, packet_state_events,
--      and signal_pool_events are INSERT-only (enforced by trigger).
--   2. Single write path to the board. Every board mutation is a task_events
--      row; tasks_current and task_blockers are write-through projections
--      maintained in the same transaction, and rebuild_board() reconstructs
--      them from events alone (run in CI to prove the projection is honest).
--   3. Evidence is pointers. evidence_refs stores Graph resource ids, spans,
--      and content hashes -- never message text.
--   4. Full lineage: task_events.decision_id -> decisions.proposal_id ->
--      proposal_signals -> signals -> evidence_refs. Manual dashboard edits
--      write task_events with actor_type='manual_edit' and NULL decision_id,
--      so "human typed it" is as auditable as "model proposed it".
-- =========================================================================

BEGIN;

-- ------------------------------------------------------------- enum domains

CREATE TABLE ref_signal_types (v text PRIMARY KEY);
INSERT INTO ref_signal_types VALUES
  ('task_mention'), ('assignment'), ('status_update'), ('completion_claim'),
  ('deadline_signal'), ('blocker'), ('blocker_resolved'), ('proposal'),
  ('cancellation');

CREATE TABLE ref_ops (v text PRIMARY KEY);
INSERT INTO ref_ops VALUES
  ('create_task'), ('add_subtask'), ('update_status'), ('update_deadline'),
  ('reassign'), ('add_blocker'), ('resolve_blocker'), ('complete_task'),
  ('cancel_task'), ('merge_tasks');

CREATE TABLE ref_task_statuses (v text PRIMARY KEY);
INSERT INTO ref_task_statuses VALUES
  ('proposed'), ('in_progress'), ('blocked'), ('completed'), ('cancelled');

-- --------------------------------------------------------------- extraction

CREATE TABLE extraction_batches (
  batch_id        text PRIMARY KEY,          -- content hash (idempotent re-runs)
  extracted_at    timestamptz NOT NULL,
  extractor_model text NOT NULL,
  prompt_version  text NOT NULL,
  source_type     text NOT NULL CHECK (source_type IN
                    ('teams_channel','teams_chat','email_thread',
                     'meeting_transcript','meeting_recap',
                     'copilot_claims','document',
                     'manual_note')),   -- typed by a person, no meeting
  source_ref      text NOT NULL,
  window_start    timestamptz NOT NULL,
  window_end      timestamptz NOT NULL,
  CHECK (window_end >= window_start)
);

CREATE TABLE signals (
  signal_id       text PRIMARY KEY,          -- content hash
  batch_id        text NOT NULL REFERENCES extraction_batches(batch_id),
  signal_type     text NOT NULL REFERENCES ref_signal_types(v),
  title           text NOT NULL CHECK (char_length(title) <= 120),
  description     text,
  status_signal   text REFERENCES ref_task_statuses(v),
  confidence      numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  deadline_date   date,
  deadline_precision text CHECK (deadline_precision IN
                    ('exact','week','month','quarter','vague')),
  deadline_raw    text,
  blocker_desc    text,
  blocker_party   text,
  author_ref      text,                      -- who SAID it (speaker of the
                                             -- primary evidence); owner_
                                             -- candidates is who it is FOR
  owner_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
  raw             jsonb NOT NULL             -- full ExtractedSignal as emitted
);
CREATE INDEX signals_batch_idx ON signals(batch_id);
CREATE INDEX signals_type_idx  ON signals(signal_type);

CREATE TABLE evidence_refs (
  signal_id     text NOT NULL REFERENCES signals(signal_id),
  ordinal       smallint NOT NULL,
  source_type   text NOT NULL CHECK (source_type IN
                  ('teams_message','email_message','transcript_utterance',
                   'document_span','meeting_insight','assistant_claim',
                   'manual_note')),
  resource_id   text NOT NULL,               -- Graph id; pointer, never a copy
  span_start    integer,
  span_end      integer,
  content_hash  text,
  PRIMARY KEY (signal_id, ordinal),
  CHECK (span_end IS NULL OR span_start IS NULL OR span_end >= span_start)
);

-- Workload sheet: hours and priority per person per week.
-- Full rationale in migrate_workload.sql (which existing
-- databases apply); the short version: three kinds of
-- number -- stated, measured, proposed -- and only the
-- first two are ever stored.


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
                 'meeting_time',    -- measured: minutes, from transcripts
                 'worked')),        -- stated: hours actually spent
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
-- (append-only trigger attached in the loop below,
--  after forbid_mutation() exists)

-- Cross-board duplicate rulings: "same work, or genuinely two?" --
-- answered once by either owner, remembered forever. Rationale in
-- migrate_team.sql.
CREATE TABLE IF NOT EXISTS crossboard_rulings (
  ruling_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_a     text NOT NULL,
  task_b     text NOT NULL,
  ruling     text NOT NULL CHECK (ruling IN ('same','distinct')),
  ruled_by   text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (task_a < task_b)
);
CREATE INDEX IF NOT EXISTS crossboard_pair_idx
  ON crossboard_rulings (task_a, task_b, ruling_id DESC);

-- Signal pool: which signals are awaiting reconciliation for which user.
-- Append-only events; current pool is the latest event per (signal, user).
CREATE TABLE signal_pool_events (
  event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  signal_id   text NOT NULL REFERENCES signals(signal_id),
  user_ref    text NOT NULL,
  event       text NOT NULL CHECK (event IN ('pooled','taken','returned')),
  occurred_at timestamptz NOT NULL DEFAULT now(),
  run_id      text                            -- cadence run that took/returned
);
CREATE INDEX pool_signal_user_idx ON signal_pool_events(signal_id, user_ref, event_id DESC);

CREATE VIEW v_unconsumed_signals AS
SELECT s.*, le.user_ref
FROM (
  SELECT DISTINCT ON (signal_id, user_ref) signal_id, user_ref, event
  FROM signal_pool_events
  ORDER BY signal_id, user_ref, event_id DESC
) le
JOIN signals s USING (signal_id)
WHERE le.event IN ('pooled','returned');

-- ------------------------------------------------------------------ packets

CREATE TABLE review_packets (
  packet_id      text PRIMARY KEY,
  user_ref       text NOT NULL,
  cadence_run_id text NOT NULL,
  board_version  bigint NOT NULL,
  generated_at   timestamptz NOT NULL,
  expires_at     timestamptz
);
CREATE INDEX packets_user_idx ON review_packets(user_ref, generated_at DESC);

CREATE TABLE packet_state_events (
  event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  packet_id   text NOT NULL REFERENCES review_packets(packet_id),
  state       text NOT NULL CHECK (state IN
                ('draft','delivered','partially_decided','closed',
                 'expired','superseded')),
  occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE proposals (
  proposal_id    text PRIMARY KEY,
  packet_id      text NOT NULL REFERENCES review_packets(packet_id),
  op             text NOT NULL REFERENCES ref_ops(v),
  target_task_id text,                        -- FK added after tasks_current
  rationale      text NOT NULL CHECK (char_length(rationale) <= 200),
  confidence     numeric(4,3) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  conflict_type  text CHECK (conflict_type IN
                   ('contradicts_manual_edit','contradicts_prior_decision',
                    'competing_owner','stale_evidence')),
  conflict_detail text,
  payload        jsonb NOT NULL,
  CHECK ((conflict_type IS NULL) = (conflict_detail IS NULL))
);
CREATE INDEX proposals_packet_idx ON proposals(packet_id);
CREATE INDEX proposals_target_idx ON proposals(target_task_id)
  WHERE target_task_id IS NOT NULL;

CREATE TABLE proposal_signals (
  proposal_id text NOT NULL REFERENCES proposals(proposal_id),
  signal_id   text NOT NULL REFERENCES signals(signal_id),
  PRIMARY KEY (proposal_id, signal_id)
);

CREATE TABLE decisions (
  decision_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  proposal_id    text NOT NULL REFERENCES proposals(proposal_id),
  packet_id      text NOT NULL REFERENCES review_packets(packet_id),
  decision       text NOT NULL CHECK (decision IN
                   ('accept','accept_edited','reject','defer')),
  reject_reason  text CHECK (reject_reason IN
                   ('not_a_task','duplicate','wrong_task_match','wrong_status',
                    'wrong_owner','bad_date_resolution','stale','other')),
  reject_note    text,
  edited_payload jsonb,
  decided_by     text NOT NULL,     -- authenticated identity, never card data
  decided_at     timestamptz NOT NULL,
  invalidated_reason text,          -- set when optimistic concurrency blocked apply
  UNIQUE (proposal_id, decided_by), -- idempotency anchor
  CHECK (decision <> 'reject' OR reject_reason IS NOT NULL),
  CHECK (decision =  'accept_edited' OR edited_payload IS NULL)
);
CREATE INDEX decisions_packet_idx ON decisions(packet_id);

-- -------------------------------------------------------------------- board

-- The event log: the ONLY source of truth for board state.
CREATE TABLE task_events (
  event_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id       text NOT NULL,
  user_ref      text NOT NULL,               -- board owner
  board_version bigint NOT NULL,             -- per-user monotonic, assigned by writer
  event_type    text NOT NULL CHECK (event_type IN
                  ('created','status_changed','deadline_changed','reassigned',
                   'blocker_added','blocker_resolved','completed','cancelled',
                   'reopened','merged_into','alias_added','subtask_linked',
                   'annotated')),
  -- auto_applied: board state changed with no human approval, under the
  -- narrow auto_apply gate in matching_policy.yaml (off by default). Its
  -- own value rather than 'system' so "how much of this board did nobody
  -- confirm?" stays a question with an answer.
  actor_type    text NOT NULL CHECK (actor_type IN
                  ('user_decision','manual_edit','system','auto_applied')),
  decision_id   bigint REFERENCES decisions(decision_id),
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (actor_type <> 'user_decision' OR decision_id IS NOT NULL),
  UNIQUE (user_ref, board_version)           -- versions never fork
);
CREATE INDEX task_events_task_idx ON task_events(task_id, event_id);
CREATE INDEX task_events_user_idx ON task_events(user_ref, board_version);

CREATE TABLE user_boards (
  user_ref text PRIMARY KEY,
  version  bigint NOT NULL DEFAULT 0
);

-- Write-through projection of current state. Mutable BY DESIGN -- it is a
-- cache of task_events, rebuildable at any time by rebuild_board().
CREATE TABLE tasks_current (
  task_id        text PRIMARY KEY,
  user_ref       text NOT NULL,
  title          text NOT NULL,
  description    text,
  status         text NOT NULL REFERENCES ref_task_statuses(v),
  owner_ref      text,
  deadline       date,
  parent_task_id text REFERENCES tasks_current(task_id)
                   DEFERRABLE INITIALLY DEFERRED,
  merged_into    text REFERENCES tasks_current(task_id)
                   DEFERRABLE INITIALLY DEFERRED,
  created_at     timestamptz NOT NULL,
  updated_at     timestamptz NOT NULL,
  last_event_id  bigint NOT NULL
);
CREATE INDEX tasks_user_status_idx ON tasks_current(user_ref, status);
CREATE INDEX tasks_parent_idx ON tasks_current(parent_task_id)
  WHERE parent_task_id IS NOT NULL;

ALTER TABLE proposals ADD CONSTRAINT proposals_target_fk
  FOREIGN KEY (target_task_id) REFERENCES tasks_current(task_id)
  DEFERRABLE INITIALLY DEFERRED;   -- deferred: create_task proposals precede rows

CREATE TABLE task_aliases (
  task_id   text NOT NULL REFERENCES tasks_current(task_id)
              DEFERRABLE INITIALLY DEFERRED,
  alias     text NOT NULL,
  source_signal_id text REFERENCES signals(signal_id),
  added_at  timestamptz NOT NULL DEFAULT now(),
  -- embedding vector(3072),  -- enable with pgvector for alias matching
  PRIMARY KEY (task_id, alias)
);

CREATE TABLE task_blockers (
  blocker_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_id        text NOT NULL REFERENCES tasks_current(task_id)
                   DEFERRABLE INITIALLY DEFERRED,
  description    text NOT NULL,
  blocking_party text,
  blocking_task_id text REFERENCES tasks_current(task_id)
                     DEFERRABLE INITIALLY DEFERRED,          -- the dependency edge
  opened_event_id  bigint NOT NULL,
  resolved_event_id bigint                                 -- NULL = open
);
CREATE INDEX blockers_open_idx ON task_blockers(task_id)
  WHERE resolved_event_id IS NULL;
CREATE INDEX blockers_edge_idx ON task_blockers(blocking_task_id)
  WHERE blocking_task_id IS NOT NULL AND resolved_event_id IS NULL;

-- Memoised embeddings: a pure function of (model, text), recomputed
-- hundreds of times per tick before this table existed. Keyed by model so
-- a swapped embedder misses rather than poisons. Safe to TRUNCATE; it
-- only costs the next tick some model calls.
CREATE TABLE embedding_cache (
  model      text NOT NULL,
  text_sha   text NOT NULL,
  vec        jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (model, text_sha)
);

-- --------------------------------------------------- append-only enforcement

-- Append-only, with exactly one exemption: a NULL author may be filled in.
-- signals.author_ref and task_candidate_events.speaker_ref were added after
-- the rows that need them were written, and re-extraction cannot supply the
-- value (write_batch inserts ON CONFLICT DO NOTHING, so a replay fills
-- nothing). Without this, the breadth half of candidate_promotion is dead
-- data for every signal predating the column. The fill is monotonic --
-- NULL -> value only, never the reverse, never an overwrite -- and every
-- other column must be byte-identical, so no claim, title, confidence or
-- piece of evidence can be rewritten after the fact. See
-- storage/migrate_author_backfill.sql for the full argument.
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
DECLARE
  fillable text;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    fillable := CASE TG_TABLE_NAME
                  WHEN 'signals'               THEN 'author_ref'
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

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'extraction_batches','signals','evidence_refs','signal_pool_events',
    'review_packets','packet_state_events','proposals','proposal_signals',
    'decisions','task_events','workload_events','crossboard_rulings']
  LOOP
    EXECUTE format(
      'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON %I
       FOR EACH ROW EXECUTE FUNCTION forbid_mutation()', t, t);
  END LOOP;
END $$;

-- ------------------------------------------------------------- board writer

-- Single entry point for board mutations. Assigns the next board_version,
-- appends the event, and maintains the projections in one transaction.
CREATE OR REPLACE FUNCTION apply_task_event(
  p_task_id text, p_user_ref text, p_event_type text, p_actor_type text,
  p_decision_id bigint, p_payload jsonb
) RETURNS bigint AS $$
DECLARE v bigint; eid bigint;
BEGIN
  INSERT INTO user_boards(user_ref, version) VALUES (p_user_ref, 0)
    ON CONFLICT (user_ref) DO NOTHING;
  UPDATE user_boards SET version = version + 1
    WHERE user_ref = p_user_ref RETURNING version INTO v;

  INSERT INTO task_events(task_id, user_ref, board_version, event_type,
                          actor_type, decision_id, payload)
  VALUES (p_task_id, p_user_ref, v, p_event_type, p_actor_type,
          p_decision_id, p_payload)
  RETURNING event_id INTO eid;

  IF p_event_type = 'created' THEN
    INSERT INTO tasks_current(task_id, user_ref, title, description, status,
                              owner_ref, deadline, parent_task_id,
                              created_at, updated_at, last_event_id)
    VALUES (p_task_id, p_user_ref,
            p_payload->>'title', p_payload->>'description',
            COALESCE(p_payload->>'status','proposed'),
            p_payload->>'owner_ref', (p_payload->>'deadline')::date,
            p_payload->>'parent_task_id', now(), now(), eid);
  ELSIF p_event_type = 'status_changed' THEN
    UPDATE tasks_current SET status = p_payload->>'to_status',
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
  ELSIF p_event_type = 'deadline_changed' THEN
    UPDATE tasks_current SET deadline = (p_payload->>'to_deadline')::date,
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
  ELSIF p_event_type = 'reassigned' THEN
    UPDATE tasks_current SET owner_ref = p_payload->>'to_owner_ref',
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
  ELSIF p_event_type IN ('completed','cancelled') THEN
    UPDATE tasks_current SET
           status = CASE p_event_type WHEN 'completed' THEN 'completed'
                                      ELSE 'cancelled' END,
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
  ELSIF p_event_type = 'reopened' THEN
    UPDATE tasks_current SET status = 'in_progress',
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
  ELSIF p_event_type = 'blocker_added' THEN
    UPDATE tasks_current SET status = 'blocked',
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
    INSERT INTO task_blockers(task_id, description, blocking_party,
                              blocking_task_id, opened_event_id)
    VALUES (p_task_id, p_payload->>'description', p_payload->>'blocking_party',
            p_payload->>'blocking_task_id', eid);
  ELSIF p_event_type = 'blocker_resolved' THEN
    UPDATE task_blockers SET resolved_event_id = eid
     WHERE blocker_id = (p_payload->>'blocker_id')::bigint;
    UPDATE tasks_current tc SET
           status = CASE WHEN EXISTS (SELECT 1 FROM task_blockers b
                          WHERE b.task_id = p_task_id
                            AND b.resolved_event_id IS NULL)
                    THEN 'blocked' ELSE 'in_progress' END,
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
  ELSIF p_event_type = 'merged_into' THEN
    UPDATE tasks_current SET merged_into = p_payload->>'surviving_task_id',
           status = 'cancelled', updated_at = now(), last_event_id = eid
     WHERE task_id = p_task_id;
  ELSIF p_event_type = 'alias_added' THEN
    INSERT INTO task_aliases(task_id, alias, source_signal_id)
    VALUES (p_task_id, p_payload->>'alias', p_payload->>'source_signal_id')
    ON CONFLICT DO NOTHING;
  END IF;
  RETURN eid;
END $$ LANGUAGE plpgsql;

-- Rebuild projections from the event log. Run in CI against a copy and diff
-- with tasks_current to prove the write-through projection is honest; run in
-- anger only for disaster recovery.
CREATE OR REPLACE FUNCTION rebuild_board(p_user_ref text) RETURNS integer AS $$
DECLARE e record; n integer := 0;
BEGIN
  DELETE FROM task_blockers WHERE task_id IN
    (SELECT task_id FROM tasks_current WHERE user_ref = p_user_ref);
  DELETE FROM task_aliases WHERE task_id IN
    (SELECT task_id FROM tasks_current WHERE user_ref = p_user_ref);
  UPDATE tasks_current SET parent_task_id = NULL, merged_into = NULL
    WHERE user_ref = p_user_ref;
  DELETE FROM tasks_current WHERE user_ref = p_user_ref;

  FOR e IN SELECT * FROM task_events WHERE user_ref = p_user_ref
           ORDER BY board_version LOOP
    n := n + 1;
    IF e.event_type = 'created' THEN
      INSERT INTO tasks_current(task_id, user_ref, title, description, status,
                                owner_ref, deadline, parent_task_id,
                                created_at, updated_at, last_event_id)
      VALUES (e.task_id, e.user_ref, e.payload->>'title',
              e.payload->>'description',
              COALESCE(e.payload->>'status','proposed'),
              e.payload->>'owner_ref', (e.payload->>'deadline')::date,
              e.payload->>'parent_task_id', e.occurred_at, e.occurred_at,
              e.event_id);
    ELSIF e.event_type = 'status_changed' THEN
      UPDATE tasks_current SET status = e.payload->>'to_status',
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type = 'deadline_changed' THEN
      UPDATE tasks_current SET deadline = (e.payload->>'to_deadline')::date,
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type = 'reassigned' THEN
      UPDATE tasks_current SET owner_ref = e.payload->>'to_owner_ref',
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type IN ('completed','cancelled') THEN
      UPDATE tasks_current SET
             status = CASE e.event_type WHEN 'completed' THEN 'completed'
                                        ELSE 'cancelled' END,
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type = 'reopened' THEN
      UPDATE tasks_current SET status = 'in_progress',
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type = 'blocker_added' THEN
      UPDATE tasks_current SET status = 'blocked',
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
      INSERT INTO task_blockers(task_id, description, blocking_party,
                                blocking_task_id, opened_event_id)
      VALUES (e.task_id, e.payload->>'description',
              e.payload->>'blocking_party', e.payload->>'blocking_task_id',
              e.event_id);
    ELSIF e.event_type = 'blocker_resolved' THEN
      UPDATE task_blockers SET resolved_event_id = e.event_id
       WHERE task_id = e.task_id AND resolved_event_id IS NULL
         AND description = e.payload->>'description';
      UPDATE tasks_current SET
             status = CASE WHEN EXISTS (SELECT 1 FROM task_blockers b
                            WHERE b.task_id = e.task_id
                              AND b.resolved_event_id IS NULL)
                      THEN 'blocked' ELSE 'in_progress' END,
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type = 'merged_into' THEN
      UPDATE tasks_current SET merged_into = e.payload->>'surviving_task_id',
             status = 'cancelled', updated_at = e.occurred_at,
             last_event_id = e.event_id
       WHERE task_id = e.task_id;
    ELSIF e.event_type = 'alias_added' THEN
      INSERT INTO task_aliases(task_id, alias, source_signal_id)
      VALUES (e.task_id, e.payload->>'alias', e.payload->>'source_signal_id')
      ON CONFLICT DO NOTHING;
    END IF;
  END LOOP;
  RETURN n;
END $$ LANGUAGE plpgsql;


-- ------------------------------------------------- candidate register

CREATE TABLE task_candidate_events (
  event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  candidate_id text NOT NULL,          -- content hash of the normalised title
  user_ref     text NOT NULL,          -- whose pool the sighting arrived in
  event        text NOT NULL CHECK (event IN
                 ('sighted','promoted','dismissed','accepted')),
  title        text NOT NULL,
  signal_id    text REFERENCES signals(signal_id),   -- null for state events
  speaker_ref  text,                   -- who said it; corroboration counts these
  run_id       text,
  occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX candidate_events_user_idx
  ON task_candidate_events(user_ref, candidate_id);

COMMENT ON TABLE task_candidate_events IS
  'Work referenced in conversation that is not on any board yet. Append-only; '
  'v_task_candidates is the projection. A candidate is NOT a task and never '
  'becomes one without a human decision -- promotion only puts it in a packet.';

-- Current state per candidate, plus the corroboration counts the promotion
-- rule reads. Distinct SPEAKERS, not distinct sightings: one person saying
-- the same thing in four meetings is one person's opinion, and that is the
-- same standard the closure gates already hold.
CREATE OR REPLACE VIEW v_task_candidates AS
WITH latest AS (
  SELECT DISTINCT ON (candidate_id, user_ref)
         candidate_id, user_ref, event, title
  FROM task_candidate_events
  ORDER BY candidate_id, user_ref, event_id DESC
), decided AS (
  -- 'sighted' is an observation; promoted/dismissed/accepted are decisions,
  -- and latest-wins let an observation overwrite one. Mentioning work again
  -- is not a reason to reopen a question somebody already answered -- on a
  -- live register a third sighting flipped a promoted candidate back to
  -- 'sighted', which would have raised a duplicate proposal for work
  -- already in a review queue. See migrate_candidate_state_sticky.sql.
  SELECT DISTINCT ON (candidate_id, user_ref)
         candidate_id, user_ref, event
  FROM task_candidate_events
  WHERE event IN ('promoted', 'dismissed', 'accepted')
  ORDER BY candidate_id, user_ref, event_id DESC
), counts AS (
  SELECT candidate_id, user_ref,
         count(*) FILTER (WHERE event = 'sighted')            AS sightings,
         count(DISTINCT speaker_ref)
           FILTER (WHERE event = 'sighted'
                     AND speaker_ref IS NOT NULL)             AS speakers,
         min(occurred_at) FILTER (WHERE event = 'sighted')    AS first_seen,
         max(occurred_at) FILTER (WHERE event = 'sighted')    AS last_seen
  FROM task_candidate_events
  GROUP BY candidate_id, user_ref
)
SELECT l.candidate_id, l.user_ref, l.title,
       COALESCE(d.event, l.event) AS state,
       c.sightings, c.speakers, c.first_seen, c.last_seen
FROM latest l
JOIN counts c USING (candidate_id, user_ref)
LEFT JOIN decided d USING (candidate_id, user_ref);

COMMENT ON VIEW v_task_candidates IS
  'Latest-event-wins projection of task_candidate_events, with the sighting '
  'and distinct-speaker counts the promotion rule in matching_policy.yaml '
  'reads.';

-- Append-only, like every other evidence table here.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger
                 WHERE tgname = 'task_candidate_events_append_only') THEN
    CREATE TRIGGER task_candidate_events_append_only
      BEFORE UPDATE OR DELETE ON task_candidate_events
      FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
  END IF;
END $$;


-- --------------------------------------------------- derived progress

CREATE OR REPLACE VIEW v_task_progress AS
SELECT p.task_id,
       p.user_ref,
       count(c.task_id)                                   AS subtasks_total,
       count(c.task_id) FILTER (WHERE c.status = 'completed')
                                                          AS subtasks_done,
       -- NULL, not zero: a task with no subtasks has unknown progress, and
       -- rendering that as 0% would read as "nothing has happened".
       CASE WHEN count(c.task_id) = 0 THEN NULL
            ELSE round(
              count(c.task_id) FILTER (WHERE c.status = 'completed')::numeric
              / count(c.task_id), 3)
       END                                                AS subtask_fraction,
       p.deadline,
       CASE WHEN p.deadline IS NULL THEN NULL
            ELSE (p.deadline - CURRENT_DATE)
       END                                                AS days_to_deadline,
       (p.deadline IS NOT NULL AND p.deadline < CURRENT_DATE
        AND p.status NOT IN ('completed','cancelled'))    AS overdue
FROM tasks_current p
LEFT JOIN tasks_current c
       ON c.parent_task_id = p.task_id AND c.merged_into IS NULL
WHERE p.merged_into IS NULL
GROUP BY p.task_id, p.user_ref, p.deadline, p.status;

COMMENT ON VIEW v_task_progress IS
  'Derived progress: completed subtasks over total, and deadline distance. '
  'Stored nowhere -- every input is a human-decided event, so this cannot '
  'drift from the log and no model ever writes a percentage.';

COMMIT;
