"""Tests for the digest layer.

The builder and renderers are pure, so most of this runs anywhere. The
live section checks the part that cannot be faked: that the project
loader never fetches the fields a progress-level reader must not see, and
that recipients come from current grants, so a lapsed grant stops the
digest with nothing to prune.

Run: python3 test_digest.py   (live section skips politely without a DB)
"""

import json
import os
import sys

sys.path.insert(0, ".")
import digest as dg

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


FULL = {
    "confirmed": [{"title": "Ship the intake fix", "event_type": "completed",
                   "decision": "accept", "occurred_at": "2026-08-05"}],
    "pending": {"undecided": 3, "conflicts": 1,
                "oldest_at": "2026-08-01T09:00:00"},
    "held": [{"title": "Sandbox guide edits done", "signal_type":
              "completion_claim", "sources": 1}],
    "quiet": [{"task_id": "t-1", "title": "Retraining alerting",
               "status": "in_progress", "quiet_days": 21}],
    "overdue": [{"task_id": "t-2", "title": "Drift monitor",
                 "deadline": "2026-07-31"}],
    "blocked_on": [{"my_title": "Ship drift monitoring",
                    "blocking_title": "Provision IAM role",
                    "blocking_owner": "priya",
                    "blocking_status": "in_progress"}],
    "blocking": [{"blocked_user": "dev", "blocked_title": "Model rollout"}],
    "meetings": [{"source_ref": "copilot-2026-08-03-Scrum",
                  "met_at": "2026-08-03", "claims": 11, "landed": 4,
                  "waiting": 1}],
    "latency": {"median_hours": 156.3, "n": 10},
}


def test_personal():
    print("personal digest")
    d = dg.build_personal("aad-dev", FULL)
    keys = [s["key"] for s in d["sections"]]
    ok(not d["quiet"] and "review" in keys and "confirmed" in keys,
       "a full week produces the sections it has material for")
    ok(keys.index("review") == 0,
       "what needs a decision leads, ahead of what was accomplished")
    urgent_titles = ("Waiting for you", "Past their date",
                     "You are waiting on", "Waiting on you")
    ok(d["headline"].startswith(urgent_titles),
       "the headline picks an urgent section, not the cheerful one "
       "({0})".format(d["headline"]))
    ok(d["headline"] == "Waiting for you: 3 items",
       "the headline counts the things, not the lines describing them "
       "({0})".format(d["headline"]))

    empty = dg.build_personal("aad-dev", {})
    ok(empty["quiet"] and not empty["sections"],
       "a week with nothing to say is marked quiet, not padded")
    ok("Nothing needs you" in empty["headline"],
       "a quiet digest says so in one line")

    text = dg.render_text(d)
    ok("WAITING FOR YOU" in text and "3 proposals awaiting review" in text
       and "oldest 6 days old" in text,
       "text rendering carries the counts and the age of the queue")
    ok("python3 cli/review.py --as aad-dev" in text,
       "the digest tells you how to act on it")
    ok("156" in text or "6.5 days" in text,
       "the footer carries talk-to-tracking latency")


def test_truncation_and_escaping():
    print("bounds and escaping")
    many = {"overdue": [{"title": "Task {0}".format(i),
                         "deadline": "2026-07-01"} for i in range(20)]}
    d = dg.build_personal("aad-dev", many)
    sec = d["sections"][0]
    ok(len(sec["lines"]) == dg.MAX_LINES_PER_SECTION and sec["more"] == 14,
       "long sections are capped and say how many were left out")
    ok("and 14 more" in dg.render_text(d), "the remainder is stated, not hidden")

    nasty = {"overdue": [{"title": "<script>alert(1)</script> & co",
                          "deadline": "2026-07-01"}]}
    html = dg.render_html(dg.build_personal("aad-dev", nasty))
    ok("<script>alert" not in html and "&lt;script&gt;" in html,
       "titles are escaped: a task title can never inject markup")


