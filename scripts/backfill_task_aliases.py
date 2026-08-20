#!/usr/bin/env python3
"""Carry the register's wordings onto tasks accepted before the carry existed.

    DB_DSN=... python3 scripts/backfill_task_aliases.py [--apply]

Dry run by default.

Accepting a promoted candidate now seeds the new task with every wording
the register sighted it under, so the paraphrases that corroborated the
work match it directly instead of losing a judge call. Tasks accepted
BEFORE that carry existed began life knowing only their canonical title,
and the variants that proved them real still sit in the pool failing to
attach to them.

This finds tasks whose title matches a promoted or accepted candidate on
the same person's register and writes the missing wordings as aliases --
through apply_task_event('alias_added'), the same door every alias goes
through, each carrying its sighting's signal_id. ON CONFLICT in the board
writer makes a re-run a no-op. A task renamed at edit-accept will not
match by title and is listed rather than guessed at.
"""
import argparse
import json
import os
import sys

import psycopg2
import psycopg2.extras


def main():
    ap = argparse.ArgumentParser(
        description="Seed pre-carry tasks with their register wordings.")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset.")
        return 2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.task_id, t.user_ref, t.title,
                       e.title AS alias, max(e.signal_id) AS signal_id
                FROM tasks_current t
                JOIN v_task_candidates c
                  ON c.user_ref = t.user_ref AND c.title = t.title
                 AND c.state IN ('promoted', 'accepted')
                JOIN task_candidate_events e
                  ON e.candidate_id = c.candidate_id
                 AND e.user_ref = c.user_ref
                 AND e.event = 'sighted' AND e.title <> t.title
                WHERE NOT EXISTS (SELECT 1 FROM task_aliases a
                                  WHERE a.task_id = t.task_id
                                    AND a.alias = e.title)
                GROUP BY t.task_id, t.user_ref, t.title, e.title
                ORDER BY t.task_id, e.title""")
            rows = cur.fetchall()
            by_task = {}
            for r in rows:
                by_task.setdefault((r["task_id"], r["title"],
                                    r["user_ref"]), []).append(r)
            print("{0} alias(es) missing across {1} task(s)".format(
                len(rows), len(by_task)))
            for (tid, title, _u), rs in by_task.items():
                print("  {0}  \"{1}\"".format(tid, title[:48]))
                for r in rs:
                    print("      + {0}".format(r["alias"][:64]))
            if not args.apply:
                print("\nDRY RUN -- nothing written. Re-run with --apply.")
                return 0
            for r in rows:
                cur.execute(
                    "SELECT apply_task_event(%s,%s,'alias_added','system',"
                    "NULL,%s::jsonb)",
                    (r["task_id"], r["user_ref"], json.dumps(
                        {"alias": r["alias"],
                         "source_signal_id": r["signal_id"]})))
            print("\nwrote {0} alias(es). The next tick's matcher embeds "
                  "them, so the held paraphrases auto-link.".format(
                      len(rows)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
