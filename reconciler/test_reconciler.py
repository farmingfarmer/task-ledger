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


def _sha_title(t):
    return reconciler._sha("candidate", reconciler._norm(t))


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

    # Malformed judge output is never PARSED into a verdict -- but at
    # 0.89 similarity it no longer strands the claim either: the ask band
    # hands the question to the owner, and the card says out loud that no
    # judge ruled. What this test always guarded against was garbage being
    # trusted as a match; a human question trusts nothing.
    r4 = make(lambda messages: "I think it matches task-ocr probably")
    packet4, rep4 = r4.reconcile("aad-dev", [copy.deepcopy(s)], BOARD, 3,
                                 "run-t")
    ok(len(packet4["proposals"]) == 1
       and packet4["proposals"][0]["op"] == "complete_task"
       and "wording alone" in packet4["proposals"][0]["rationale"]
       and packet4["proposals"][0]["payload"].get("kind") == "complete_task",
       "malformed judge output is not guessed at: the claim becomes a "
       "question that admits no judge ruled, and closes nothing by itself")


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


def test_packet_local_create_dedup():
    print("packet-local create dedup")
    # Empty board: every proposal signal becomes a create, which is the
    # shape the first live run produced -- one piece of work on one card
    # twice, as a vague restatement and a specific description.
    def prop(sid, title, conf, rid, **kw):
        return sig(signal_id=sid, signal_type="proposal", title=title,
                   confidence=conf, rid=rid, **kw)

    r = make()
    a = prop("sig-a", "Deploy drift monitor staging", 0.8, "utt-a")
    b = prop("sig-b", "Staging deploy of the drift monitor", 0.7, "utt-b")
    packet, rep = r.reconcile("aad-alex", [a, b], [], 1, "run-t")
    ok(len(packet["proposals"]) == 1
       and packet["proposals"][0]["payload"]["title"]
       == "Deploy drift monitor staging",
       "near-verbatim creates merge into the higher-confidence proposal")
    p = packet["proposals"][0]
    ok(p["source_signals"] == ["sig-a", "sig-b"] and len(p["evidence"]) == 2
       and abs(p["confidence"] - 0.94) < 1e-6,
       "merged create pools evidence, lineage, and noisy-OR confidence")
    ok(rep["judge_calls"] == 0 and len(rep["merged"]) == 1
       and "merged" in p["rationale"],
       "auto band needs no judge; the merge is reported and visible")

    # Gray band: similarity alone cannot decide, so the merge judge does.
    gray = [prop("sig-c", "Deploy drift monitor staging environment", 0.8,
                 "utt-c"),
            prop("sig-d", "Deploy drift monitor staging", 0.7, "utt-d")]
    same = judge_script([{"verdict": "same_task",
                          "matched_task_id": "pending-kept",
                          "confidence": 0.9,
                          "deciding_factor": "same staging deploy"}])
    packet2, rep2 = make(same).reconcile("aad-alex", copy.deepcopy(gray), [],
                                         1, "run-t")
    ok(len(packet2["proposals"]) == 1 and rep2["judge_calls"] == 1,
       "gray band merges only on a same_task verdict from the merge judge")

    distinct = judge_script([{"verdict": "distinct", "matched_task_id": None,
                              "confidence": 0.9,
                              "deciding_factor": "staging is not production"}])
    packet3, _ = make(distinct).reconcile("aad-alex", copy.deepcopy(gray), [],
                                          1, "run-t")
    ok(len(packet3["proposals"]) == 2,
       "judge verdict distinct keeps both creates on the card")

    packet4, rep4 = make().reconcile("aad-alex", copy.deepcopy(gray), [], 1,
                                     "run-t")
    ok(len(packet4["proposals"]) == 2 and rep4["judge_calls"] == 0,
       "no judge (offline tick) fails open: gray-band creates both survive")

    far = [prop("sig-e", "Rebuild feature store documentation", 0.8, "utt-e"),
           prop("sig-f", "Provision Fargate IAM role", 0.8, "utt-f")]
    packet5, rep5 = make(judge_script([])).reconcile("aad-alex", far, [], 1,
                                                     "run-t")
    ok(len(packet5["proposals"]) == 2 and rep5["judge_calls"] == 0,
       "unrelated creates never reach the judge, never merge")

    # An ownerless create is invisible on every board; a twin that names an
    # owner contributes it rather than losing it in the merge.
    ownerless = prop("sig-g", "Build the ingest replay harness", 0.9, "utt-g")
    owned = sig(signal_id="sig-h", signal_type="assignment",
                title="Ingest replay harness build", confidence=0.7,
                rid="utt-h",
                owner_candidates=[{"person_ref": "aad-dev", "confidence": 0.9,
                                   "basis": "explicit_assignment"}])
    packet6, _ = make().reconcile("aad-alex", [ownerless, owned], [], 1,
                                  "run-t")
    ok(len(packet6["proposals"]) == 1
       and packet6["proposals"][0]["payload"]["owner_ref"] == "aad-dev",
       "survivor inherits the owner its twin carried")


