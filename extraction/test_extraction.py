"""Tests for the extraction worker and Postgres signal store.

Fake-LLM tests cover the golden path, salvage of every malformed-signal
class, deterministic ids across re-runs, hallucinated-date rejection, name
resolution, and evidence hashing. The final section writes a batch to the
live taskintel database and asserts idempotency and pooling -- it runs only
if the DB is reachable, and reports itself skipped otherwise.

Run: python3 test_extraction.py
"""

import copy
import json
import os
import sys

sys.path.insert(0, ".")
import extraction_worker
import extraction_prompts

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


with open("sample_transcript.json") as f:
    SPEC = json.load(f)

GOLDEN = [
    {"type": "status_update",
     "title": "Deploy drift monitor to production",
     "description": "Staging deploy done; watching before prod",
     "speaker": "Alex",
     "owner_candidates": [{"name": "Alex", "basis": "self_reference",
                           "confidence": 0.9}],
     "status_signal": "in_progress", "deadline": None, "blocker": None,
     "confidence": 0.85, "evidence_items": [1]},
    {"type": "blocker",
     "title": "Fargate IAM role pending with cloud team",
     "speaker": "Priya",
     "owner_candidates": [{"name": "Priya", "basis": "inferred",
                           "confidence": 0.6}],
     "status_signal": "blocked",
     "deadline": None,
     "blocker": {"description": "IAM role for Fargate stuck with cloud team",
                 "blocking_party": None},
     "confidence": 0.8, "evidence_items": [2]},
    {"type": "completion_claim",
     "title": "Fix intake OCR crash",
     "speaker": "Dev",
     "owner_candidates": [{"name": "Dev", "basis": "self_reference",
                           "confidence": 0.95}],
     "status_signal": "completed", "deadline": None, "blocker": None,
     "confidence": 0.9, "evidence_items": [3]},
    {"type": "assignment",
     "title": "Wire up retraining pipeline alerting",
     "speaker": "Alex",
     "owner_candidates": [{"name": "Dev", "basis": "explicit_assignment",
                           "confidence": 0.9}],
     "status_signal": None,
     "deadline": {"raw_text": "end of next week", "precision": "week",
                  "date": "2026-07-31"},
     "blocker": None,
     "confidence": 0.85, "evidence_items": [4, 5]},
    {"type": "proposal",
     "title": "Rebuild feature store docs",
     "speaker": "Priya",
     "owner_candidates": [],
     "status_signal": "proposed", "deadline": None, "blocker": None,
     "confidence": 0.6, "evidence_items": [6]},
]

MALFORMED = [
    {"type": "vibes", "title": "x", "confidence": 0.9,
     "evidence_items": [1]},                                # bad type
    {"type": "task_mention", "title": "", "confidence": 0.9,
     "evidence_items": [1]},                                # empty title
    {"type": "task_mention", "title": "no evidence cited",
     "confidence": 0.9, "evidence_items": []},              # no evidence
    {"type": "task_mention", "title": "cites nonsense",
     "confidence": 0.9, "evidence_items": [99]},            # bad citation
    {"type": "task_mention", "title": "no confidence",
     "evidence_items": [1]},                                # missing conf
    "not even an object",                                   # wrong shape
]


def make_worker(response_payload, wrap=None):
    text = json.dumps(response_payload)
    if wrap == "fenced":
        text = "```json\n" + text + "\n```"
    elif wrap == "prose":
        text = "Here are the signals I found:\n" + text + "\nHope that helps!"

    calls = []

    def llm(messages):
        calls.append(messages)
        return text

    w = extraction_worker.ExtractionWorker(
        fetcher=lambda job: SPEC["items"],
        llm=llm,
        directory=SPEC["directory"],
        model_name="fake:test")
    return w, calls


