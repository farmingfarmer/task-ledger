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
                     'meeting_transcript','document')),
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
                   'document_span','meeting_insight')),
  resource_id   text NOT NULL,               -- Graph id; pointer, never a copy
  span_start    integer,
  span_end      integer,
  content_hash  text,
  PRIMARY KEY (signal_id, ordinal),
  CHECK (span_end IS NULL OR span_start IS NULL OR span_end >= span_start)
);

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
  actor_type    text NOT NULL CHECK (actor_type IN
                  ('user_decision','manual_edit','system')),
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

-- --------------------------------------------------- append-only enforcement

CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION '% is append-only; % not permitted', TG_TABLE_NAME, TG_OP;
END $$ LANGUAGE plpgsql;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'extraction_batches','signals','evidence_refs','signal_pool_events',
    'review_packets','packet_state_events','proposals','proposal_signals',
    'decisions','task_events']
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

COMMIT;