def test_cross_board_duplicate_annotation():
    print("cross-board duplicate annotation")
    r = make()
    unowned = sig(signal_id="sig-cb", signal_type="proposal",
                  title="Rebuild feature store documentation", confidence=0.8)
    others = [task(task_id="task-priya-1",
                   title="Feature store documentation rebuild",
                   user_ref="aad-priya", owner_ref="aad-priya")]
    packet, rep = r.reconcile("aad-alex", [unowned], [], 1, "run-t",
                              all_tasks=others)
    p = packet["proposals"][0]
    ok(len(packet["proposals"]) == 1 and p["op"] == "create_task",
       "cross-board match annotates only: the create still stands")
    ok("similar task exists on aad-priya's board" in p["rationale"]
       and len(p["rationale"]) <= 200,
       "rationale names the other board, within the schema's 200 chars")
    ok(any(a["kind"] == "cross_board_duplicate"
           and a["task_id"] == "task-priya-1" for a in rep["annotations"]),
       "the finding is recorded as an annotation with the matched task")

    far = [task(task_id="task-priya-2", title="Provision Fargate IAM role",
                user_ref="aad-priya", owner_ref="aad-priya")]
    packet2, rep2 = r.reconcile("aad-alex", [copy.deepcopy(unowned)], [], 1,
                                "run-t", all_tasks=far)
    ok("similar task" not in packet2["proposals"][0]["rationale"]
       and not [a for a in rep2["annotations"]
                if a["kind"] == "cross_board_duplicate"],
       "unrelated work on another board raises no note")

    owned = sig(signal_id="sig-cb2", signal_type="assignment",
                title="Rebuild feature store documentation", confidence=0.8,
                owner_candidates=[{"person_ref": "aad-alex",
                                   "confidence": 0.9,
                                   "basis": "explicit_assignment"}])
    packet3, _ = r.reconcile("aad-alex", [owned], [], 1, "run-t",
                             all_tasks=others)
    ok("similar task" not in packet3["proposals"][0]["rationale"],
       "creates that already name an owner are not cross-board checked")

    mine = [task(task_id="task-mine", title="Feature store documentation "
                 "rebuild", user_ref="aad-alex", owner_ref="aad-alex")]
    packet4, _ = r.reconcile("aad-alex", [copy.deepcopy(unowned)], mine, 1,
                             "run-t", all_tasks=mine)
    ok(all("similar task" not in p["rationale"]
           for p in packet4["proposals"]),
       "the reviewer's own board is matching territory, not a duplicate note")


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


