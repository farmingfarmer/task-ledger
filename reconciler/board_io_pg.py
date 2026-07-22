"""Postgres adapter for the reconciler.

load_signals(dsn, user_ref)      v_unconsumed_signals rows -> reconciler shape
load_board(dsn, user_ref)        matchable tasks (Q9 alias sets + blockers,
                                 deadline, age) + current board_version
write_packet(dsn, packet, ...)   review_packets + proposals + proposal_signals
                                 + packet_state_events('draft'), marks
                                 consumed signals 'taken', applies alias
                                 annotations through apply_task_event
                                 -- all in one transaction.

Held signals are untouched: they simply stay pooled, which is the whole point
of holding. Python 3.8 compatible. Requires psycopg2.
"""

import json

import psycopg2
import psycopg2.extras


def load_signals(dsn, user_ref):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT s.signal_id, s.signal_type, s.title, s.description,
                       s.status_signal, s.confidence::float AS confidence,
                       s.deadline_date, s.deadline_precision, s.deadline_raw,
                       s.blocker_desc, s.blocker_party,
                       s.owner_candidates, s.raw
                FROM v_unconsumed_signals s
                WHERE s.user_ref = %s""", (user_ref,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                raw = r.pop("raw") or {}
                r["evidence"] = raw.get("evidence") or []
                if r["deadline_date"] is not None:
                    r["deadline_date"] = r["deadline_date"].isoformat()
                if isinstance(r["owner_candidates"], str):
                    r["owner_candidates"] = json.loads(r["owner_candidates"])
            return rows
    finally:
        conn.close()


def load_board(dsn, user_ref):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.task_id, t.title, t.status, t.owner_ref,
                       t.deadline, t.created_at,
                       COALESCE(array_agg(a.alias)
                         FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases
                FROM tasks_current t
                LEFT JOIN task_aliases a USING (task_id)
                WHERE t.user_ref = %s AND t.merged_into IS NULL
                  AND (t.status NOT IN ('completed','cancelled')
                       OR t.updated_at > now() - interval '14 days')
                GROUP BY t.task_id""", (user_ref,))
            tasks = []
            for r in cur.fetchall():
                r = dict(r)
                cur.execute("""
                    SELECT blocker_id, description FROM task_blockers
                    WHERE task_id = %s AND resolved_event_id IS NULL""",
                    (r["task_id"],))
                blockers = [dict(b) for b in cur.fetchall()]
                cur.execute("""
                    SELECT EXTRACT(epoch FROM now() - %s)/86400.0 AS age
                    """, (r["created_at"],))
                age = float(cur.fetchone()["age"])
                tasks.append({
                    "task_id": r["task_id"], "title": r["title"],
                    "status": r["status"], "owner_ref": r["owner_ref"],
                    "deadline": (r["deadline"].isoformat()
                                 if r["deadline"] else None),
                    "aliases": list(r["aliases"]),
                    "age_days": age,
                    "open_blockers": blockers,
                })
            cur.execute("SELECT version FROM user_boards WHERE user_ref = %s",
                        (user_ref,))
            row = cur.fetchone()
            version = row["version"] if row else 0
            return tasks, version
    finally:
        conn.close()


def supersede_open_packets(cur, user_ref, run_id, exclude_packet_id=None):
    """Mark every open packet for the user superseded and return the signals
    of their undecided proposals to the pool -- the orchestrator's supersede
    semantics, enforced at the storage layer so every writer inherits them.
    Returns (superseded_packet_ids, returned_signal_count)."""
    cur.execute("""
        WITH latest AS (
          SELECT DISTINCT ON (packet_id) packet_id, state
          FROM packet_state_events ORDER BY packet_id, event_id DESC)
        SELECT rp.packet_id FROM review_packets rp
        JOIN latest l USING (packet_id)
        WHERE rp.user_ref = %s
          AND l.state IN ('draft','delivered','partially_decided')
          AND rp.packet_id <> COALESCE(%s, '')""",
        (user_ref, exclude_packet_id))
    stale = [r[0] for r in cur.fetchall()]
    returned = 0
    for pid in stale:
        cur.execute("""
            INSERT INTO signal_pool_events (signal_id, user_ref, event,
                                            run_id)
            SELECT DISTINCT ps.signal_id, %s, 'returned', %s
            FROM proposals p
            JOIN proposal_signals ps USING (proposal_id)
            LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
            WHERE p.packet_id = %s AND d.decision_id IS NULL""",
            (user_ref, run_id, pid))
        returned += cur.rowcount
        cur.execute("INSERT INTO packet_state_events (packet_id, state) "
                    "VALUES (%s, 'superseded')", (pid,))
    return stale, returned