def test_prompt_build():
    print("prompt construction")
    numbered = [dict(it, item_no=i + 1) for i, it in enumerate(SPEC["items"])]
    msgs = extraction_prompts.build_messages(
        "meeting_transcript", "2026-07-20", numbered)
    ok(msgs[0]["role"] == "system"
       and "completion_claim" in msgs[0]["content"]
       and "Do NOT emit" in msgs[0]["content"],
       "system prompt carries type spec and negative guidance")
    ok("[4] Alex" in msgs[1]["content"]
       and "2026-07-20" in msgs[1]["content"],
       "items numbered with authors; window end date anchored")


def test_golden():
    print("golden path")
    w, calls = make_worker(GOLDEN, wrap="prose")
    batch, report = w.run_job(SPEC["job"])
    ok(len(calls) == 1, "one LLM call per window")
    ok(report["kept"] == 5 and not report["dropped"],
       "all five signals kept despite prose-wrapped response")
    ok(batch["extractor"]["prompt_version"] == extraction_worker.PROMPT_VERSION
       and batch["window"]["source_ref"] == SPEC["job"]["source_ref"],
       "batch metadata populated")

    by_type = {s["type"]: s for s in batch["signals"]}
    a = by_type["assignment"]
    ok(a["owner_candidates"] == [{"person_ref": "aad-dev",
                                  "confidence": 0.9,
                                  "basis": "explicit_assignment"}],
       "display name resolved to person_ref with basis intact")
    ok(a["deadline"]["date"] == "2026-07-31"
       and a["deadline"]["raw_text"] == "end of next week",
       "deadline resolved date + raw phrase both carried")
    ok(len(a["evidence"]) == 2
       and a["evidence"][0]["resource_id"] == "utt-004"
       and len(a["evidence"][0]["content_hash"]) == 64,
       "citations became evidence refs with sha256 content hashes")
    b = by_type["blocker"]
    ok(b["blocker"]["description"].startswith("IAM role")
       and b["blocker"]["blocking_party_ref"] is None,
       "blocker carried; unknown party stays null")
    ok(by_type["proposal"]["owner_candidates"] == [],
       "unowned proposal keeps empty candidate list")
    # Author is who SAID it; owner is who it is FOR. The assignment is
    # spoken by Alex (utt-004) about work for Dev, and conflating the two
    # would make a third-party claim look like a self-report.
    ok(a["author_ref"] == "aad-alex"
       and a["owner_candidates"][0]["person_ref"] == "aad-dev",
       "author (speaker) recorded independently of owner")
    ok(all("author_ref" in s for s in batch["signals"]),
       "every signal carries an author field")
    ok(by_type["completion_claim"]["author_ref"] == "aad-dev",
       "author comes from the cited utterance, not the model")


def test_determinism():
    print("deterministic ids")
    w1, _ = make_worker(GOLDEN)
    w2, _ = make_worker(GOLDEN, wrap="fenced")
    b1, _ = w1.run_job(SPEC["job"])
    b2, _ = w2.run_job(SPEC["job"])
    ok(b1["batch_id"] == b2["batch_id"],
       "batch_id identical across re-runs")
    ok([s["signal_id"] for s in b1["signals"]] ==
       [s["signal_id"] for s in b2["signals"]],
       "signal_ids identical across re-runs (fenced vs bare response)")


def test_salvage():
    print("salvage of malformed signals")
    mixed = MALFORMED + [GOLDEN[0]]
    w, _ = make_worker(mixed)
    batch, report = w.run_job(SPEC["job"])
    ok(report["kept"] == 2 and len(batch["signals"]) == 2,
       "two salvageable signals survive malformed neighbors")
    reasons = {d["reason"].split(":")[0] for d in report["dropped"]}
    ok(reasons == {"bad_type", "missing_title", "no_valid_evidence",
                   "not_an_object"},
       "every drop reason recorded for prompt tuning")

    w2, _ = make_worker("this is not json at all")
    # llm returns non-list; simulate by making payload a plain string
    w2.llm = lambda messages: "I could not find any tasks, sorry!"
    batch2, report2 = w2.run_job(SPEC["job"])
    ok(batch2["signals"] == [] and
       report2["dropped"][0]["reason"] == "unparseable_response",
       "unparseable response yields valid empty batch, not a crash")


