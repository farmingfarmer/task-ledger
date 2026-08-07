"""Tests for the share CLI against the live database.

The rules being tested are enforced in SQL (grant_access / revoke_access),
not in the CLI -- which is the point. These checks prove the CLI reports
them faithfully rather than routing around them:

  - you may share your own board, and only your own
  - only a project owner may share a project
  - a grant can expire, and expiry is not revocation
  - revoking removes access while the history stays queryable

Run: python3 test_share.py   (skips politely without a database)
"""

import io
import os
import sys

sys.path.insert(0, ".")

CHECKS = [0]

ALICE = "share-alice"
BOB = "share-bob"
CAROL = "share-carol"
PROJECT = "proj-share-fixture"


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


class Args(object):
    """A stand-in for the parsed namespace, so the tests drive run()
    directly instead of shelling out."""

    def __init__(self, command, **kw):
        self.command = command
        self.to = kw.get("to")
        self.board = kw.get("board")
        self.project = kw.get("project")
        self.level = kw.get("level", "progress")
        self.days = kw.get("days")
        self.note = kw.get("note")


def granted_by_count(cur, who, subject_id):
    """How many grants this person has written for that subject. A refused
    call must leave this at whatever it was -- normally zero."""
    cur.execute("""
        SELECT count(*) AS n FROM access_grant_events
        WHERE granted_by = %s AND subject_id = %s AND event = 'granted'""",
        (who, subject_id))
    return cur.fetchone()["n"]


def current(cur, grantee, subject_id):
    cur.execute("""
        SELECT level, expires_at FROM v_current_grants
        WHERE grantee_ref = %s AND subject_id = %s""", (grantee, subject_id))
    return cur.fetchone()


def main():
    dsn = os.environ.get(
        "DB_DSN",
        "host=/home/claude/pgsock port=5433 dbname=taskintel user=postgres")
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(dsn)
    except Exception as e:
        print("skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return
    import share

    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO projects (project_id, name, owner_ref)
                VALUES (%s, 'Share fixture project', %s)
                ON CONFLICT (project_id) DO NOTHING""", (PROJECT, ALICE))
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    print("granting")
    out = io.StringIO()
    rc = share.run(dsn, ALICE, Args("grant", to=BOB, board=ALICE,
                                    level="full_trace", note="teammate"), out)
    row = current(cur, BOB, ALICE)
    ok(rc == 0 and row and row["level"] == "full_trace"
       and row["expires_at"] is None,
       "granting your own board works and lasts until revoked")
    ok("full trace includes the provenance rail" in out.getvalue(),
       "full_trace grants say out loud what they expose")

    print("the rules are SQL, not CLI")
    # Assert on the audit trail rather than on current access: a refusal
    # must write NOTHING, which stays true however many times this suite
    # has run against the database before.
    out = io.StringIO()
    rc = share.run(dsn, BOB, Args("grant", to=CAROL, board=ALICE), out)
    ok(rc == 3 and "refused" in out.getvalue()
       and granted_by_count(cur, BOB, ALICE) == 0,
       "nobody may share someone else's board on their behalf")
    out = io.StringIO()
    rc = share.run(dsn, BOB, Args("grant", to=CAROL, project=PROJECT), out)
    ok(rc == 3 and granted_by_count(cur, BOB, PROJECT) == 0,
       "only the project owner may share the project")
    out = io.StringIO()
    rc = share.run(dsn, ALICE, Args("grant", to=CAROL, project=PROJECT), out)
    ok(rc == 0 and current(cur, CAROL, PROJECT) is not None,
       "the project owner may share it")

    print("expiry")
    out = io.StringIO()
    rc = share.run(dsn, ALICE, Args("grant", to=CAROL, board=ALICE, days=30),
                   out)
    row = current(cur, CAROL, ALICE)
    ok(rc == 0 and row["expires_at"] is not None
       and "expires" in out.getvalue(),
       "a time-boxed grant records when it lapses")
    cur.execute("""
        INSERT INTO access_grant_events (grantee_ref, subject_type,
          subject_id, level, event, granted_by, expires_at)
        VALUES (%s,'user',%s,'progress','granted',%s,
                now() - interval '1 minute')""", (CAROL, ALICE, ALICE))
    conn.commit()
    ok(current(cur, CAROL, ALICE) is None,
       "a lapsed grant disappears from current access with nobody revoking")

    print("revoking")
    out = io.StringIO()
    rc = share.run(dsn, ALICE, Args("revoke", to=BOB, board=ALICE), out)
    ok(rc == 0 and current(cur, BOB, ALICE) is None,
       "revoking removes current access")
    cur.execute("""
        SELECT count(*) AS n FROM access_grant_events
        WHERE grantee_ref = %s AND subject_id = %s""", (BOB, ALICE))
    ok(cur.fetchone()["n"] >= 2,
       "the grant and the revocation both stay in the audit trail")
    out = io.StringIO()
    rc = share.run(dsn, ALICE, Args("revoke", to=BOB, board=ALICE), out)
    ok(rc == 0 and "nothing to revoke" in out.getvalue(),
       "revoking twice is a no-op that says so")

    print("listing")
    out = io.StringIO()
    share.run(dsn, ALICE, Args("list"), out)
    text = out.getvalue()
    ok("Access {0} has given".format(ALICE) in text
       and "project {0}".format(PROJECT) in text,
       "list shows what you have shared")
    out = io.StringIO()
    share.run(dsn, CAROL, Args("list"), out)
    ok("project {0} at progress".format(PROJECT) in out.getvalue(),
       "list shows what you hold, and from whom")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
    print("\nall {0} share CLI checks passed".format(CHECKS[0]))