def write_packet(dsn, packet, consumed_signal_ids, annotations,
                 run_id, expires_hours=48):
    """Persists the packet and its side effects atomically. Supersedes any
    open packets for the user first (their undecided signals rejoin the
    pool). Packets with zero proposals are NOT inserted -- silence is the
    output -- but the supersede still runs, matching the orchestrator's
    tick order. Returns counts."""
    conn = psycopg2.connect(dsn)
    out = {"proposals": 0, "taken": 0, "aliases": 0,
           "superseded": [], "returned_signals": 0}
    try:
        with conn:
            with conn.cursor() as cur:
                stale, returned = supersede_open_packets(
                    cur, packet["user_ref"], run_id,
                    exclude_packet_id=packet["packet_id"])
                out["superseded"] = stale
                out["returned_signals"] = returned
                if not packet["proposals"]:
                    for sid in consumed_signal_ids:
                        cur.execute("""
                            INSERT INTO signal_pool_events
                              (signal_id, user_ref, event, run_id)
                            VALUES (%s,%s,'taken',%s)""",
                            (sid, packet["user_ref"], run_id))
                        out["taken"] += 1
                    return out
                cur.execute("""
                    INSERT INTO review_packets
                      (packet_id, user_ref, cadence_run_id, board_version,
                       generated_at, expires_at)
                    VALUES (%s,%s,%s,%s,%s,
                            now() + (%s || ' hours')::interval)
                    ON CONFLICT (packet_id) DO NOTHING""",
                    (packet["packet_id"], packet["user_ref"],
                     packet["cadence_run_id"], packet["board_version"],
                     packet["generated_at"], str(expires_hours)))
                if cur.rowcount == 0:
                    return out   # idempotent replay
                cur.execute("""
                    INSERT INTO packet_state_events (packet_id, state)
                    VALUES (%s, 'draft')""", (packet["packet_id"],))
                for p in packet["proposals"]:
                    c = p.get("conflict")
                    cur.execute("""
                        INSERT INTO proposals
                          (proposal_id, packet_id, op, target_task_id,
                           rationale, confidence, conflict_type,
                           conflict_detail, payload)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (proposal_id) DO NOTHING""",
                        (p["proposal_id"], packet["packet_id"], p["op"],
                         p["target_task_id"], p["rationale"],
                         p["confidence"],
                         c["type"] if c else None,
                         c["detail"] if c else None,
                         json.dumps(p["payload"])))
                    out["proposals"] += cur.rowcount
                    for sid in p["source_signals"]:
                        cur.execute("""
                            INSERT INTO proposal_signals
                              (proposal_id, signal_id)
                            VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                            (p["proposal_id"], sid))
                for sid in consumed_signal_ids:
                    cur.execute("""
                        INSERT INTO signal_pool_events
                          (signal_id, user_ref, event, run_id)
                        VALUES (%s,%s,'taken',%s)""",
                        (sid, packet["user_ref"], run_id))
                    out["taken"] += 1
                for ann in annotations:
                    if ann.get("kind") != "alias":
                        continue
                    cur.execute(
                        "SELECT apply_task_event(%s, %s, 'alias_added', "
                        "'system', NULL, %s::jsonb)",
                        (ann["task_id"], packet["user_ref"],
                         json.dumps({"alias": ann["alias"],
                                     "source_signal_id":
                                         ann["source_signal_id"]})))
                    out["aliases"] += 1
        return out
    finally:
        conn.close()