def test_card():
    print("adaptive card")
    card = dg.build_digest_card(dg.build_personal("aad-dev", FULL))
    ok(card["type"] == "AdaptiveCard" and card["body"],
       "a card payload is produced for the bot host to deliver")
    size = len(json.dumps(card).encode("utf-8"))
    ok(size < dg.MAX_CARD_BYTES,
       "the card stays under the Teams ceiling ({0} bytes)".format(size))
    huge = {"overdue": [{"title": "T" * 300, "deadline": "2026-07-01"}
                        for _ in range(200)]}
    big = dg.build_personal("aad-dev", huge)
    big["sections"][0]["lines"] = ["T" * 300 for _ in range(400)]
    try:
        dg.build_digest_card(big)
        ok(False, "oversized card should have raised")
    except ValueError as e:
        ok("over the" in str(e),
           "an oversized card fails at build, not on delivery")


def test_project_shape():
    print("project digest")
    proj = {"project_id": "proj-drift", "name": "Drift monitoring"}
    d = dg.build_project(proj, {
        "changes": [{"title": "Ship drift monitoring",
                     "event_type": "status_changed",
                     "occurred_at": "2026-08-04", "actor_type":
                     "user_decision"}],
        "open": [{"title": "Ship drift monitoring", "status": "blocked",
                  "owner_ref": "alex", "deadline": "2026-07-31",
                  "overdue": True}],
        "counts": {"in_progress": 2, "blocked": 1, "proposed": 0,
                   "completed": 3}})
    blob = json.dumps(d)
    ok(not d["quiet"] and "Where it stands" in blob,
       "a project digest reports state and what changed")
    for leaked in ("rationale", "confidence", "decided_by", "signal",
                   "evidence"):
        ok(leaked not in blob,
           "no {0} appears anywhere in a project digest".format(leaked))
    ok("progress level" in d["footer"],
       "the digest says which level it was written at")


def test_live():
    print("loader (live)")
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
    import digest_io_pg

    user = "digest-fixture-user"
    project = "proj-digest-fixture"
    conn = psycopg2.connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO projects (project_id, name, owner_ref)
                VALUES (%s,'Digest fixture',%s)
                ON CONFLICT (project_id) DO NOTHING""", (project, user))
            cur.execute("SELECT 1 FROM tasks_current WHERE task_id = %s",
                        ("task-digest-fixture",))
            if cur.fetchone() is None:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'created','manual_edit',"
                    "NULL,%s::jsonb)",
                    ("task-digest-fixture", user, json.dumps({
                        "title": "Digest fixture task", "status": "blocked",
                        "owner_ref": user, "project_id": project})))
            cur.execute("""
                SELECT 1 FROM v_current_grants WHERE grantee_ref = %s
                  AND subject_type = 'project' AND subject_id = %s""",
                ("digest-watcher", project))
            if cur.fetchone() is None:
                cur.execute("SELECT grant_access(%s,%s,'project',%s,%s,%s)",
                            (user, "digest-watcher", project, "progress",
                             "digest test"))
    conn.close()

    proj, data = digest_io_pg.load_project(dsn, project, days=30)
    blob = json.dumps(data, default=str)
    ok(proj is not None and data["counts"]["blocked"] >= 1,
       "the project loader reports current state")
    for column in ("rationale", "confidence", "decided_by", "signal_id",
                   "content_hash"):
        ok(column not in blob,
           "the project query never fetches {0} -- redaction is "
           "structural".format(column))

    people = digest_io_pg.recipients(dsn, project)
    ok(any(r["grantee_ref"] == "digest-watcher" for r in people),
       "recipients are whoever currently holds a grant")

    personal = digest_io_pg.load_personal(dsn, user, days=30)
    ok("pending" in personal and "held" in personal
       and isinstance(personal["overdue"], list),
       "the personal loader returns every section the builder expects")
    d = dg.build_personal(user, personal)
    ok(d["kind"] == "personal" and d["audience"] == user,
       "a live personal digest assembles end to end")


if __name__ == "__main__":
    test_personal()
    test_truncation_and_escaping()
    test_card()
    test_project_shape()
    test_live()
    print("\nall {0} digest checks passed".format(CHECKS[0]))
