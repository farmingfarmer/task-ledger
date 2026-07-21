"""Tests for the reconciler.

Offline sections use TokenEmbedder (lexical cosine, so band placement is
controllable by wording) and scripted judges. The live section seeds a task
through apply_task_event, reconciles the real extraction signals written by
test_extraction.py, persists the packet, and asserts pool consumption --
skipping itself if the database is unreachable.

Run: python3 test_reconciler.py
"""

import copy
import json
import os
import sys

sys.path.insert(0, ".")
import embedders
import reconciler

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


def sig(**kw):
    base = {
        "signal_id": kw.get("signal_id", "sig-" + kw["title"][:12]),
        "signal_type": "status_update",
        "title": "", "description": None, "status_signal": None,
        "confidence": 0.85, "deadline_date": None,
        "deadline_precision": None, "deadline_raw": None,
        "blocker_desc": None, "blocker_party": None,
        "owner_candidates": [],
        "evidence": [{"source_type": "transcript_utterance",
                      "resource_id": kw.get("rid", "utt-x"),
                      "span": None, "content_hash": "h"}],
    }
    base.update(kw)
    base.pop("rid", None)
    return base


def task(**kw):
    base = {"task_id": "", "title": "", "status": "in_progress",
            "owner_ref": "aad-alex", "deadline": None, "aliases": [],
            "age_days": 10.0, "open_blockers": []}
    base.update(kw)
    return base


def judge_script(responses):
    """Returns a judge llm that pops canned JSON responses in order."""
    queue = list(responses)

    def llm(messages):
        return json.dumps(queue.pop(0))
    return llm


def make(judge=None):
    return reconciler.Reconciler(embed=embedders.TokenEmbedder(),
                                 judge_llm=judge)


BOARD = [
    task(task_id="task-ocr", title="Fix intake OCR crash",
         owner_ref="aad-dev",
         aliases=["intake OCR fix", "OCR crash intake"]),
    task(task_id="task-drift", title="Deploy drift monitor staging",
         owner_ref="aad-alex", status="proposed",
         aliases=["drift monitor staging deploy"]),
]


def test_auto_link_and_status():
    print("auto-link + status gate")
    r = make()
    s = sig(title="Deploy drift monitor staging",
            status_signal="in_progress",
            owner_candidates=[{"person_ref": "aad-alex", "confidence": 0.9,
                               "basis": "self_reference"}])
    packet, rep = r.reconcile("aad-alex", [s], BOARD, 7, "run-t")
    ok(len(packet["proposals"]) == 1
       and packet["proposals"][0]["op"] == "update_status"
       and packet["proposals"][0]["payload"]["to_status"] == "in_progress"
       and packet["proposals"][0]["target_task_id"] == "task-drift",
       "identical-title signal auto-links; forward transition proposed")
    ok(rep["judge_calls"] == 0, "no judge call above auto_link")
    ok(packet["board_version"] == 7 and packet["user_ref"] == "aad-alex",
       "packet carries board version and user")


def test_regression_conflict():
    print("status regression")
    board = [task(task_id="task-x", title="Ship weekly report automation",
                  status="completed",
                  aliases=["weekly report automation ship"])]
    r = make()
    s = sig(title="Ship weekly report automation",
            status_signal="in_progress")
    packet, _ = r.reconcile("aad-alex", [s], board, 1, "run-t")
    p = packet["proposals"][0]
    ok(p["conflict"] is not None
       and p["conflict"]["type"] == "stale_evidence",
       "completed -> in_progress flagged as regression, not silently applied")


