-- =========================================================================
-- Migration: a fourth actor type, for changes no person approved.
--     psql -d taskintel -f storage/migrate_auto_applied.sql
-- Fresh databases get this from schema.sql.
--
-- WHAT THIS COSTS, stated plainly because it is the only place this system
-- gives ground. The rule has been: no board mutation without a human
-- DecisionRecord, enforced by a CHECK. This does not weaken that CHECK --
-- an AI-originated change still cannot masquerade as human-typed. It adds
-- a THIRD possibility and makes it permanently visible: a change nobody
-- approved, applied by the pipeline, labelled as such forever.
--
-- 'system' was the obvious existing home and the wrong one. System events
-- are alias_added and the like -- matching metadata that never touches
-- board state. A status moving from in_progress to blocked IS board state.
-- Filing it under 'system' would have hidden the one thing worth being
-- able to count, which is how much of the board no human ever agreed to.
--
-- Every query that wants only human-approved state can now say so:
--     WHERE actor_type <> 'auto_applied'
-- and every audit can ask the honest question -- how much of this board
-- did somebody actually confirm? -- and get an answer.
--
-- The gate itself lives in matching_policy.yaml (auto_apply), is OFF by
-- default, and is deliberately narrow: one op, the owner's own words about
-- their own task on their own board, forward transitions only, auto-linked
-- matches only. Undo writes a compensating event as manual_edit, because
-- reverting IS a person typing something.
-- =========================================================================

BEGIN;

ALTER TABLE task_events DROP CONSTRAINT IF EXISTS task_events_actor_type_check;
ALTER TABLE task_events ADD CONSTRAINT task_events_actor_type_check
  CHECK (actor_type IN
    ('user_decision','manual_edit','system','auto_applied'));

COMMENT ON COLUMN task_events.actor_type IS
  'user_decision: a person accepted a proposal (decision_id required). '
  'manual_edit: a person typed it directly. system: pipeline metadata that '
  'does not change board state. auto_applied: board state changed with no '
  'human approval, under the narrow auto_apply gate -- countable precisely '
  'so it can be audited and revoked.';

COMMIT;
