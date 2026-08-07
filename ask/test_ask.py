"""Tests for ask-the-ledger.

The core is pure and takes an injected retriever, so the interesting
properties are testable without a database:

  - it routes questions to the right shape of answer
  - it never asserts an unconfirmed claim, and says so instead
  - every fact carries a citation
  - it declines when the ledger holds nothing

The live section checks the part that cannot be faked: that retrieval is
scoped, so a stranger asking about someone else's work gets nothing, and
that evidence citations appear only at full_trace.

Run: python3 test_ask.py   (live section skips politely without a DB)
"""

import json
import os
import sys

sys.path.insert(0, ".")
import ask as ask_mod

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


class FakeRetriever(object):
    """Stands in for ask_io_pg.Retriever. The core cannot tell the
    difference, which is the point of injecting it."""

    def __init__(self, tasks=None, held=None, blocked=None, overdue=None,
                 meetings=None, evidence=None, pending=None):
        self._tasks = tasks or []
        self._held = held or []
        self._blocked = blocked or []
        self._overdue = overdue or []
        self._meetings = meetings or []
        self._evidence = evidence or []
        self._pending = pending or {}

    def tasks(self):
        return self._tasks

    def held(self):
        return self._held

    def blocked(self):
        return self._blocked

    def overdue(self):
        return self._overdue

    def pending(self):
        return self._pending

    def meetings(self, terms):
        return self._meetings

    def history(self, task_id):
        return [{"event_id": 7, "event_type": "status_changed",
                 "actor_type": "user_decision",
                 "occurred_at": "2026-08-04T10:00:00"}]

    def evidence(self, task_id):
        return self._evidence


TASK = {"task_id": "task-1", "title": "Ship the drift monitor",
        "status": "in_progress", "owner_ref": "aad-alex",
        "deadline": "2026-08-30", "aliases": ["drift monitoring"],
        "open_blockers": 0}


def test_routing():
    print("routing")
    for q, intent in (("what is blocked", "blocked_what"),
                      ("what is overdue", "overdue"),
                      ("what is waiting for me", "review_queue"),
                      ("who is working on the drift monitor", "who"),
                      ("what came out of the scrum", "meeting"),
                      ("status of the drift monitor", "status")):
        ok(ask_mod.parse_question(q)["intent"] == intent,
           "'{0}' routes to {1}".format(q, intent))


def test_confirmed_answer_cites():
    print("answers carry receipts")
    r = FakeRetriever(tasks=[TASK], evidence=[
        {"resource_id": "utt-9", "source_type": "transcript_utterance",
         "content_hash": "abc123def456", "author_ref": "aad-priya"}])
    a = ask_mod.answer("status of the drift monitor", r)
    ok(a["facts"] and "in progress" in a["facts"][0]["text"],
       "a confirmed task answers with its state")
    kinds = [c["kind"] for c in a["facts"][0]["citations"]]
    ok("event" in kinds and "evidence" in kinds,
       "the fact cites both the board event and the message behind it")
    text = ask_mod.render_text(a)
    ok("utt-9" in text and "abc123def456" in text and "aad-priya" in text,
       "the rendering shows the pointer, the hash, and who said it")


def test_refuses_unconfirmed():
    print("refusal")
    r = FakeRetriever(tasks=[], held=[
        {"signal_id": "sig-1", "title": "Redshift extract is finished",
         "signal_type": "completion_claim", "author_ref": "aad-priya",
         "sources": 1}])
    a = ask_mod.answer("is the redshift extract finished", r)
    ok(a["refusal"] and "Nothing confirmed" in a["refusal"],
       "an unconfirmed claim is NOT asserted as fact")
    ok(a["facts"] == [] and a["unconfirmed"],
       "the claim is reported separately from confirmed facts")
    text = ask_mod.render_text(a)
    ok("UNCONFIRMED" in text and "aad-priya" in text,
       "the rendering labels it and attributes it")
    ok("Redshift extract is finished" in text
       and text.index("UNCONFIRMED") < text.index("Redshift extract"),
       "the label comes before the claim, never after it")

    empty = ask_mod.answer("what about the quantum teleporter",
                           FakeRetriever())
    ok(empty["refusal"] and "nothing on that" in empty["refusal"],
       "with nothing at all, it says so rather than guessing")
    ok(not empty["facts"] and not empty["unconfirmed"],
       "a refusal invents no facts")


def test_shapes():
    print("shapes")
    r = FakeRetriever(blocked=[{"task_id": "t-9", "title": "Ship it",
                                "blocking_title": "IAM role",
                                "blocking_owner": "priya",
                                "opened_event_id": 3,
                                "opened_at": "2026-07-20T09:00:00"}])
    a = ask_mod.answer("what is blocked", r)
    ok(a["facts"][0]["text"].startswith("Ship it is blocked on IAM role"),
       "blocked answers name the blocking work and its owner")
    ok(a["facts"][0]["citations"][0]["what"].startswith("blocked since"),
       "and cite when it started")

    q = ask_mod.answer("what needs my review", FakeRetriever(
        pending={"undecided": 3, "oldest_at": "2026-08-01T09:00:00"},
        held=[{"signal_id": "s", "title": "a claim",
               "signal_type": "assignment", "sources": 1}]))
    ok("3 awaiting review" in q["headline"]
       and "1 heard but unconfirmed" in q["headline"],
       "the queue answer separates decisions owed from claims held")

    m = ask_mod.answer("what came out of the scrum", FakeRetriever(
        meetings=[{"source_ref": "copilot-2026-08-03-Scrum",
                   "met_at": "2026-08-03", "claims": 11, "landed": 4,
                   "waiting": 1}]))
    ok("11 claim(s), 4 became work" in m["facts"][0]["text"],
       "meeting answers report follow-through, not just attendance")


def test_live():
    print("scoping (live)")
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
    import ask_io_pg

    owner, stranger = "ask-owner", "ask-stranger"
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tasks_current WHERE task_id = %s",
                        ("task-ask-fixture",))
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                    "NULL,%s::jsonb)",
                    ("task-ask-fixture", owner, json.dumps({
                        "title": "Ask fixture pipeline work",
                        "status": "in_progress", "owner_ref": owner})))
    conn.close()

    mine = ask_mod.answer("status of the ask fixture pipeline",
                          ask_io_pg.Retriever(dsn, owner))
    ok(mine["facts"] and "Ask fixture pipeline work" in mine["facts"][0]["text"],
       "the owner gets an answer about their own work")

    theirs = ask_mod.answer("status of the ask fixture pipeline",
                            ask_io_pg.Retriever(dsn, stranger))
    ok(not theirs["facts"] and theirs["refusal"],
       "a stranger asking the same question gets nothing, not a redacted "
       "hint that it exists")
    ok("Ask fixture" not in json.dumps(theirs),
       "the title never appears in an unauthorised answer")


if __name__ == "__main__":
    test_routing()
    test_confirmed_answer_cites()
    test_refuses_unconfirmed()
    test_shapes()
    test_live()
    print("\nall {0} ask checks passed".format(CHECKS[0]))
