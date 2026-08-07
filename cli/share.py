"""Task Ledger share CLI: grant and revoke access from the terminal.

The dashboard API is READ ONLY on purpose -- a viewer-facing surface that
cannot write is a security argument you get to make for free -- and grant
writes were routed to the bot's /share command instead. The bot is not
wired yet, so until now sharing your board meant hand-written SQL, which is
the kind of gap that quietly turns a two-level access model into "everyone
is an owner".

This is review.py's sibling: same consenting-adult identity model (--as is
trusted local identity, right for a single-operator machine), same single
write path (grant_access / revoke_access, which enforce who may share
what), same refusal to invent a second one. When the bot ships, both
surfaces call the same two functions.

  python3 share.py --as alex --dsn "..." list
  python3 share.py --as alex --dsn "..." grant --to vic --board alex \
      --level progress --days 30 --note "drift launch"
  python3 share.py --as alex --dsn "..." grant --to vic \
      --project proj-drift --level progress
  python3 share.py --as alex --dsn "..." revoke --to vic --board alex

Two things the SQL enforces and this CLI merely reports: only you may share
your own board (no manager sharing someone else's work), and only a project
owner may share a project. Grants may expire; expiry is not revocation, and
the audit trail keeps the difference.

Python 3.8 compatible. Requires psycopg2.
"""

import argparse
import datetime
import sys

import psycopg2
import psycopg2.extras


def dsn_hint():
    if sys.platform == "win32":
        return ('set DB_DSN=host=localhost port=5433 dbname=taskintel '
                'user=%USERNAME%')
    return ('export DB_DSN="host=localhost port=5432 dbname=taskintel '
            'user=$(whoami)"')


def subject_of(args):
    """--board X or --project Y, never both."""
    if bool(args.board) == bool(args.project):
        raise SystemExit("choose exactly one of --board or --project")
    if args.board:
        return "user", args.board
    return "project", args.project


def cmd_list(cur, user, out):
    cur.execute("""
        SELECT grantee_ref, subject_type, subject_id, level, occurred_at,
               expires_at
        FROM v_current_grants WHERE granted_by = %s
        ORDER BY subject_type, subject_id, grantee_ref""", (user,))
    given = cur.fetchall()
    cur.execute("""
        SELECT subject_type, subject_id, level, granted_by, expires_at
        FROM v_current_grants WHERE grantee_ref = %s
        ORDER BY subject_type, subject_id""", (user,))
    held = cur.fetchall()

    out.write("Access {0} has given:\n".format(user))
    if not given:
        out.write("  (nothing shared)\n")
    for g in given:
        out.write("  {0} -> {1} {2} at {3}{4}\n".format(
            g["grantee_ref"], g["subject_type"], g["subject_id"], g["level"],
            _expiry_note(g["expires_at"])))
    out.write("Access {0} holds:\n".format(user))
    if not held:
        out.write("  (nothing shared with you)\n")
    for g in held:
        out.write("  {0} {1} at {2}, from {3}{4}\n".format(
            g["subject_type"], g["subject_id"], g["level"], g["granted_by"],
            _expiry_note(g["expires_at"])))
    return 0


def _expiry_note(expires_at):
    if expires_at is None:
        return "  (until revoked)"
    days = (expires_at - datetime.datetime.now(
        datetime.timezone.utc)).days
    return "  (expires {0}, in {1} day{2})".format(
        expires_at.date().isoformat(), days, "" if days == 1 else "s")


def cmd_grant(cur, user, args, out):
    stype, sid = subject_of(args)
    expires = None
    if args.days:
        expires = (datetime.datetime.now(datetime.timezone.utc)
                   + datetime.timedelta(days=args.days))
    cur.execute("SELECT grant_access(%s,%s,%s,%s,%s,%s,%s)",
                (user, args.to, stype, sid, args.level, args.note, expires))
    out.write("granted: {0} may see {1} {2} at {3}{4}\n".format(
        args.to, stype, sid, args.level, _expiry_note(expires)))
    if args.level == "full_trace":
        out.write("  full trace includes the provenance rail: rationales, "
                  "confidence, and the messages behind every fact.\n")
    return 0


def cmd_revoke(cur, user, args, out):
    stype, sid = subject_of(args)
    cur.execute("""
        SELECT 1 FROM v_current_grants
        WHERE grantee_ref = %s AND subject_type = %s AND subject_id = %s""",
        (args.to, stype, sid))
    if cur.fetchone() is None:
        out.write("nothing to revoke: {0} has no current access to {1} "
                  "{2}\n".format(args.to, stype, sid))
        return 0
    cur.execute("SELECT revoke_access(%s,%s,%s,%s)",
                (user, args.to, stype, sid))
    out.write("revoked: {0} can no longer see {1} {2}\n".format(
        args.to, stype, sid))
    out.write("  the grant history stays queryable -- who shared what with "
              "whom, when, and why.\n")
    return 0


def run(dsn, user, args, out=None):
    out = out or sys.stdout
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if args.command == "list":
                rc = cmd_list(cur, user, out)
            elif args.command == "grant":
                rc = cmd_grant(cur, user, args, out)
            else:
                rc = cmd_revoke(cur, user, args, out)
        conn.commit()
        return rc
    except psycopg2.errors.RaiseException as e:
        # grant_access and revoke_access raise when the rule is broken:
        # sharing a board that is not yours, or a project you do not own.
        conn.rollback()
        out.write("refused: {0}\n".format(
            str(e).strip().splitlines()[0].replace("ERROR:  ", "")))
        return 3
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(
        description="Grant and revoke Task Ledger access.")
    ap.add_argument("--as", dest="user", required=True)
    ap.add_argument("--dsn", required=True)
    sub = ap.add_subparsers(dest="command")
    sub.required = True                      # py3.8: not a constructor arg

    sub.add_parser("list", help="what you have shared, and what you hold")

    g = sub.add_parser("grant", help="share a board or a project")
    g.add_argument("--to", required=True, help="who may look")
    g.add_argument("--board", help="a user_ref; only your own")
    g.add_argument("--project", help="a project_id you own")
    g.add_argument("--level", choices=["progress", "full_trace"],
                   default="progress")
    g.add_argument("--days", type=int,
                   help="expire the grant after N days (default: until "
                        "revoked)")
    g.add_argument("--note", help="why -- kept in the audit trail")

    r = sub.add_parser("revoke", help="withdraw access")
    r.add_argument("--to", required=True)
    r.add_argument("--board")
    r.add_argument("--project")

    args = ap.parse_args()
    try:
        return run(args.dsn, args.user, args)
    except psycopg2.OperationalError as e:
        sys.stderr.write("cannot reach the database: {0}\n  {1}\n".format(
            str(e).strip(), dsn_hint()))
        return 2


if __name__ == "__main__":
    sys.exit(main())
