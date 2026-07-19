-- validate_access.sql
-- Asserts, against the already-seeded taskintel database:
--   A. grant authority: only the board owner shares a board; only the project
--      owner shares a project
--   B. visibility: a project-scoped stakeholder sees ONLY that project's
--      tasks, not the rest of the owner's board
--   C. levels: progress vs full_trace resolve correctly; owner always full
--   D. revocation: latest-event-wins removes access
--   E. rebuild preserves project_id (projection honesty still holds)
-- Run: psql -v ON_ERROR_STOP=1 -f validate_access.sql

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- seed

INSERT INTO projects VALUES
 ('proj-drift','Readmission drift monitoring','alex', now()),
 ('proj-intake','Intake OCR','alex', now());

-- Assign existing tasks to projects through the write path.
SELECT apply_task_event('task-88','alex','project_assigned','manual_edit',NULL,
 '{"project_id":"proj-drift"}'::jsonb);
SELECT apply_task_event('task-70','alex','project_assigned','manual_edit',NULL,
 '{"project_id":"proj-intake"}'::jsonb);
SELECT apply_task_event('task-99','priya','project_assigned','manual_edit',NULL,
 '{"project_id":"proj-drift"}'::jsonb);

-- vic: outside stakeholder on the drift project only, progress level.
SELECT grant_access('alex','vic','project','proj-drift','progress',
                    'external stakeholder for drift work');
-- priya: teammate with full trace on alex's whole board.
SELECT grant_access('alex','priya','user','alex','full_trace', NULL);

-- ---------------------------------------------------------------- asserts

-- A. grant authority
DO $$
BEGIN
  BEGIN
    PERFORM grant_access('mallory','mallory','user','alex','full_trace',NULL);
    RAISE EXCEPTION 'ASSERT FAILED: mallory shared someone else''s board';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE '%may share their own board%' THEN
      RAISE NOTICE 'PASS: only the board owner may share a board';
    ELSE RAISE; END IF;
  END;
  BEGIN
    PERFORM grant_access('priya','vic','project','proj-drift','full_trace',NULL);
    RAISE EXCEPTION 'ASSERT FAILED: non-owner granted project access';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE '%owner of project%' THEN
      RAISE NOTICE 'PASS: only the project owner may share a project';
    ELSE RAISE; END IF;
  END;
END $$;

-- B + C. visibility and levels
DO $$
DECLARE n integer; lvl text;
BEGIN
  -- vic sees the drift tasks (alex's task-88 AND priya's task-99), nothing else
  SELECT count(*) INTO n FROM visible_tasks('vic');
  IF n <> 2 THEN RAISE EXCEPTION
    'ASSERT FAILED: vic should see exactly 2 tasks, sees %', n; END IF;
  SELECT count(*) INTO n FROM visible_tasks('vic') v
    WHERE v.task_id IN ('task-88','task-99');
  IF n <> 2 THEN RAISE EXCEPTION
    'ASSERT FAILED: vic sees the wrong tasks'; END IF;
  RAISE NOTICE 'PASS: project grant scopes across owners, excludes task-70';

  SELECT task_access('vic','task-88') INTO lvl;
  IF lvl <> 'progress' THEN RAISE EXCEPTION
    'ASSERT FAILED: vic should be progress on task-88, is %', lvl; END IF;
  IF task_access('vic','task-70') IS NOT NULL THEN RAISE EXCEPTION
    'ASSERT FAILED: vic can see task-70'; END IF;
  RAISE NOTICE 'PASS: stakeholder level is progress; unshared task invisible';

  SELECT count(*) INTO n FROM visible_tasks('priya');
  IF n <> 3 THEN RAISE EXCEPTION
    'ASSERT FAILED: priya should see 3 tasks (own + alex board), sees %', n;
  END IF;
  SELECT task_access('priya','task-70') INTO lvl;
  IF lvl <> 'full_trace' THEN RAISE EXCEPTION
    'ASSERT FAILED: priya should be full_trace via user grant'; END IF;
  SELECT task_access('alex','task-88') INTO lvl;
  IF lvl <> 'full_trace' THEN RAISE EXCEPTION
    'ASSERT FAILED: owner not full_trace'; END IF;
  RAISE NOTICE 'PASS: user-scope grant covers whole board; owner always full';

  -- Level precedence: give vic full_trace on the project too; max wins.
  PERFORM grant_access('alex','vic','project','proj-drift','full_trace',NULL);
  SELECT task_access('vic','task-88') INTO lvl;
  IF lvl <> 'full_trace' THEN RAISE EXCEPTION
    'ASSERT FAILED: upgraded grant did not take effect'; END IF;
  RAISE NOTICE 'PASS: latest grant event wins (upgrade to full_trace)';
END $$;

-- D. revocation
DO $$
DECLARE n integer;
BEGIN
  PERFORM revoke_access('alex','vic','project','proj-drift');
  SELECT count(*) INTO n FROM visible_tasks('vic');
  IF n <> 0 THEN RAISE EXCEPTION
    'ASSERT FAILED: vic still sees % tasks after revoke', n; END IF;
  RAISE NOTICE 'PASS: revoke removes visibility; audit rows retained';
  SELECT count(*) INTO n FROM access_grant_events
    WHERE grantee_ref = 'vic';
  IF n < 3 THEN RAISE EXCEPTION
    'ASSERT FAILED: grant audit trail missing rows'; END IF;
  -- restore for the API demo
  PERFORM grant_access('alex','vic','project','proj-drift','progress',NULL);
END $$;

-- E. rebuild preserves project assignment
DO $$
DECLARE pid text;
BEGIN
  PERFORM rebuild_board('alex');
  SELECT project_id INTO pid FROM tasks_current WHERE task_id = 'task-88';
  IF pid <> 'proj-drift' THEN RAISE EXCEPTION
    'ASSERT FAILED: rebuild lost project_id (%)', pid; END IF;
  RAISE NOTICE 'PASS: rebuild_board preserves project assignment';
END $$;

\echo ALL ACCESS CONTROL ASSERTIONS PASSED
