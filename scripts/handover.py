#!/usr/bin/env python3
"""Everything one person carries, with the conversational history of each.

    DB_DSN=... python3 scripts/handover.py aad-hari-krishna-rekapalli
    DB_DSN=... python3 scripts/handover.py aad-hari --out hari-handover.txt

Read-only. Organizations bleed knowledge at transitions -- someone leaves,
joins, or goes on leave, and the handover document everyone wishes existed
gets written from memory, badly, if at all. This one is generated from the
ledger: every open task with its state, deadline, blockers, the wordings
it goes by, when it was first raised and in which meeting, and how it has
moved since. Plus the untracked mentions still standing in their register,
because "things people said to them that nobody wrote down" is exactly
what a successor cannot recover any other way.
"""
import argparse
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for _s in ("reconciler", "scripts"):
    _p = os.path.join(ROOT, _s)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg2                                         # noqa: E402
import psycopg2.extras                                  # noqa: E402
import serieslib                                        # noqa: E402


def connect(dsn):
    try:
        return psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        print("database unreachable ({0}) -- start it and re-run."
              .format(exc.__class__.__name__))
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(
        description="A handover document for one person, from receipts. "
                    "Read-only.")
    ap.add_argument("person", help="ref or unambiguous fragment, "
                                   "e.g. aad-hari or hari")
    ap.add_argument("--out", help="also write the document to this file")
    args = ap.parse_args()
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset.")
        return 2

    conn = connect(dsn)
    lines = []
    say = lines.append
    try:
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT owner_ref AS r FROM tasks_current
                WHERE owner_ref IS NOT NULL
                UNION SELECT DISTINCT user_ref FROM user_boards""")
            refs = sorted({r["r"] for r in cur.fetchall()})
            hits = [r for r in refs if args.person.lower() in r.lower()]
            if not hits:
                print("Nobody matches “{0}”. Known: {1}".format(
                    args.person, ", ".join(refs)))
                return 1
            if len(hits) > 1:
                print("“{0}” matches: {1}".format(
                    args.person, ", ".join(hits)))
                return 1
            who = hits[0]

            say("=" * 64)
            say("HANDOVER · {0}".format(who.replace("aad-", "").upper()))
            say("generated from the ledger; every item traces to a "
                "sentence")
            say("=" * 64)

            cur.execute("""
                SELECT task_id, title, status, deadline, created_at
                FROM tasks_current
                WHERE owner_ref = %s AND merged_into IS NULL
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY status, deadline NULLS LAST, title""", (who,))
            open_tasks = cur.fetchall()
            say("")
            say("CARRYING ({0} open)".format(len(open_tasks)))
            for t in open_tasks:
                say("")
                say("  {0}  [{1}]{2}".format(
                    t["title"], t["status"],
                    "  due " + str(t["deadline"]) if t["deadline"] else ""))
                cur.execute("""SELECT alias FROM task_aliases
                               WHERE task_id = %s LIMIT 4""",
                            (t["task_id"],))
                al = [r["alias"] for r in cur.fetchall()]
                if al:
                    say("      also called: {0}".format("; ".join(al)))
                # Where it came from: the earliest cited sentence.
                cur.execute("""
                    SELECT b.source_ref, b.window_start, s.title AS said
                    FROM proposal_signals ps
                    JOIN decisions d ON d.proposal_id = ps.proposal_id
                    JOIN task_events e ON e.decision_id = d.decision_id
                                      AND e.task_id = %s
                    JOIN signals s USING (signal_id)
                    JOIN extraction_batches b ON b.batch_id = s.batch_id
                    ORDER BY b.window_start LIMIT 1""", (t["task_id"],))
                src = cur.fetchone()
                if src:
                    say("      first raised: {0}, {1}".format(
                        serieslib.pretty(src["source_ref"]),
                        str(src["window_start"])[:10]))
                cur.execute("""
                    SELECT event_type, occurred_at FROM task_events
                    WHERE task_id = %s ORDER BY event_id DESC LIMIT 1""",
                    (t["task_id"],))
                ev = cur.fetchone()
                if ev:
                    say("      last movement: {0}, {1}".format(
                        ev["event_type"], str(ev["occurred_at"])[:10]))
                cur.execute("""
                    SELECT description, blocking_party FROM task_blockers
                    WHERE task_id = %s AND resolved_event_id IS NULL""",
                    (t["task_id"],))
                for b in cur.fetchall():
                    say("      BLOCKED on {0}".format(
                        (b["blocking_party"] or b["description"]
                         or "?").replace("aad-", "")))

            try:
                cur.execute("""
                    SELECT title, sightings, last_seen
                    FROM v_task_candidates
                    WHERE user_ref = %s AND state = 'sighted'
                    ORDER BY sightings DESC, last_seen DESC""", (who,))
                cands = cur.fetchall()
            except psycopg2.errors.UndefinedTable:
                conn.rollback()
                cands = []
            if cands:
                say("")
                say("HEARD ABOUT, NEVER TRACKED ({0}) -- a successor "
                    "cannot recover these any other way".format(len(cands)))
                for c in cands:
                    say("  {0:<50} {1} sighting(s), last {2}".format(
                        c["title"][:50], c["sightings"],
                        str(c["last_seen"])[:10]))

            cur.execute("""
                SELECT title, updated_at FROM tasks_current
                WHERE owner_ref = %s AND status = 'completed'
                ORDER BY updated_at DESC LIMIT 8""", (who,))
            done = cur.fetchall()
            if done:
                say("")
                say("RECENTLY FINISHED")
                for t in done:
                    say("  {0:<50} {1}".format(
                        t["title"][:50], str(t["updated_at"])[:10]))
            say("")
            say("-" * 64)
            say("tl recall \"<words>\" quotes any of it back with the "
                "receipt.")
    finally:
        conn.close()
    text = "\n".join(lines)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("\nwritten to {0}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