def test_closure_gates():
    print("completion gates")
    # Owner self-reference: judged (never auto-link), single source suffices.
    j = judge_script([{"verdict": "same_task", "matched_task_id": "task-ocr",
                       "confidence": 0.92,
                       "deciding_factor": "same OCR crash deliverable"}])
    r = make(j)
    s = sig(signal_type="completion_claim", title="Fix OCR crash in intake",
            status_signal="completed", confidence=0.9,
            owner_candidates=[{"person_ref": "aad-dev", "confidence": 0.9,
                               "basis": "self_reference"}])
    packet, rep = r.reconcile("aad-dev", [s], BOARD, 3, "run-t")
    ok(rep["judge_calls"] == 1, "completion never auto-links: judge invoked")
    ok(len(packet["proposals"]) == 1
       and packet["proposals"][0]["op"] == "complete_task",
       "owner self-claim completes on a single source")

    # Third party, single source: held, not consumed.
    j2 = judge_script([{"verdict": "same_task", "matched_task_id": "task-ocr",
                        "confidence": 0.92, "deciding_factor": "same fix"}])
    r2 = make(j2)
    s2 = sig(signal_type="completion_claim",
             title="Fix OCR crash in intake", status_signal="completed",
             confidence=0.9,
             owner_candidates=[{"person_ref": "aad-priya",
                                "confidence": 0.8, "basis": "inferred"}])
    packet2, rep2 = r2.reconcile("aad-dev", [s2], BOARD, 3, "run-t")
    ok(packet2["proposals"] == [] and rep2["held"] == [s2["signal_id"]]
       and rep2["consumed"] == [],
       "third-party single-source completion held, signal stays pooled")

    # Third party with independent corroboration: proposes.
    j3 = judge_script([
        {"verdict": "same_task", "matched_task_id": "task-ocr",
         "confidence": 0.92, "deciding_factor": "same fix"},
        {"verdict": "same_task", "matched_task_id": "task-ocr",
         "confidence": 0.92, "deciding_factor": "same fix"}])
    r3 = make(j3)
    s3a = copy.deepcopy(s2)
    s3b = sig(signal_id="sig-corrob", signal_type="completion_claim",
              title="intake OCR crash fix", status_signal="completed",
              confidence=0.85, rid="mail-77",
              owner_candidates=[{"person_ref": "aad-priya",
                                 "confidence": 0.6, "basis": "inferred"}])
    packet3, rep3 = r3.reconcile("aad-dev", [s3a, s3b], BOARD, 3, "run-t")
    completes = [p for p in packet3["proposals"]
                 if p["op"] == "complete_task"]
    ok(len(completes) == 1
       and set(completes[0]["source_signals"]) ==
           {s3a["signal_id"], "sig-corrob"}
       and len(completes[0]["evidence"]) == 2,
       "two independent sources corroborate; one aggregated proposal")

    # Malformed judge output fails toward distinct -> no completion.
    r4 = make(lambda messages: "I think it matches task-ocr probably")
    packet4, rep4 = r4.reconcile("aad-dev", [copy.deepcopy(s)], BOARD, 3,
                                 "run-t")
    ok(packet4["proposals"] == [] and rep4["held"],
       "malformed judge output holds instead of guessing")


def test_assignment_ladder():
    print("assignment ladder")
    j = judge_script([{"verdict": "same_task", "matched_task_id": "task-ocr",
                       "confidence": 0.9, "deciding_factor": "same work"}] * 3)
    r = make(j)
    explicit = sig(signal_type="assignment", title="Fix intake OCR crash",
                   owner_candidates=[{"person_ref": "aad-priya",
                                      "confidence": 0.9,
                                      "basis": "explicit_assignment"}])
    packet, _ = r.reconcile("aad-dev", [explicit], BOARD, 1, "run-t")
    p = packet["proposals"][0]
    ok(p["op"] == "reassign" and p["payload"]["to_owner_ref"] == "aad-priya"
       and p["conflict"] is None,
       "explicit assignment reassigns an owned task cleanly")

    selfclaim = sig(signal_type="assignment", title="Fix intake OCR crash",
                    owner_candidates=[{"person_ref": "aad-priya",
                                       "confidence": 0.9,
                                       "basis": "self_reference"}])
    packet2, _ = make(judge_script(
        [{"verdict": "same_task", "matched_task_id": "task-ocr",
          "confidence": 0.9, "deciding_factor": "same"}])).reconcile(
        "aad-dev", [selfclaim], BOARD, 1, "run-t")
    p2 = packet2["proposals"][0]
    ok(p2["conflict"] is not None
       and p2["conflict"]["type"] == "competing_owner",
       "self-claim on an owned task raises competing_owner")

    inferred = sig(signal_type="assignment", title="Fix intake OCR crash",
                   owner_candidates=[{"person_ref": "aad-priya",
                                      "confidence": 0.9,
                                      "basis": "inferred"}])
    packet3, _ = make(judge_script(
        [{"verdict": "same_task", "matched_task_id": "task-ocr",
          "confidence": 0.9, "deciding_factor": "same"}])).reconcile(
        "aad-dev", [inferred], BOARD, 1, "run-t")
    ok(packet3["proposals"] == [],
       "inferred attribution never proposes a reassign")


