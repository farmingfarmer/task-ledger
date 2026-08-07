-- =========================================================================
-- Migration: expiring grants + blocker-implied status visibility.
-- Apply to databases created before these existed:
--     psql -d taskintel -f access/migrate_sharing.sql
-- Fresh databases get both from access_control.sql.
--
-- 1. GRANTS CAN EXPIRE. The stakeholder case -- "follow this project
--    through launch" -- is inherently temporary, but a grant lasted until
--    someone remembered to revoke it, and nobody remembers. A grant may now
--    carry expires_at; v_current_grants simply stops returning it after
--    that moment, so every read path inherits the expiry with no new code.
--    Revocation stays an event, because "it expired" and "they took it
--    back" are different facts and the audit trail should keep both.
--
-- 2. A BLOCKER IMPLIES NARROW VISIBILITY. If my task is blocked on your
--    task, I currently learn nothing about whether it is moving -- so the
--    dependency edge, which is the most defensible reason for automatic
--    access in the whole model, granted none. It now yields a third level,
--    status_only: the title and status of the specific blocking task, and
--    nothing else. No history, no rationales, no provenance, no siblings,
--    no deadline. The edge is self-limiting: resolve the blocker and the
--    visibility disappears with it, because the rule reads open blockers
--    rather than any stored grant.
--
--    The ladder is now owner > full_trace > progress > status_only, and it
--    stays resolved in ONE function. status_only is deliberately below
--    progress: a stakeholder follows a whole board's history skeleton,
--    while this shows one row of one task.
-- =========================================================================

BEGIN;

ALTER TABLE access_grant_events
  ADD COLUMN IF NOT EXISTS expires_at timestamptz;

COMMENT ON COLUMN access_grant_events.expires_at IS
  'When this grant stops applying. NULL = until revoked. Enforced in '
  'v_current_grants, so every read path inherits it.';

CREATE OR REPLACE VIEW v_current_grants AS
SELECT grantee_ref, subject_type, subject_id, level, granted_by, occurred_at,
       expires_at
FROM (
  SELECT DISTINCT ON (grantee_ref, subject_type, subject_id) *
  FROM access_grant_events
  ORDER BY grantee_ref, subject_type, subject_id, event_id DESC
) latest
WHERE latest.event = 'granted'
  AND (latest.expires_at IS NULL OR latest.expires_at > now());

-- grant_access gains an expiry argument. The old six-argument signature is
-- dropped rather than left beside it: two overloads reachable by the same
-- six-argument call is an ambiguity error waiting to happen, and there must
-- be exactly one write path for grants.
DROP FUNCTION IF EXISTS grant_access(text, text, text, text, text, text);

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

-- THE authorization function, now four levels deep. Everything a viewer may
-- see, at the strongest level they hold: their own board, then grants, then
-- the tasks that are blocking work they own.
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
     -- Blocker-implied: this task is blocking a task the viewer owns, and
     -- the blocker is still open. Nothing is stored; the edge IS the grant.
     OR EXISTS (SELECT 1 FROM task_blockers b
                JOIN tasks_current w ON w.task_id = b.task_id
                WHERE b.blocking_task_id = t.task_id
                  AND b.resolved_event_id IS NULL
                  AND w.user_ref = p_viewer)
$$ LANGUAGE sql STABLE;

COMMIT;
