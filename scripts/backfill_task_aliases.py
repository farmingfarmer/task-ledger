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
            # Matching is normalized (case and whitespace) and goes through
            # the candidate's SIGHTED WORDINGS, not only its canonical
            # title. The first version used exact equality on the canonical
            # title alone, and a board that said "Synth Kit Project" against
            # a register that heard "Synth kit project" matched nothing --
            # and reported "0 across 0" as if that were health. An
            # edit-accept retitle is matched the same way: if the new title
            # normalizes to ANY wording the register sighted, it is the
            # same work.
            cur.execute(r"""
                WITH cand AS (
                  SELECT c.candidate_id, c.user_ref, e.title,
                         max(e.signal_id) AS signal_id,
                         lower(regexp_replace(btrim(e.title), '\s+', ' ',
                               'g')) AS ntitle
                  FROM v_task_candidates c
                  JOIN task_candidate_events e
                    ON e.candidate_id = c.candidate_id
                   AND e.user_ref = c.user_ref AND e.event = 'sighted'
                  WHERE c.state IN ('promoted', 'accepted')
                  GROUP BY c.candidate_id, c.user_ref, e.title
                ), hit AS (
                  SELECT DISTINCT t.task_id, t.user_ref, t.title,
                         k.candidate_id
                  FROM tasks_current t
                  JOIN cand k
                    ON k.user_ref = t.user_ref
                   AND k.ntitle = lower(regexp_replace(btrim(t.title),
                                        '\s+', ' ', 'g'))
                )
                SELECT h.task_id, h.user_ref, h.title,
                       k.title AS alias, k.signal_id
                FROM hit h
                JOIN cand k ON k.candidate_id = h.candidate_id
                           AND k.user_ref = h.user_ref
                WHERE k.ntitle <> lower(regexp_replace(btrim(h.title),
                                        '\s+', ' ', 'g'))
                  AND NOT EXISTS (SELECT 1 FROM task_aliases a
                                  WHERE a.task_id = h.task_id
                                    AND a.alias = k.title)
                ORDER BY h.task_id, k.title""")
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
            # Tasks that matched nothing are the interesting failures, so
            # they are shown beside what the register could have offered --
            # a silent zero here already cost a round trip to another
            # machine.
            cur.execute(r"""
                SELECT t.task_id, t.title, t.user_ref
                FROM tasks_current t
                WHERE t.merged_into IS NULL
                  AND t.status NOT IN ('completed', 'cancelled')
                  AND NOT EXISTS (
                    SELECT 1 FROM v_task_candidates c
                    JOIN task_candidate_events e
                      ON e.candidate_id = c.candidate_id
                     AND e.user_ref = c.user_ref AND e.event = 'sighted'
                    WHERE c.user_ref = t.user_ref
                      AND c.state IN ('promoted', 'accepted')
                      AND lower(regexp_replace(btrim(e.title), '\s+', ' ',
                                'g')) =
                          lower(regexp_replace(btrim(t.title), '\s+', ' ',
                                'g')))
                ORDER BY t.user_ref, t.title""")
            misses = cur.fetchall()
            if misses:
                print("\n{0} task(s) match no promoted candidate under any "
                      "wording (retitled past recognition, or not born "
                      "from the register):".format(len(misses)))
                for m in misses:
                    print("  {0}  \"{1}\"  [{2}]".format(
                        m["task_id"], m["title"][:48], m["user_ref"]))
                    cur.execute(r"""
                        SELECT DISTINCT c.title
                        FROM v_task_candidates c
                        WHERE c.user_ref = %s
                          AND c.state IN ('promoted', 'accepted')
                        ORDER BY c.title LIMIT 6""", (m["user_ref"],))
                    near = [r["title"] for r in cur.fetchall()]
                    if near:
                        print("      register offered: {0}".format(
                            "; ".join(t[:40] for t in near)))
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