def test_create_and_subtask():
    print("new work + subtask verdict")
    r = make()
    s = sig(signal_type="assignment",
            title="Wire retraining pipeline alerting hooks",
            status_signal=None, deadline_date="2026-07-31",
            deadline_precision="week", deadline_raw="end of next week",
            owner_candidates=[{"person_ref": "aad-dev", "confidence": 0.9,
                               "basis": "explicit_assignment"}])
    packet, _ = r.reconcile("aad-dev", [s], BOARD, 1, "run-t")
    p = packet["proposals"][0]
    ok(p["op"] == "create_task"
       and p["payload"]["owner_ref"] == "aad-dev"
       and p["payload"]["deadline"] == "2026-07-31"
       and "below merge threshold" in p["payload"]["match_note"],
       "unmatched assignment creates task with owner, deadline, match_note")

    j = judge_script([{"verdict": "subtask_of",
                       "matched_task_id": "task-drift",
                       "confidence": 0.85,
                       "deciding_factor": "component of the drift deploy"}])
    r2 = make(j)
    s2 = sig(signal_type="proposal",
             title="Drift monitor staging deploy alert thresholds",
             status_signal="proposed", confidence=0.7)
    packet2, _ = r2.reconcile("aad-alex", [s2], BOARD, 1, "run-t")
    p2 = packet2["proposals"][0]
    ok(p2["op"] == "add_subtask"
       and p2["payload"]["parent_task_id"] == "task-drift",
       "judge subtask_of verdict becomes add_subtask under the parent")


def test_deadline_and_blockers():
    print("deadline + blocker gates")
    r = make()
    vague = sig(signal_type="deadline_signal",
                title="Deploy drift monitor staging",
                deadline_precision="vague", deadline_raw="sometime soon")
    packet, rep = r.reconcile("aad-alex", [vague], BOARD, 1, "run-t")
    ok(packet["proposals"] == []
       and any(a["kind"] == "deadline_note" for a in rep["annotations"]),
       "vague deadline annotates, never proposes")

    dated = sig(signal_type="deadline_signal",
                title="Deploy drift monitor staging",
                deadline_precision="exact", deadline_raw="July 30",
                deadline_date="2026-07-30", confidence=0.8,
                owner_candidates=[{"person_ref": "aad-alex",
                                   "confidence": 0.9,
                                   "basis": "self_reference"}])
    packet2, _ = r.reconcile("aad-alex", [dated], BOARD, 1, "run-t")
    p = packet2["proposals"][0]
    ok(p["op"] == "update_deadline"
       and p["payload"]["to_deadline"] == "2026-07-30"
       and p["payload"]["raw_text"] == "July 30",
       "resolved differing deadline proposes with raw phrase")

    board = [task(task_id="task-prod",
                  title="Deploy drift monitor to production",
                  owner_ref="aad-priya",
                  aliases=["production drift monitor deploy"]),
             task(task_id="task-iam",
                  title="Provision Fargate IAM role cloud team",
                  owner_ref="aad-priya")]
    blk = sig(signal_type="blocker",
              title="Deploy drift monitor to production",
              status_signal="blocked",
              blocker_desc="Fargate IAM role stuck with cloud team",
              owner_candidates=[{"person_ref": "aad-priya",
                                 "confidence": 0.7, "basis": "inferred"}])
    packet3, _ = r.reconcile("aad-priya", [blk], board, 1, "run-t")
    p3 = packet3["proposals"][0]
    ok(p3["op"] == "add_blocker"
       and p3["payload"]["blocking_task_id"] == "task-iam",
       "blocker resolves its dependency edge to the IAM task")

    board2 = [task(task_id="task-prod",
                   title="Deploy drift monitor to production",
                   owner_ref="aad-priya", status="blocked",
                   aliases=["production drift monitor deploy"],
                   open_blockers=[{"blocker_id": 41,
                                   "description": "Fargate IAM role stuck "
                                                  "with cloud team"}])]
    cleared = sig(signal_type="blocker_resolved",
                  title="Deploy drift monitor to production",
                  blocker_desc="cloud team delivered the Fargate IAM role",
                  confidence=0.8)
    packet4, _ = r.reconcile("aad-priya", [cleared], board2, 1, "run-t")
    p4 = packet4["proposals"][0]
    ok(p4["op"] == "resolve_blocker"
       and p4["payload"]["blocker_id"] == "41",
       "blocker_resolved matches the open blocker by description")


