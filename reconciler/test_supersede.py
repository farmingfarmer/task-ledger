"""Supersede-on-manual-write: live-DB test with its own fixture user.

Asserts: writing packet B supersedes open packet A and returns A's undecided
signals; writing an EMPTY packet supersedes but inserts nothing; replaying a
write neither self-supersedes nor duplicates.

Run: python3 test_supersede.py
"""

import json
import os
import sys
import uuid

sys.path.insert(0, ".")
import board_io_pg
import psycopg2

CHECKS = [0]


def ok(cond, label):
    assert cond, label
    CHECKS[0] += 1
    print("  pass: {0}".format(label))


def mk_packet(user, pid, props):
    return {"packet_id": pid, "user_ref": user, "cadence_run_id": "sup-run",
            "board_version": 1,
            "generated_at": "2026-07-22T08:00:00+00:00",
            "expires_at": None, "proposals": props}


def mk_prop(pid, sid):
    return {"proposal_id": pid, "op": "create_task", "target_task_id": None,
            "rationale": "supersede fixture", "confidence": 0.9,
            "source_signals": [sid], "evidence": [], "conflict": None,
            "payload": {"kind": "create_task", "title": "fixture",
                        "status": "proposed"}}


def main():
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("skipped: DB_DSN not set")
        return
    try:
        psycopg2.connect(dsn).close()
    except Exception as e:
        print("skipped: database not reachable ({0})".format(
            e.__class__.__name__))
        return

    user = "sup-test-" + uuid.uuid4().hex[:8]
    b = "batch-" + user
    s1, s2 = "sig1-" + user, "sig2-" + user
    conn = psycopg2.connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO extraction_batches VALUES
                (%s,'2026-07-22T07:00Z','fake:test','extract-v1.0',
                 'teams_chat','chat-x','2026-07-22T06:00Z',
                 '2026-07-22T07:00Z')""", (b,))
            for s in (s1, s2):
                cur.execute("""INSERT INTO signals (signal_id,batch_id,
                    signal_type,title,confidence,owner_candidates,raw)
                    VALUES (%s,%s,'proposal','fixture work',0.9,'[]','{}')""",
                    (s, b))
    conn.close()

    p1 = mk_packet(user, "pktA-" + user, [mk_prop("propA-" + user, s1)])
    r1 = board_io_pg.write_packet(dsn, p1, [s1], [], "runA")
    ok(r1["proposals"] == 1 and r1["superseded"] == [],
       "first packet writes clean, nothing to supersede")

    p2 = mk_packet(user, "pktB-" + user, [mk_prop("propB-" + user, s2)])
    r2 = board_io_pg.write_packet(dsn, p2, [s2], [], "runB")
    ok(r2["superseded"] == [p1["packet_id"]] and r2["returned_signals"] == 1,
       "second write supersedes the open packet and returns its signal")

    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("""SELECT state FROM packet_state_events
            WHERE packet_id=%s ORDER BY event_id DESC LIMIT 1""",
            (p1["packet_id"],))
        ok(cur.fetchone()[0] == "superseded",
           "superseded state recorded on packet A")
        cur.execute("""SELECT count(*) FROM v_unconsumed_signals
            WHERE user_ref=%s AND signal_id=%s""", (user, s1))
        ok(cur.fetchone()[0] == 1, "packet A's signal is back in the pool")
    conn.close()

    p3 = mk_packet(user, "pktC-" + user, [])
    r3 = board_io_pg.write_packet(dsn, p3, [s1], [], "runC")
    ok(r3["superseded"] == [p2["packet_id"]] and r3["proposals"] == 0,
       "empty packet supersedes but inserts nothing")
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review_packets WHERE packet_id=%s",
                    (p3["packet_id"],))
        ok(cur.fetchone()[0] == 0, "no row for the empty packet")
    conn.close()

    r4 = board_io_pg.write_packet(dsn, p2, [s2], [], "runB")
    ok(r4["proposals"] == 0 and p2["packet_id"] not in r4["superseded"],
       "replay: idempotent, never self-supersedes")

    print("\nall {0} supersede checks passed".format(CHECKS[0]))


if __name__ == "__main__":
    main()