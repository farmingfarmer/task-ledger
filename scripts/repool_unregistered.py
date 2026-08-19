#!/usr/bin/env python3
"""Re-pool signals that were consumed without leaving any record.

    DB_DSN=... python3 scripts/repool_unregistered.py [--user aad-x] [--apply]

Dry run by default.

WHY. Until the register learned to take work from any signal type, a
status_update, assignment, completion_claim or blocker naming work on
nobody's board was held and then dropped -- the whole point of the register,
applied to one signal type and not the other five. Signals processed under
that behaviour are gone from the pool and left nothing behind, so the fix
cannot help them: it only sees what is still pooled.

This puts those signals back so they are gated again under the current
rules. Routing is pool_targets, the same function the store uses, reading
author_ref from the column rather than the raw JSON -- raw is the
extractor's original output and predates the author backfill.

WHAT IT WILL NOT TOUCH, which is most of the ledger:

  * signals still in the pool -- re-pooling those would double-pool them
  * signals that produced a candidate sighting -- they are already recorded,
    and the register writes one sighting per signal, so nothing would come
    of it anyway
  * signals that produced a proposal -- they reached a human queue, which
    is the job. Re-pooling them would raise the same proposal twice.

So it moves exactly the signals that were heard, consumed, and left no
trace anywhere. Re-gated, a completion_claim may now reach a review queue
as well as the register; that is a proposal for a person to accept or
reject, not a board change.
"""
import argparse
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for _sub in ("extraction", "reconciler"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg2                                         # noqa: E402
from signal_store_pg import pool_targets                # noqa: E402

RUN_ID = "repool-unregistered"


def main():
    ap = argparse.ArgumentParser(
        description="Re-pool signals consumed without leaving a record.")
    ap.add_argument("--user", help="only signals that would route to this "
                                   "person (default: everyone)")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset.")
        return 2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                  SELECT DISTINCT ON (signal_id, user_ref)
                         signal_id, user_ref, event
                  FROM signal_pool_events
                  ORDER BY signal_id, user_ref, event_id DESC
                )
                SELECT s.signal_id, s.signal_type, s.title, s.raw,
                       s.author_ref
                FROM signals s
                WHERE NOT EXISTS (
                        SELECT 1 FROM latest l
                        WHERE l.signal_id = s.signal_id
                          AND l.event IN ('pooled', 'returned'))
                  AND NOT EXISTS (
                        SELECT 1 FROM task_candidate_events c
                        WHERE c.signal_id = s.signal_id)
                  AND NOT EXISTS (
                        SELECT 1 FROM proposal_signals p
                        WHERE p.signal_id = s.signal_id)
                ORDER BY s.signal_id""")
            rows = cur.fetchall()

            planned, unrouted, by_type = [], 0, {}
            for signal_id, stype, title, raw, author_ref in rows:
                s = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                s.setdefault("owner_candidates", [])
                s["author_ref"] = s.get("author_ref") or author_ref
                targets, _how = pool_targets(s, [], False)
                if not targets:
                    unrouted += 1
                    continue
                for u in targets:
                    if args.user and u != args.user:
                        continue
                    planned.append((signal_id, u))
                    by_type[stype] = by_type.get(stype, 0) + 1

            print("signals that left no record anywhere: {0}".format(
                len(rows)))
            print("pool events to write: {0}{1}".format(
                len(planned),
                "" if not unrouted else
                "  ({0} unroutable, skipped)".format(unrouted)))
            for stype, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
                print("   {0:<18} {1}".format(stype, n))
            by_user = {}
            for _sid, u in planned:
                by_user[u] = by_user.get(u, 0) + 1
            for u, n in sorted(by_user.items(), key=lambda kv: -kv[1]):
                print("   {0:<32} {1}".format(u, n))

            if not args.apply:
                print("\nDRY RUN -- nothing written. Re-run with --apply.")
                return 0
            for signal_id, u in planned:
                cur.execute("""
                    INSERT INTO signal_pool_events
                      (signal_id, user_ref, event, run_id)
                    VALUES (%s, %s, 'pooled', %s)""",
                    (signal_id, u, RUN_ID))
            print("\nre-pooled {0} signal(s). The next tick gates them under "
                  "the current rules.".format(len(planned)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