def test_floors_and_mentions():
    print("floors + mentions")
    r = make()
    weak = sig(signal_type="status_update",
               title="Deploy drift monitor staging",
               status_signal="in_progress", confidence=0.45)
    packet, rep = r.reconcile("aad-alex", [weak], BOARD, 1, "run-t")
    ok(packet["proposals"] == []
       and rep["dropped"][0]["reason"] == "below_min_conf",
       "sub-floor signal dropped before gating")

    mention = sig(signal_type="task_mention",
                  title="the drift monitor staging deploy",
                  confidence=0.7)
    packet2, rep2 = r.reconcile("aad-alex", [mention], BOARD, 1, "run-t")
    ok(packet2["proposals"] == []
       and any(a["kind"] == "alias"
               and a["task_id"] == "task-drift" for a in rep2["annotations"]),
       "mention proposes nothing; feeds the alias set")


def test_cross_owner_guard():
    print("cross-owner guard")
    seen = {"called": False}

    def llm(messages):
        seen["called"] = True
        return json.dumps({"verdict": "distinct", "matched_task_id": None,
                           "confidence": 0.9,
                           "deciding_factor": "different owner context"})
    r = reconciler.Reconciler(embed=embedders.TokenEmbedder(), judge_llm=llm)
    s = sig(title="Fix intake OCR crash", status_signal="in_progress",
            owner_candidates=[{"person_ref": "aad-priya", "confidence": 0.9,
                               "basis": "self_reference"}])
    packet, _ = r.reconcile("aad-dev", [s], BOARD, 1, "run-t")
    ok(seen["called"], "identical title but different owner forces the judge")


def test_pg_live():
    print("postgres end-to-end (live)")
    dsn = os.environ.get(
        "DB_DSN",
        "host=/home/claude/pgsock port=5433 dbname=taskintel user=postgres")
    try:
        import psycopg2
        psycopg2.connect(dsn).close()
    except Exception as e:
        print("  skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return
    import board_io_pg
    import psycopg2
    conn = psycopg2.connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tasks_current WHERE task_id=%s",
                        ("task-devocr",))
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT apply_task_event('task-devocr','aad-dev',"
                    "'created','manual_edit',NULL,"
                    "%s::jsonb)",
                    (json.dumps({"title": "Fix OCR crash in intake",
                                 "status": "in_progress",
                                 "owner_ref": "aad-dev"}),))
            # Backdate so the completion youth gate (age < 1 day holds
            # 'done' echoes) does not trip on a freshly seeded task.
            cur.execute("UPDATE tasks_current SET created_at = "
                        "now() - interval '5 days' "
                        "WHERE task_id = 'task-devocr'")
    conn.close()

    signals = board_io_pg.load_signals(dsn, "aad-dev")
    tasks, version = board_io_pg.load_board(dsn, "aad-dev")
    ok(len(signals) >= 2 and any(t["task_id"] == "task-devocr"
                                 for t in tasks),
       "live pool + seeded board loaded ({0} signals, board v{1})".format(
           len(signals), version))

    j = judge_script([{"verdict": "same_task",
                       "matched_task_id": "task-devocr",
                       "confidence": 0.95,
                       "deciding_factor": "identical OCR crash fix"}] * 5)
    r = reconciler.Reconciler(embed=embedders.TokenEmbedder(), judge_llm=j)
    run_id = "test-live-run"
    packet, rep = r.reconcile("aad-dev", signals, tasks, version, run_id)
    ops = sorted(p["op"] for p in packet["proposals"])
    ok("complete_task" in ops and "create_task" in ops,
       "live reconcile: completion matches seeded task, assignment creates")

    result = board_io_pg.write_packet(dsn, packet, rep["consumed"],
                                      rep["annotations"], run_id)
    ok(result["proposals"] == len(packet["proposals"])
       and result["taken"] == len(rep["consumed"]),
       "packet persisted; consumed signals marked taken")
    replay = board_io_pg.write_packet(dsn, packet, rep["consumed"],
                                      rep["annotations"], run_id)
    ok(replay["proposals"] == 0 and replay["taken"] == 0,
       "packet write is idempotent on replay")
    left = board_io_pg.load_signals(dsn, "aad-dev")
    ok(len(left) == len(signals) - len(rep["consumed"]),
       "pool shrank by exactly the consumed count; held signals remain")


if __name__ == "__main__":
    test_auto_link_and_status()
    test_regression_conflict()
    test_closure_gates()
    test_assignment_ladder()
    test_create_and_subtask()
    test_deadline_and_blockers()
    test_floors_and_mentions()
    test_cross_owner_guard()
    test_pg_live()
    print("\nall {0} reconciler checks passed".format(CHECKS[0]))