def test_auto_apply_gate():
    print("auto-apply gate")
    board = [task(task_id="task-drift", title="Deploy drift monitor staging",
                  owner_ref="aad-alex", status="proposed",
                  aliases=["drift monitor staging deploy"])]
    mine = sig(title="Deploy drift monitor staging",
               status_signal="in_progress", confidence=0.9,
               owner_candidates=[{"person_ref": "aad-alex",
                                  "confidence": 0.95,
                                  "basis": "self_reference"}])

    # Off by default: the same signal goes to a person, as it always has.
    r = make()
    packet, rep = r.reconcile("aad-alex", [mine], board, 1, "run-a")
    ok(len(packet["proposals"]) == 1 and not rep["auto_applied"],
       "with the gate off nothing applies itself -- the default is unchanged")

    def enabled(**over):
        rr = make()
        rr.policy["auto_apply"] = dict(rr.policy["auto_apply"])
        rr.policy["auto_apply"]["enabled"] = True
        rr.policy["auto_apply"].update(over)
        return rr

    r2 = enabled()
    packet2, rep2 = r2.reconcile("aad-alex", [copy.deepcopy(mine)], board, 1,
                                 "run-a")
    ok(packet2["proposals"] == [] and len(rep2["auto_applied"]) == 1
       and rep2["auto_applied"][0]["op"] == "update_status",
       "own words, own task, own board, forward, auto-linked: applies and "
       "leaves the queue entirely")

    # Someone else reporting your progress is the corroboration case.
    third = sig(signal_id="sig-third", title="Deploy drift monitor staging",
                status_signal="in_progress", confidence=0.9,
                owner_candidates=[{"person_ref": "aad-priya",
                                   "confidence": 0.9,
                                   "basis": "inferred"}])
    packet3, rep3 = enabled().reconcile("aad-alex", [third], board, 1, "run-a")
    # Cross-owner forces the judge; with none configured it holds. Either
    # way the point is the same: it did not move the board by itself.
    ok(not rep3["auto_applied"]
       and (packet3["proposals"] or rep3["held"]),
       "a third party reporting your progress still needs you")

    # Not your board.
    theirs = [task(task_id="task-drift", title="Deploy drift monitor staging",
                   owner_ref="aad-priya", status="proposed",
                   aliases=["drift monitor staging deploy"])]
    packet4, rep4 = enabled().reconcile("aad-alex", [copy.deepcopy(mine)],
                                        theirs, 1, "run-a")
    ok(not rep4["auto_applied"]
       and (packet4["proposals"] or rep4["held"]),
       "and it never moves a task owned by somebody else")

    # A regression carries a conflict flag, and conflicts want eyes.
    done = [task(task_id="task-drift", title="Deploy drift monitor staging",
                 owner_ref="aad-alex", status="completed",
                 aliases=["drift monitor staging deploy"])]
    packet5, rep5 = enabled().reconcile("aad-alex", [copy.deepcopy(mine)],
                                        done, 1, "run-a")
    ok(len(packet5["proposals"]) == 1 and not rep5["auto_applied"],
       "a backwards move is a conflict, and conflicts are never automatic")

    # Only the ops the policy names.
    packet6, rep6 = enabled(ops=["complete_task"]).reconcile(
        "aad-alex", [copy.deepcopy(mine)], board, 1, "run-a")
    ok(len(packet6["proposals"]) == 1 and not rep6["auto_applied"],
       "an op the policy does not list is never applied, whatever else fits")


