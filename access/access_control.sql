-- =========================================================================
-- Access control migration: projects, grants, and visibility resolution
-- Apply after schema.sql. Companion doc: ACCESS_CONTROL.md
--
-- Model:
--   - Tasks may belong to a project (nullable project_id).
--   - Access is granted per viewer at one of two scopes:
--       subject_type='user'     see a person's whole board
--       subject_type='project'  see one project's tasks, regardless of owner
--     and one of two levels:
--       'progress'    board state + history skeleton; rationales, decision
--                     detail, and provenance are REDACTED (stakeholder view)
--       'full_trace'  everything down to evidence pointers (teammate view)
--   - Owners always have full_trace on their own tasks; no grant rows needed.
--   - Grants are append-only events (granted/revoked); current state is the
--     latest event per (grantee, subject). Full audit trail for free.
--   - Who may grant: your own board -> you; a project -> the project owner.
--     Enforced in grant_access(), the only write path.
--   - Visibility resolution lives in visible_tasks(viewer): the ONE function
--     every read path goes through, so policy changes happen in one place
--     and the API cannot accidentally bypass it.
-- =========================================================================

BEGIN;

-- ---------------------------------------------------------------- projects

CREATE TABLE projects (
  project_id  text PRIMARY KEY,
  name        text NOT NULL,
  owner_ref   text NOT NULL,                 -- may grant project access
  created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE tasks_current ADD COLUMN project_id text
  REFERENCES projects(project_id);
CREATE INDEX tasks_project_idx ON tasks_current(project_id)
  WHERE project_id IS NOT NULL;

-- ------------------------------------------------------------------ grants

CREATE TABLE access_grant_events (
  event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  grantee_ref  text NOT NULL,                -- who gets to look
  subject_type text NOT NULL CHECK (subject_type IN ('user','project')),
  subject_id   text NOT NULL,                -- user_ref or project_id
  level        text NOT NULL CHECK (level IN ('progress','full_trace')),
  event        text NOT NULL CHECK (event IN ('granted','revoked')),
  granted_by   text NOT NULL,
  note         text,
  expires_at   timestamptz,                  -- NULL = until revoked
  occurred_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX grant_lookup_idx
  ON access_grant_events(grantee_ref, subject_type, subject_id, event_id DESC);

CREATE TRIGGER access_grant_events_append_only
  BEFORE UPDATE OR DELETE ON access_grant_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

-- Current grants: latest event per (grantee, subject) that is 'granted'.
CREATE VIEW v_current_grants AS
SELECT grantee_ref, subject_type, subject_id, level, granted_by, occurred_at,
       expires_at
FROM (
  SELECT DISTINCT ON (grantee_ref, subject_type, subject_id) *
  FROM access_grant_events
  ORDER BY grantee_ref, subject_type, subject_id, event_id DESC
) latest
WHERE latest.event = 'granted'
  AND (latest.expires_at IS NULL OR latest.expires_at > now());

-- The only write path for grants; enforces who may grant what.
CREATE OR REPLACE FUNCTION grant_access(
  p_granted_by text, p_grantee text, p_subject_type text, p_subject_id text,
  p_level text, p_note text DEFAULT NULL, p_expires_at timestamptz DEFAULT NULL
) RETURNS bigint AS $$
DECLARE eid bigint;
BEGIN
  IF p_subject_type = 'user' THEN
    IF p_granted_by <> p_subject_id THEN
      RAISE EXCEPTION 'only % may share their own board', p_subject_id;
    END IF;
  ELSIF p_subject_type = 'project' THEN
    IF NOT EXISTS (SELECT 1 FROM projects
                   WHERE project_id = p_subject_id
                     AND owner_ref = p_granted_by) THEN
      RAISE EXCEPTION 'only the owner of project % may grant access to it',
        p_subject_id;
    END IF;
  END IF;
  IF p_expires_at IS NOT NULL AND p_expires_at <= now() THEN
    RAISE EXCEPTION 'expiry % is already in the past', p_expires_at;
  END IF;
  INSERT INTO access_grant_events(grantee_ref, subject_type, subject_id,
                                  level, event, granted_by, note, expires_at)
  VALUES (p_grantee, p_subject_type, p_subject_id, p_level, 'granted',
          p_granted_by, p_note, p_expires_at)
  RETURNING event_id INTO eid;
  RETURN eid;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION revoke_access(
  p_revoked_by text, p_grantee text, p_subject_type text, p_subject_id text
) RETURNS bigint AS $$
DECLARE eid bigint;
BEGIN
  IF p_subject_type = 'user' AND p_revoked_by <> p_subject_id THEN
    RAISE EXCEPTION 'only % may revoke access to their board', p_subject_id;
  END IF;
  IF p_subject_type = 'project' AND NOT EXISTS (
      SELECT 1 FROM projects WHERE project_id = p_subject_id
        AND owner_ref = p_revoked_by) THEN
    RAISE EXCEPTION 'only the project owner may revoke access to %',
      p_subject_id;
  END IF;
  INSERT INTO access_grant_events(grantee_ref, subject_type, subject_id,
                                  level, event, granted_by)
  VALUES (p_grantee, p_subject_type, p_subject_id, 'progress', 'revoked',
          p_revoked_by)
  RETURNING event_id INTO eid;
  RETURN eid;
END $$ LANGUAGE plpgsql;

-- -------------------------------------------------- visibility resolution

-- THE authorization function. Returns every task the viewer may see, with
-- the effective access level (owner > full_trace grant > progress grant).
-- Every dashboard query joins through this; nothing reads tasks_current
-- for another user directly.
CREATE OR REPLACE FUNCTION visible_tasks(p_viewer text)
RETURNS TABLE (task_id text, access_level text) AS $$
  SELECT t.task_id,
         CASE
           WHEN t.user_ref = p_viewer THEN 'full_trace'
           WHEN EXISTS (SELECT 1 FROM v_current_grants g
                        WHERE g.grantee_ref = p_viewer
                          AND ((g.subject_type = 'user'
                                AND g.subject_id = t.user_ref)
                            OR (g.subject_type = 'project'
                                AND g.subject_id = t.project_id))
                          AND g.level = 'full_trace') THEN 'full_trace'
           WHEN EXISTS (SELECT 1 FROM v_current_grants g
                        WHERE g.grantee_ref = p_viewer
                          AND ((g.subject_type = 'user'
                                AND g.subject_id = t.user_ref)
                            OR (g.subject_type = 'project'
                                AND g.subject_id = t.project_id)))
                THEN 'progress'
           -- Blocking a task the viewer owns: title and status, nothing more.
           ELSE 'status_only'
         END AS access_level
  FROM tasks_current t
  WHERE t.user_ref = p_viewer
     OR EXISTS (SELECT 1 FROM v_current_grants g
                WHERE g.grantee_ref = p_viewer
                  AND ((g.subject_type = 'user'
                        AND g.subject_id = t.user_ref)
                    OR (g.subject_type = 'project'
                        AND g.subject_id = t.project_id)))
     -- Blocker-implied visibility: the edge IS the grant, so resolving the
     -- blocker withdraws it with no revocation to remember.
     OR EXISTS (SELECT 1 FROM task_blockers b
                JOIN tasks_current w ON w.task_id = b.task_id
                WHERE b.blocking_task_id = t.task_id
                  AND b.resolved_event_id IS NULL
                  AND w.user_ref = p_viewer)
$$ LANGUAGE sql STABLE;

-- Convenience: may this viewer see this task, and at what level?
CREATE OR REPLACE FUNCTION task_access(p_viewer text, p_task_id text)
RETURNS text AS $$
  SELECT access_level FROM visible_tasks(p_viewer) WHERE task_id = p_task_id
$$ LANGUAGE sql STABLE;

-- --------------------------- project-aware writer and rebuild (replacement)

-- apply_task_event and rebuild_board are replaced to carry project_id on
-- 'created' and to support a 'project_assigned' event. Bodies are identical
-- to schema.sql otherwise.

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
                              owner_ref, deadline, parent_task_id, project_id,
                              created_at, updated_at, last_event_id)
    VALUES (p_task_id, p_user_ref,
            p_payload->>'title', p_payload->>'description',
            COALESCE(p_payload->>'status','proposed'),
            p_payload->>'owner_ref', (p_payload->>'deadline')::date,
            p_payload->>'parent_task_id', p_payload->>'project_id',
            now(), now(), eid);
  ELSIF p_event_type = 'project_assigned' THEN
    UPDATE tasks_current SET project_id = p_payload->>'project_id',
           updated_at = now(), last_event_id = eid WHERE task_id = p_task_id;
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
                                owner_ref, deadline, parent_task_id, project_id,
                                created_at, updated_at, last_event_id)
      VALUES (e.task_id, e.user_ref, e.payload->>'title',
              e.payload->>'description',
              COALESCE(e.payload->>'status','proposed'),
              e.payload->>'owner_ref', (e.payload->>'deadline')::date,
              e.payload->>'parent_task_id', e.payload->>'project_id',
              e.occurred_at, e.occurred_at, e.event_id);
    ELSIF e.event_type = 'project_assigned' THEN
      UPDATE tasks_current SET project_id = e.payload->>'project_id',
             updated_at = e.occurred_at, last_event_id = e.event_id
       WHERE task_id = e.task_id;
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

-- task_events check constraint must admit the new event type.
ALTER TABLE task_events DROP CONSTRAINT task_events_event_type_check;
ALTER TABLE task_events ADD CONSTRAINT task_events_event_type_check
  CHECK (event_type IN
    ('created','status_changed','deadline_changed','reassigned',
     'blocker_added','blocker_resolved','completed','cancelled',
     'reopened','merged_into','alias_added','subtask_linked','annotated',
     'project_assigned'));

COMMIT;
