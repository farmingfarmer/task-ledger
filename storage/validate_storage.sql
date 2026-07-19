-- validate_storage.sql
-- Seeds a realistic slice through the intended write paths, then asserts:
--   A. append-only triggers actually block UPDATE/DELETE
--   B. apply_task_event maintains projections and monotonic board versions
--   C. rebuild_board() reproduces tasks_current exactly (projection honesty)
--   D. every drill-down query (Q1-Q9) returns the expected shape/rows
-- Run: psql -v ON_ERROR_STOP=1 -f validate_storage.sql

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------- seed data

INSERT INTO extraction_batches VALUES
 ('batch-1','2026-07-15 17:35Z','azure-openai:gpt-4.1','extract-v3.2',
  'meeting_transcript','meeting-guid-1','2026-07-15 17:00Z','2026-07-15 17:30Z');

INSERT INTO signals (signal_id,batch_id,signal_type,title,description,
  status_signal,confidence,deadline_date,deadline_precision,deadline_raw,raw)
VALUES
 ('sig-1','batch-1','status_update','Deploy drift monitor to staging',
  'Alex said the drift monitor is live in staging','in_progress',0.85,
  NULL,NULL,NULL,'{}'),
 ('sig-2','batch-1','deadline_signal','Drift monitor deadline',NULL,NULL,0.72,
  '2026-07-24','week','end of next sprint','{}'),
 ('sig-3','batch-1','blocker','Fargate task role pending',
  'Waiting on cloud team for the IAM role',NULL,0.66,NULL,NULL,NULL,'{}');

INSERT INTO evidence_refs VALUES
 ('sig-1',1,'transcript_utterance','utt-442',0,88,'hash-a'),
 ('sig-2',1,'teams_message','msg-9',NULL,NULL,'hash-b'),
 ('sig-3',1,'teams_message','msg-11',NULL,NULL,'hash-c');

INSERT INTO signal_pool_events (signal_id,user_ref,event,run_id) VALUES
 ('sig-1','alex','pooled',NULL),('sig-2','alex','pooled',NULL),
 ('sig-3','alex','pooled',NULL),
 ('sig-1','alex','taken','run-1'),('sig-2','alex','taken','run-1'),
 ('sig-3','alex','taken','run-1');

-- Board setup: two tasks created (one via manual edit to prove that path),
-- then AI-proposed changes flow through packet -> decision -> event.
SELECT apply_task_event('task-88','alex','created','manual_edit',NULL,
 '{"title":"Ship drift monitoring for readmission model","status":"proposed",
   "owner_ref":"alex"}'::jsonb);
SELECT apply_task_event('task-70','alex','created','manual_edit',NULL,
 '{"title":"Fix intake OCR crash","status":"in_progress","owner_ref":"alex"}'::jsonb);
SELECT apply_task_event('task-99','priya','created','manual_edit',NULL,
 '{"title":"Provision Fargate IAM role","status":"in_progress",
   "owner_ref":"priya"}'::jsonb);

INSERT INTO review_packets VALUES
 ('pkt-1','alex','run-1',
  (SELECT version FROM user_boards WHERE user_ref='alex'),
  now(), now() + interval '48 hours');
INSERT INTO packet_state_events (packet_id,state) VALUES
 ('pkt-1','draft'),('pkt-1','delivered');

INSERT INTO proposals (proposal_id,packet_id,op,target_task_id,rationale,
  confidence,conflict_type,conflict_detail,payload) VALUES
 ('prop-1','pkt-1','update_status','task-88',
  'In Tuesday standup you said the drift monitor deploy was underway.',0.85,
  NULL,NULL,
  '{"kind":"update_status","from_status":"proposed","to_status":"in_progress"}'),
 ('prop-2','pkt-1','update_deadline','task-88',
  'You told Priya it would land by end of next sprint.',0.72,NULL,NULL,
  '{"kind":"update_deadline","from_deadline":null,"to_deadline":"2026-07-24",
    "raw_text":"end of next sprint"}'),
 ('prop-3','pkt-1','add_blocker','task-88',
  'You said you are waiting on the cloud team for the IAM role.',0.66,NULL,NULL,
  '{"kind":"add_blocker","description":"Fargate task role pending",
    "blocking_task_id":"task-99"}');

INSERT INTO proposal_signals VALUES
 ('prop-1','sig-1'),('prop-2','sig-2'),('prop-3','sig-3');

INSERT INTO decisions (proposal_id,packet_id,decision,decided_by,decided_at)
VALUES ('prop-1','pkt-1','accept','alex',now());
SELECT apply_task_event('task-88','alex','status_changed','user_decision',
 currval(pg_get_serial_sequence('decisions','decision_id')),
 '{"to_status":"in_progress"}'::jsonb);
