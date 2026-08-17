-- =========================================================================
-- Migration: progress as a projection, not a column.
--     psql -d taskintel -f storage/migrate_task_progress.sql
-- Fresh databases get this from schema.sql.
--
-- WHY A VIEW. "How far along is this?" is the obvious thing to store and
-- the wrong thing to store. A percentage field would be a fact nobody
-- decided: either a model writes it, which breaks the one rule this system
-- has, or a person maintains it by hand, which nobody does, and it rots
-- into a number that contradicts the events underneath it.
--
-- Every subtask completion is ALREADY a human-decided event with
-- provenance. Counting them costs nothing, inherits that provenance, and
-- cannot drift from the log because it is not stored anywhere. The same
-- reasoning as rebuild_board: tasks_current is a projection too, and the
-- suite asserts it by rebuilding and diffing.
--
-- WHAT IS DELIBERATELY ABSENT: an estimated completion date. Forecasting
-- needs either effort estimates, which nobody says out loud in a standup,
-- or enough velocity history to extrapolate from. Inventing one from three
-- weeks of transcripts would be a number with a decimal point and no
-- meaning. days_to_deadline is what the ledger actually knows.
-- =========================================================================

BEGIN;

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