def test_candidate_register():
    print("candidate register")
    r = make()
    # One person mentioning something once is noise, and stays noise.
    m1 = sig(signal_id="sig-c1", signal_type="task_mention",
             title="the retention sweep", confidence=0.7,
             author_ref="aad-alex")
    packet, rep = r.reconcile("aad-alex", [m1], [], 0, "run-c")
    ok(packet["proposals"] == []
       and len(rep["candidate_sightings"]) == 1
       and not rep["candidate_promotions"],
       "an unmatched mention is registered as a sighting, not discarded")

    # Same work, different words, a second speaker: now it is a thing the
    # team keeps returning to and nobody has written down.
    r2 = make()
    m2 = sig(signal_id="sig-c2", signal_type="task_mention",
             title="retention sweep work", confidence=0.7,
             author_ref="aad-priya")
    prior = [{"candidate_id": "cand-1", "title": "the retention sweep",
              "sightings": 1, "speakers": 1, "state": "sighted"}]
    packet2, rep2 = r2.reconcile("aad-alex", [m2], [], 0, "run-c",
                                 candidates=prior)
    ok(len(rep2["candidate_sightings"]) == 1
       and rep2["candidate_sightings"][0]["candidate_id"] == "cand-1",
       "a reworded mention lands on the candidate already registered")
    ok(len(packet2["proposals"]) == 1
       and packet2["proposals"][0]["op"] == "create_task"
       and rep2["candidate_promotions"],
       "two sightings by two speakers promote to a create_task PROPOSAL")

    # Same speaker twice used to be refused as one person's opinion. Under
    # min_sightings 2 it is taken, and that inversion is the whole cost of
    # the change -- asserted here so it is a decision on the record rather
    # than a regression somebody discovers later.
    r3 = make()
    m3 = sig(signal_id="sig-c3", signal_type="task_mention",
             title="the retention sweep again", confidence=0.7,
             author_ref="aad-alex")
    prior_same = [{"candidate_id": "cand-1", "title": "the retention sweep",
                   "sightings": 1, "speakers": 0, "state": "sighted",
                   "lead": {"signal_id": "sig-prior", "description": None,
                            "confidence": 0.7, "owner_candidates": [],
                            "evidence": [{"source_type":
                                          "transcript_utterance",
                                          "resource_id": "utt-prior",
                                          "span": None,
                                          "content_hash": "h"}]}}]
    packet3, rep3 = r3.reconcile("aad-alex", [m3], [], 0, "run-c",
                                 candidates=prior_same)
    ok(len(packet3["proposals"]) == 1 and rep3["candidate_promotions"],
       "one person saying it twice now promotes: persistence is 2, because "
       "no candidate on either real corpus ever reached 3")

    # Already over the bar, and nobody said it again this run. It must
    # still promote: otherwise relaxing the rule does nothing until the
    # words happen to recur, which is how 92 candidates sat untouched
    # through a policy change written to promote them.
    r5 = make()
    standing = [{"candidate_id": "cand-2", "title": "the retention sweep",
                 "sightings": 4, "speakers": 1, "state": "sighted",
                 "lead": {"signal_id": "sig-old", "description": None,
                          "confidence": 0.7, "owner_candidates": [],
                          "evidence": [{"source_type": "transcript_utterance",
                                        "resource_id": "utt-old",
                                        "span": None, "content_hash": "h"}]}}]
    packet5, rep5 = r5.reconcile("aad-alex", [], [], 0, "run-c",
                                 candidates=standing)
    ok(len(packet5["proposals"]) == 1 and rep5["candidate_promotions"],
       "persistence alone promotes: four mentions by one speaker is work "
       "the team keeps returning to, and the shipped rule takes it")

    # ...but only if it can be cited. No evidence, no reviewable card.
    r6 = make()
    bare = [{"candidate_id": "cand-3", "title": "the retention sweep",
             "sightings": 4, "speakers": 1, "state": "sighted", "lead": None}]
    packet6, _ = r6.reconcile("aad-alex", [], [], 0, "run-c",
                              candidates=bare)
    ok(packet6["proposals"] == [],
       "and one with nothing to trace back to keeps waiting")

    # Under 'any' the bars are still bars: too few of both stays waiting.
    # One sighting by one speaker is the case that must never promote --
    # otherwise every passing remark becomes a card and the queue is unusable.
    r7 = make()
    thin = [{"candidate_id": "cand-4", "title": "the retention sweep",
             "sightings": 1, "speakers": 1, "state": "sighted",
             "lead": {"signal_id": "sig-thin", "description": None,
                      "confidence": 0.7, "owner_candidates": [],
                      "evidence": [{"source_type": "transcript_utterance",
                                    "resource_id": "utt-thin",
                                    "span": None, "content_hash": "h"}]}}]
    packet7, _rep7 = r7.reconcile("aad-alex", [], [], 0, "run-c",
                                  candidates=thin)
    ok(packet7["proposals"] == [],
       "a single mention by one speaker clears neither bar and keeps waiting")

    # The assessment is reported whether or not anything promoted: "0
    # promoted" alone cannot distinguish a bar nothing reached from a
    # policy file whose edit never saved.
    a7 = _rep7["candidate_assessment"]
    ok(a7["assessed"] == 1 and a7["short"] == 1 and a7["promoted"] == 0
       and a7["best_sightings"] == 1 and a7["bar"] == "2 sighting(s) or 2 "
       "speaker(s)",
       "and the run reports the bar in force and how far the best "
       "candidate fell short of it")

    # A mention that DOES match the board is still an alias, not a candidate.
    r4 = make()
    known = sig(signal_id="sig-c4", signal_type="task_mention",
                title="Deploy drift monitor staging", confidence=0.7,
                author_ref="aad-alex")
    _, rep4 = r4.reconcile("aad-alex", [known], BOARD, 1, "run-c")
    ok(not rep4["candidate_sightings"]
       and any(a["kind"] == "alias" for a in rep4["annotations"]),
       "a mention of tracked work feeds the alias set, not the register")