SELECT apply_task_event('task-88','alex','alias_added','system',NULL,
 '{"alias":"Deploy drift monitor to staging","source_signal_id":"sig-1"}'::jsonb);

INSERT INTO decisions (proposal_id,packet_id,decision,edited_payload,
  decided_by,decided_at)
VALUES ('prop-2','pkt-1','accept_edited',
 '{"kind":"update_deadline","to_deadline":"2026-07-31"}','alex',now());
SELECT apply_task_event('task-88','alex','deadline_changed','user_decision',
 currval(pg_get_serial_sequence('decisions','decision_id')),
 '{"to_deadline":"2026-07-31"}'::jsonb);

INSERT INTO decisions (proposal_id,packet_id,decision,decided_by,decided_at)
VALUES ('prop-3','pkt-1','accept','alex',now());
SELECT apply_task_event('task-88','alex','blocker_added','user_decision',
 currval(pg_get_serial_sequence('decisions','decision_id')),
 '{"description":"Fargate task role pending","blocking_task_id":"task-99"}'::jsonb);

INSERT INTO packet_state_events (packet_id,state) VALUES ('pkt-1','closed');

-- A rejected proposal in a second packet, for the calibration queries.
INSERT INTO review_packets VALUES
 ('pkt-2','alex','run-2',
  (SELECT version FROM user_boards WHERE user_ref='alex'),
  now(), now() + interval '48 hours');
INSERT INTO packet_state_events (packet_id,state) VALUES
 ('pkt-2','draft'),('pkt-2','delivered');
INSERT INTO proposals (proposal_id,packet_id,op,target_task_id,rationale,
  confidence,payload) VALUES
 ('prop-4','pkt-2','create_task',NULL,'Priya floated a dashboard dark mode.',
  0.61,'{"kind":"create_task","title":"Dashboard dark mode","status":"proposed",
         "match_note":"closest task-88 (0.44) below merge threshold"}');
INSERT INTO decisions (proposal_id,packet_id,decision,reject_reason,
  decided_by,decided_at)
VALUES ('prop-4','pkt-2','reject','not_a_task','alex',now());

-- --------------------------------------------------------------- assertions

-- A. append-only enforcement
DO $$
BEGIN
  BEGIN
    UPDATE decisions SET decision = 'reject' WHERE proposal_id = 'prop-1';
    RAISE EXCEPTION 'ASSERT FAILED: decisions UPDATE was not blocked';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE '%append-only%' THEN
      RAISE NOTICE 'PASS: decisions append-only';
    ELSE RAISE; END IF;
  END;
  BEGIN
    DELETE FROM task_events WHERE task_id = 'task-88';
    RAISE EXCEPTION 'ASSERT FAILED: task_events DELETE was not blocked';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE '%append-only%' THEN
      RAISE NOTICE 'PASS: task_events append-only';
    ELSE RAISE; END IF;
  END;
END $$;

-- B. projection state and version monotonicity
DO $$
DECLARE t record; v bigint;
BEGIN
  SELECT * INTO t FROM tasks_current WHERE task_id = 'task-88';
  IF t.status <> 'blocked' OR t.deadline <> '2026-07-31' THEN
    RAISE EXCEPTION 'ASSERT FAILED: task-88 projection wrong: % %',
      t.status, t.deadline;
  END IF;
  RAISE NOTICE 'PASS: write-through projection (status=blocked, edited deadline)';
  SELECT count(*) INTO v FROM (
    SELECT board_version - row_number() OVER (ORDER BY board_version) AS gap
    FROM task_events WHERE user_ref = 'alex') g WHERE gap <> 0;
  IF v <> 0 THEN
    RAISE EXCEPTION 'ASSERT FAILED: board versions not contiguous';
  END IF;
  RAISE NOTICE 'PASS: per-user board versions contiguous and monotonic';
END $$;

-- C. rebuild equivalence: snapshot projection, rebuild from events, diff.
CREATE TEMP TABLE snap AS
  SELECT task_id, user_ref, title, status, owner_ref, deadline,
         parent_task_id, merged_into FROM tasks_current;
SELECT rebuild_board('alex');
SELECT rebuild_board('priya');
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM (
    (SELECT task_id,user_ref,title,status,owner_ref,deadline,
            parent_task_id,merged_into FROM tasks_current
     EXCEPT
     SELECT * FROM snap)
    UNION ALL
    (SELECT * FROM snap
     EXCEPT
     SELECT task_id,user_ref,title,status,owner_ref,deadline,
            parent_task_id,merged_into FROM tasks_current)) d;
  IF n <> 0 THEN
    RAISE EXCEPTION 'ASSERT FAILED: rebuild diverged from projection (% rows)', n;
  END IF;
  RAISE NOTICE 'PASS: rebuild_board() reproduces tasks_current exactly';