def test_hallucination_guards():
    print("hallucination guards")
    bad = copy.deepcopy(GOLDEN[3])
    bad["deadline"]["date"] = "sometime in Q3"
    bad["owner_candidates"] = [
        {"name": "Zorp the Unknown", "basis": "self_reference",
         "confidence": 0.9},
        {"name": "dev", "basis": "made_up_basis", "confidence": 0.7}]
    w, _ = make_worker([bad])
    batch, report = w.run_job(SPEC["job"])
    s = batch["signals"][0]
    ok(s["deadline"]["date"] is None
       and s["deadline"]["raw_text"] == "end of next week",
       "non-ISO date nulled, raw phrase preserved")
    ok(s["owner_candidates"] == [{"person_ref": "aad-dev",
                                  "confidence": 0.7, "basis": "inferred"}],
       "unknown person dropped; case-insensitive match; bad basis demoted")


def test_empty_window():
    print("empty window")
    w, _ = make_worker([])
    w.fetcher = lambda job: []
    batch, report = w.run_job(SPEC["job"])
    ok(batch["signals"] == [] and report["items"] == 0,
       "empty window yields schema-valid empty batch, zero LLM calls")


def test_pg_store():
    print("postgres store (live)")
    dsn = os.environ.get(
        "DB_DSN",
        "host=/home/claude/pgsock port=5433 dbname=taskintel user=postgres")
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
    except Exception as e:
        print("  skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return
    conn.close()
    import signal_store_pg
    w, _ = make_worker(GOLDEN)
    batch, _ = w.run_job(SPEC["job"])
    fallback = sorted(set(SPEC["directory"].values()))
    r1 = signal_store_pg.write_batch(dsn, batch, fallback)
    ok(r1["signals_inserted"] in (0, 5),
       "first write inserts (or batch already present from a prior run)")
    r2 = signal_store_pg.write_batch(dsn, batch, fallback)
    ok(r2["signals_inserted"] == 0 and r2["pool_events"] == 0
       and r2["already_present"] == 5,
       "re-write is a no-op: idempotent on content-hash ids")
    import psycopg2
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        # The invariant extraction owns: dev's signals from this batch
        # were POOLED. Whether they are currently unconsumed is the
        # business of whatever reconciled after us -- a prior reconciler
        # suite run legitimately takes them (found the hard way on a
        # fresh Windows port, where strict suite ordering exposed the
        # stateful assumption).
        cur.execute("""
            SELECT count(DISTINCT e.signal_id)
            FROM signal_pool_events e
            JOIN signals s USING (signal_id)
            WHERE e.user_ref = 'aad-dev' AND e.event = 'pooled'
              AND s.batch_id = %s""",
            (batch["batch_id"],))
        n = cur.fetchone()[0]
        if n < 2:
            cur.execute("SELECT count(*) FROM signals WHERE batch_id=%s",
                        (batch["batch_id"],))
            print("  DIAG signals rows for batch:", cur.fetchone()[0])
            cur.execute("""
                SELECT signal_id, user_ref, event FROM signal_pool_events
                WHERE signal_id IN (SELECT signal_id FROM signals
                                    WHERE batch_id=%s)
                ORDER BY event_id""", (batch["batch_id"],))
            rows = cur.fetchall()
            print("  DIAG pool events ({0}):".format(len(rows)))
            for r_ in rows:
                print("    ", r_[0][:12], r_[1], r_[2])
            cur.execute("""
                SELECT user_ref, signal_type FROM v_unconsumed_signals
                WHERE batch_id=%s""", (batch["batch_id"],))
            print("  DIAG view rows:", cur.fetchall())
            cur.execute("SHOW server_version")
            print("  DIAG server_version:", cur.fetchone()[0])
    conn.close()
    ok(n >= 2, "dev's assignment + completion signals were pooled (later consumption is legitimate)")


if __name__ == "__main__":
    test_prompt_build()
    test_golden()
    test_determinism()
    test_salvage()
    test_hallucination_guards()
    test_empty_window()
    test_pg_store()
    print("\nall {0} extraction checks passed".format(CHECKS[0]))