def test_no_target_waits():
    print("no target waits, then expires")
    r = make()
    # Progress reported on work nobody has accepted as a task yet. The board
    # is empty, so there is nothing to match -- and this used to be consumed
    # and silently lost, which is how a whole meeting's progress vanished.
    early = sig(signal_type="status_update", title="Migrate the archive",
                status_signal="in_progress", confidence=0.9, pooled_days=1.0)
    packet, rep = r.reconcile("aad-alex", [early], [], 0, "run-w")
    ok(packet["proposals"] == [] and rep["held"] == [early["signal_id"]]
       and not rep["consumed"],
       "progress on a task that does not exist yet is held, not consumed")

    blocked = sig(signal_type="blocker", title="Migrate the archive",
                  blocker_desc="waiting on the storage team",
                  confidence=0.9, pooled_days=1.0)
    _, rep2 = r.reconcile("aad-alex", [blocked], [], 0, "run-w")
    ok(rep2["held"] == [blocked["signal_id"]] and not rep2["dropped"],
       "so is a blocker -- it used to be dropped outright")

    # Waiting ends: past the corroboration window it is dropped, and the
    # reason names what it waited for rather than disappearing quietly.
    stale = sig(signal_type="status_update", title="Migrate the archive",
                status_signal="in_progress", confidence=0.9,
                pooled_days=99.0)
    _, rep3 = r.reconcile("aad-alex", [stale], [], 0, "run-w")
    ok(not rep3["held"]
       and rep3["dropped"][0]["reason"] == "expired_no_open_target",
       "a signal that waited past the window expires with a named reason")

    # A finished task genuinely cannot be blocked: still a drop.
    done = task(task_id="task-done", title="Migrate the archive",
                status="completed")
    _, rep4 = r.reconcile("aad-alex", [blocked], [done], 1, "run-w")
    ok(rep4["dropped"] and rep4["dropped"][0]["reason"] == "target_terminal",
       "blocking a completed task is a real drop, not a wait")


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
        "host=/tmp/pgsock port=5433 dbname=taskintel user=postgres")
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

    conn = psycopg2.connect(dsn)
    with conn:
        with conn.cursor() as cur:
            # Put the whole window's claims in dev's pool, explicitly.
            # This used to re-pool only what was already dev's and rely on
            # the store's fallback having fanned unowned claims out to
            # every participant -- so the fixture silently depended on the
            # pooling default, and changing that default (unowned claims
            # now go to their speaker, not to everyone) quietly shrank the
            # packet the review CLI suite expects. A fixture should state
            # what it needs.
            cur.execute(
                "INSERT INTO signal_pool_events (signal_id, user_ref, "
                "event, run_id) SELECT s.signal_id, 'aad-dev', 'returned', "
                "'test-repool' FROM signals s "
                "JOIN extraction_batches b USING (batch_id) "
                "WHERE b.source_ref = 'meeting-standup-2026-07-20'")
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
    # Per-invocation run id: a constant here made reruns byte-identical
    # replays (packet_id and salted proposal_ids identical), so
    # write_packet correctly returned zeros and the test misread its own
    # idempotency as failure -- exposed by a pristine Windows database
    # whose board_version never advanced between runs.
    import time
    run_id = "test-live-{0}".format(int(time.time()))
    packet, rep = r.reconcile("aad-dev", signals, tasks, version, run_id)
    ops = sorted(p["op"] for p in packet["proposals"])
    # The op assertions need the fixture board this chain was built on:
    # extraction writes the signals, this test seeds task-devocr, and
    # test_review then ANSWERS the packet -- closing task-devocr and
    # creating the assignment's task. On a second run against the same
    # database the completion has nothing open to close and the assignment
    # matches the task the last run created, both correctly. The chain is
    # single-shot per database; the persistence assertions below are the
    # portable ones and still run.
    seeded = next((t for t in tasks if t["task_id"] == "task-devocr"), None)
    if len(tasks) == 1 and seeded["status"] not in ("completed",
                                                    "cancelled"):
        ok("complete_task" in ops and "create_task" in ops,
           "live reconcile: completion matches seeded task, assignment "
           "creates")
    else:
        print("  skipped: this database already answered the fixture "
              "packet (board has {0} task(s), task-devocr is {1}) -- "
              "op assertions need a fresh scratch DB".format(
                  len(tasks), seeded["status"]))

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