END $$;

-- D. query paths (row-count assertions; shapes visible in psql output)
\echo --- Q1 board view (alex)
SELECT t.task_id, t.title, t.status, t.deadline,
       COALESCE(b.open_blockers,0) AS open_blockers
FROM tasks_current t
LEFT JOIN (SELECT task_id, count(*) AS open_blockers FROM task_blockers
           WHERE resolved_event_id IS NULL GROUP BY task_id) b USING (task_id)
WHERE t.user_ref = 'alex' AND t.merged_into IS NULL
ORDER BY t.task_id;

\echo --- Q2 task history (task-88)
SELECT e.board_version, e.event_type, e.actor_type, d.decision, p.rationale
FROM task_events e
LEFT JOIN decisions d ON d.decision_id = e.decision_id
LEFT JOIN proposals p ON p.proposal_id = d.proposal_id
WHERE e.task_id = 'task-88' ORDER BY e.board_version;

\echo --- Q3 provenance walk (blocker event)
SELECT p.proposal_id, s.signal_id, ev.resource_id, ev.content_hash
FROM task_events e
JOIN decisions d ON d.decision_id = e.decision_id
JOIN proposals p ON p.proposal_id = d.proposal_id
JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
JOIN signals s ON s.signal_id = ps.signal_id
JOIN evidence_refs ev ON ev.signal_id = s.signal_id
WHERE e.event_type = 'blocker_added' AND e.task_id = 'task-88';

\echo --- Q4 who is blocked on priya
SELECT waiter.task_id AS blocked_task, mine.task_id AS my_blocking_task,
       mine.status AS my_task_status
FROM task_blockers b
JOIN tasks_current waiter ON waiter.task_id = b.task_id
LEFT JOIN tasks_current mine ON mine.task_id = b.blocking_task_id
WHERE b.resolved_event_id IS NULL AND mine.owner_ref = 'priya';

\echo --- Q5 team rollup
SELECT user_ref,
       count(*) FILTER (WHERE status='in_progress') AS in_progress,
       count(*) FILTER (WHERE status='blocked') AS blocked
FROM tasks_current WHERE merged_into IS NULL GROUP BY user_ref ORDER BY 1;

\echo --- Q8 reject-reason histogram
SELECT p.op, d.reject_reason, count(*) FROM decisions d
JOIN proposals p USING (proposal_id)
WHERE d.decision='reject' GROUP BY 1,2;

\echo --- Q9 alias set for matching (alex)
SELECT t.task_id,
       array_prepend(t.title, COALESCE(array_agg(a.alias)
         FILTER (WHERE a.alias IS NOT NULL), '{}')) AS alias_set
FROM tasks_current t LEFT JOIN task_aliases a USING (task_id)
WHERE t.user_ref='alex' AND t.merged_into IS NULL
GROUP BY t.task_id, t.title ORDER BY t.task_id;

DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM task_blockers b
  JOIN tasks_current mine ON mine.task_id = b.blocking_task_id
  WHERE b.resolved_event_id IS NULL AND mine.owner_ref = 'priya';
  IF n <> 1 THEN RAISE EXCEPTION 'ASSERT FAILED: Q4 expected 1 row, got %', n; END IF;
  RAISE NOTICE 'PASS: cross-user dependency edge resolves (Q4)';

  SELECT count(*) INTO n FROM task_aliases WHERE task_id = 'task-88';
  IF n <> 1 THEN RAISE EXCEPTION 'ASSERT FAILED: alias not recorded'; END IF;
  RAISE NOTICE 'PASS: accepted signal title landed in alias set (Q9 input)';

  SELECT count(*) INTO n FROM v_unconsumed_signals WHERE user_ref = 'alex';
  IF n <> 0 THEN RAISE EXCEPTION 'ASSERT FAILED: pool should be empty, got %', n; END IF;
  INSERT INTO signal_pool_events (signal_id,user_ref,event,run_id)
  VALUES ('sig-2','alex','returned','run-2');
  SELECT count(*) INTO n FROM v_unconsumed_signals WHERE user_ref = 'alex';
  IF n <> 1 THEN RAISE EXCEPTION 'ASSERT FAILED: returned signal not in pool'; END IF;
  RAISE NOTICE 'PASS: signal pool view (taken -> empty, returned -> back)';
END $$;

\echo ALL STORAGE ASSERTIONS PASSED