def test_uncitable_proposal_dropped():
    print("proposal with no evidence")
    # A signal whose evidence did not survive flattening. The packet schema
    # requires minItems 1 and validates the packet whole, so before this was
    # handled the ValidationError escaped reconcile() and the caller lost the
    # entire run for that user -- including the good proposal beside it.
    r = make()
    good = sig(title="Deploy drift monitor staging",
               status_signal="in_progress",
               owner_candidates=[{"person_ref": "aad-alex",
                                  "confidence": 0.9,
                                  "basis": "self_reference"}])
    bad = sig(signal_id="sig-noev", signal_type="proposal",
              title="Draft the onboarding runbook", confidence=0.9,
              evidence=[],
              owner_candidates=[{"person_ref": "aad-alex",
                                 "confidence": 0.9,
                                 "basis": "self_reference"}])
    packet, rep = r.reconcile("aad-alex", [good, bad], BOARD, 7, "run-t")
    ok(all(p["evidence"] for p in packet["proposals"]),
       "no proposal reaches the packet without a sentence behind it")
    ok(any(d["reason"] == "no_evidence" for d in rep["dropped"]),
       "and it is dropped with a reason rather than raised as an exception")
    ok(any(p["target_task_id"] == "task-drift" for p in packet["proposals"]),
       "the reviewable proposal beside it still lands -- one bad signal no "
       "longer costs the whole packet")


def test_untracked_work_is_registered_whatever_the_type():
    print("untracked work from any signal type")
    # A status_update whose task does not exist. It is held, as before --
    # but it is now also recorded, because a signal with no target is by
    # definition naming work on nobody's board.
    r = make()
    s = sig(signal_id="sig-su", signal_type="status_update",
            title="Synth kit project Bayesian conditional graph",
            status_signal="in_progress", confidence=0.8,
            author_ref="aad-alex")
    packet, rep = r.reconcile("aad-alex", [s], [], 0, "run-u")
    ok(rep["held"] == ["sig-su"],
       "a status update with no target still waits for one")
    ok(len(rep["candidate_sightings"]) == 1
       and rep["candidate_sightings"][0]["signal_id"] == "sig-su",
       "and is registered as a candidate, which only task_mention used to "
       "be -- describing progress on work is stronger evidence it is real "
       "than merely naming it")
    ok(packet["proposals"] == [],
       "registering it proposes nothing: the human gate is untouched")

    # Two types naming the same work land on one candidate, not two.
    r2 = make()
    m = sig(signal_id="sig-tm", signal_type="task_mention",
            title="Synth kit project", confidence=0.7, author_ref="aad-alex")
    _packet2, rep2 = r2.reconcile("aad-alex", [m, s], [], 0, "run-u")
    ids = {c["candidate_id"] for c in rep2["candidate_sightings"]}
    ok(len(rep2["candidate_sightings"]) == 2 and len(ids) == 1,
       "a mention and a status update about the same work corroborate each "
       "other instead of registering separately")


def test_promotion_respects_what_the_evidence_said():
    print("promotion shape")
    # Corroborated only by closure claims. "I resolved it" is not a reason
    # to open a task, and the contract forbids creating one already closed.
    r = make()
    done = [{"candidate_id": "cand-done", "title": "Brittney's access issue",
             "sightings": 3, "speakers": 1, "state": "sighted",
             "lead": {"signal_id": "sig-done",
                      "signal_type": "completion_claim",
                      "status_signal": "completed", "description": None,
                      "confidence": 0.9, "owner_candidates": [],
                      "evidence": [{"source_type": "transcript_utterance",
                                    "resource_id": "utt-d", "span": None,
                                    "content_hash": "h"}]}}]
    packet, rep = r.reconcile("aad-alex", [], [], 0, "run-p", candidates=done)
    ok(packet["proposals"] == [] and not rep["candidate_promotions"],
       "work said to be finished does not become a new open task")
    ok(rep["candidate_assessment"]["closure_only"] == 1,
       "and the run says why, rather than looking like a bar nothing met")

    # Progress on untracked work creates it in the state described, not as
    # untouched -- 'proposed' reads as nobody has picked it up.
    r2 = make()
    prog = [{"candidate_id": "cand-wip", "title": "Synth kit project",
             "sightings": 2, "speakers": 1, "state": "sighted",
             "lead": {"signal_id": "sig-wip", "signal_type": "status_update",
                      "status_signal": "in_progress", "description": None,
                      "confidence": 0.9,
                      "owner_candidates": [{"person_ref": "aad-alex",
                                            "confidence": 0.9,
                                            "basis": "self_reference"}],
                      "evidence": [{"source_type": "transcript_utterance",
                                    "resource_id": "utt-w", "span": None,
                                    "content_hash": "h"}]}}]
    packet2, _ = r2.reconcile("aad-alex", [], [], 0, "run-p", candidates=prog)
    ok(len(packet2["proposals"]) == 1
       and packet2["proposals"][0]["payload"]["status"] == "in_progress",
       "a task somebody said they were working on is created in progress")

    # A closure claim alongside a mention: the mention is what gets cited.
    r3 = make()
    m = sig(signal_id="sig-m", signal_type="task_mention",
            title="Brittney's access issue", confidence=0.7,
            author_ref="aad-priya")
    both = [{"candidate_id": _sha_title("Brittney's access issue"),
             "title": "Brittney's access issue", "sightings": 2,
             "speakers": 1, "state": "sighted",
             "lead": done[0]["lead"]}]
    packet3, rep3 = r3.reconcile("aad-alex", [m], [], 0, "run-p",
                                 candidates=both)
    ok(len(packet3["proposals"]) == 1
       and packet3["proposals"][0]["source_signals"] == ["sig-m"],
       "with a mention present the create cites the mention, not the "
       "claim that the work is over")


def test_assertions_about_work_are_not_work():
    print("not work")
    r = make()
    noise = sig(signal_id="sig-nb", signal_type="task_mention",
                title="No blockers", confidence=0.8, author_ref="aad-alex")
    real = sig(signal_id="sig-real", signal_type="task_mention",
               title="Notify the team about the rollout", confidence=0.8,
               author_ref="aad-alex")
    packet, rep = r.reconcile("aad-alex", [noise, real], [], 0, "run-n")
    ok(not any(c["signal_id"] == "sig-nb"
               for c in rep["candidate_sightings"]),
       "'No blockers' is a fact about tasks, not a task, and never reaches "
       "the register")
    ok(any(d["reason"] == "not_work" for d in rep["dropped"]),
       "and it is dropped with a reason, so the count is visible rather "
       "than the row simply never appearing")
    ok(any(c["signal_id"] == "sig-real"
           for c in rep["candidate_sightings"]),
       "a title that merely starts with the same letters is untouched -- "
       "the filter matches words, not prefixes")
    ok(packet["proposals"] == [],
       "and neither becomes a proposal on one sighting")


def test_ask_band_asks_a_person():
    print("ask band")
    board = [task(task_id="task-skr", title="synth kit project rollout",
                  status="proposed", owner_ref="aad-alex",
                  aliases=[])]
    para = sig(title="synth kit project rollout capability",
               signal_type="status_update", status_signal="in_progress",
               owner_candidates=[{"person_ref": "aad-alex",
                                  "confidence": 0.9,
                                  "basis": "self_reference"}])
    # 0.894 similarity, five tokens (no short-title penalty), no judge
    # available: the exact shape that used to hold forever. Now it is a
    # question for the reviewer, who is a better judge than a CPU llama.
    r = make()
    packet, rep = r.reconcile("aad-alex", [para], board, 1, "run-a")
    ok(len(packet["proposals"]) == 1
       and packet["proposals"][0]["op"] == "update_status"
       and packet["proposals"][0]["target_task_id"] == "task-skr",
       "a strong paraphrase with no judge becomes a proposal instead of a "
       "permanent hold")
    ok("wording alone" in packet["proposals"][0]["rationale"],
       "and the card admits its basis, so the reviewer knows exactly what "
       "they are ruling on")
    ok(not rep["held"], "nothing is held that a person could answer")
    ok(r._auto_ok == set(),
       "and it may NEVER auto-apply: wording alone does not skip a human, "
       "even for the owner's own claim")

    # A confident judge still outranks the ask band -- asking runs only
    # after the judge has had its chance.
    j = judge_script([{"verdict": "same_task",
                       "matched_task_id": "task-skr", "confidence": 0.92,
                       "deciding_factor": "same rollout deliverable"}])
    r2 = make(j)
    packet2, rep2 = r2.reconcile("aad-alex", [para], board, 1, "run-a")
    ok(len(packet2["proposals"]) == 1
       and "wording alone" not in packet2["proposals"][0]["rationale"]
       and rep2["judge_calls"] == 1,
       "with a judge available and confident, the link is judged, not "
       "asked")

    # Below the floor the old caution stands: hold, do not guess.
    r3 = make()
    weak = sig(title="review the payment intake flow",
               signal_type="status_update", status_signal="in_progress")
    board3 = [task(task_id="task-pif", title="payment intake flow",
                   status="proposed", aliases=[])]
    # 0.866 raw, but judge_floor-band closures aside: this one IS in the
    # band -- use a genuinely weak pair instead.
    weak2 = sig(title="unrelated words entirely here now",
                signal_type="status_update", status_signal="in_progress")
    _p3, rep3 = r3.reconcile("aad-alex", [weak2], board3, 1, "run-a")
    ok(rep3["held"] == [weak2["signal_id"]],
       "no resemblance still holds: the ask band is a band, not a bypass")
    hd = rep3["held_detail"]
    ok(len(hd) == 1 and hd[0]["signal_id"] == weak2["signal_id"]
       and hd[0]["closest"] == "payment intake flow"
       and hd[0]["similarity"] is not None,
       "and the hold says why -- which task it almost was and how close -- "
       "so a 0.87 paraphrase and a 0.12 fragment stop looking identical "
       "in the log")

    # A closure may ASK -- the card goes to the owner and closes nothing
    # by itself -- but only when the speaker IS the owner. This assertion
    # used to say the opposite ("does not close anything"), and holding
    # that line meant "I completed it", said by the owner about their own
    # tracked work, was held forever at 0.84: the most valuable sentence
    # the system hears, refused on principle. The principle was about
    # auto-linking, and an ask is not an auto-link.
    r4 = make()
    done = sig(title="synth kit project rollout capability",
               signal_type="completion_claim", status_signal="completed",
               confidence=0.9, author_ref="aad-alex", owner_candidates=[])
    _p4, rep4 = r4.reconcile("aad-alex", [done], board, 1, "run-a")
    ok(len(_p4["proposals"]) == 1
       and _p4["proposals"][0]["op"] == "complete_task"
       and "wording alone" in _p4["proposals"][0]["rationale"],
       "the owner reporting their own work finished becomes a question "
       "for the owner -- even when the extractor left owner_candidates "
       "empty, because author_ref IS the speaker and the policy always "
       "said 'speaker is not owner' is what needs corroboration")

    # The live scenario that exposed the young-task guard: a board
    # rebuilt this morning, every record hours old, and the owner's own
    # completion claims -- waiting in the pool since before the records
    # existed -- refused for lack of corroboration. Record age is not
    # work age.
    r4b = make()
    fresh_board = [task(task_id="task-skr", status="proposed",
                        title="synth kit project rollout",
                        owner_ref="aad-alex", age_days=0.05, aliases=[])]
    _p4b, _rep4b = r4b.reconcile("aad-alex", [copy.deepcopy(done)],
                                 fresh_board, 1, "run-a")
    ok(len(_p4b["proposals"]) == 1
       and _p4b["proposals"][0]["op"] == "complete_task",
       "an hours-old task record does not disqualify its owner's claim: "
       "the register creates records long after the work, so the claim "
       "arriving immediately is the pipeline working")

    # A NON-owner's uncorroborated claim in the same band still holds:
    # asking is allowed, closing someone's work on one bystander's say-so
    # is not.
    r5 = make()
    hearsay = sig(signal_id="sig-hearsay",
                  title="synth kit project rollout capability",
                  signal_type="completion_claim", status_signal="completed",
                  confidence=0.9, author_ref="aad-priya",
                  owner_candidates=[])
    _p5, rep5 = r5.reconcile("aad-alex", [hearsay], board, 1, "run-a")
    ok(_p5["proposals"] == [] and rep5["held"] == ["sig-hearsay"],
       "a bystander's uncorroborated completion claim still waits for a "
       "second source")


if __name__ == "__main__":
    test_auto_link_and_status()
    test_regression_conflict()
    test_closure_gates()
    test_assignment_ladder()
    test_create_and_subtask()
    test_packet_local_create_dedup()
    test_cross_board_duplicate_annotation()
    test_deadline_and_blockers()
    test_floors_and_mentions()
    test_auto_apply_gate()
    test_candidate_register()
    test_assertions_about_work_are_not_work()
    test_promotion_respects_what_the_evidence_said()
    test_untracked_work_is_registered_whatever_the_type()
    test_no_target_waits()
    test_cross_owner_guard()
    test_ask_band_asks_a_person()
    test_uncitable_proposal_dropped()
    test_pg_live()
    print("\nall {0} reconciler checks passed".format(CHECKS[0]))
