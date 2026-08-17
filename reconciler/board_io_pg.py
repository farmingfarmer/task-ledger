"""Postgres adapter for the reconciler.

load_signals(dsn, user_ref)      v_unconsumed_signals rows -> reconciler shape
load_board(dsn, user_ref)        matchable tasks (Q9 alias sets + blockers,
                                 deadline, age) + current board_version
load_all_boards(dsn)             every board's tasks (titles + ownership):
                                 blocker-edge resolution and cross-board
                                 duplicate annotation
write_packet(dsn, packet, ...)   review_packets + proposals + proposal_signals
                                 + packet_state_events('draft'), marks
                                 consumed signals 'taken', applies alias
                                 annotations through apply_task_event
                                 -- all in one transaction.

Held signals are untouched: they simply stay pooled, which is the whole point
of holding. Python 3.8 compatible. Requires psycopg2.
"""

import datetime
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
                       s.owner_candidates, s.raw, s.author_ref,
                       -- How long this has been waiting for us. A signal
                       -- held for a task that does not exist yet must wait,
                       -- but not forever; the reconciler expires it against
                       -- corroboration.window_days rather than growing the
                       -- pool without bound.
                       (SELECT min(pe.occurred_at) FROM signal_pool_events pe
                         WHERE pe.signal_id = s.signal_id
                           AND pe.user_ref = s.user_ref) AS pooled_at
                FROM v_unconsumed_signals s
                WHERE s.user_ref = %s""", (user_ref,))
            rows = [dict(r) for r in cur.fetchall()]
            now = datetime.datetime.now(datetime.timezone.utc)
            for r in rows:
                raw = r.pop("raw") or {}
                r["evidence"] = raw.get("evidence") or []
                pooled = r.pop("pooled_at", None)
                r["pooled_days"] = (
                    None if pooled is None
                    else max(0.0, (now - pooled).total_seconds() / 86400.0))
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


def load_all_boards(dsn):
    """Every board's matchable tasks, for the reconciler's all_tasks: it
    resolves blocker edges to other people's tasks (cross-user edges are
    first-class -- see Q4) and annotates unowned creates that duplicate
    work someone else already carries.

    Titles and ownership only. Same freshness window as load_board, so a
    task that closed last quarter neither annotates nor attracts an edge.
    If this ever runs multi-tenant, scope it through visible_tasks(viewer)
    -- the reconciler consumes whatever list it is handed."""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT t.task_id, t.title, t.user_ref, t.owner_ref, t.status
                FROM tasks_current t
                WHERE t.merged_into IS NULL
                  AND (t.status NOT IN ('completed','cancelled')
                       OR t.updated_at > now() - interval '14 days')""")
            return [dict(r) for r in cur.fetchall()]
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


def load_candidates(dsn, user_ref):
    """Work this person has heard about that is on no board yet.

    Only 'sighted' rows: once a candidate has been promoted into a packet,
    or dismissed by someone saying it is not a task, it must stop coming
    back every time the word is said again.
    """
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Carry the most recent sighting's signal with each candidate.
            # Promotion needs evidence to cite, and without this a candidate
            # could only ever be assessed on a run where it happened to be
            # mentioned again -- so a relaxed promotion rule did nothing at
            # all until somebody said the words one more time.
            cur.execute("""
                SELECT c.candidate_id, c.title, c.state, c.sightings,
                       c.speakers,
                       (SELECT e.signal_id FROM task_candidate_events e
                         WHERE e.candidate_id = c.candidate_id
                           AND e.user_ref = c.user_ref
                           AND e.event = 'sighted'
                           AND e.signal_id IS NOT NULL
                         ORDER BY e.event_id DESC LIMIT 1) AS lead_signal_id
                FROM v_task_candidates c
                WHERE c.user_ref = %s AND c.state = 'sighted'""", (user_ref,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                sid = r.pop("lead_signal_id", None)
                r["lead"] = None
                if not sid:
                    continue
                cur.execute("""
                    SELECT s.signal_id, s.description,
                           s.confidence::float AS confidence,
                           s.owner_candidates, s.raw
                    FROM signals s WHERE s.signal_id = %s""", (sid,))
                srow = cur.fetchone()
                if not srow:
                    continue
                srow = dict(srow)
                raw = srow.pop("raw") or {}
                oc = srow.get("owner_candidates")
                r["lead"] = {
                    "signal_id": srow["signal_id"],
                    "description": srow["description"],
                    "confidence": srow["confidence"],
                    "owner_candidates": (json.loads(oc)
                                         if isinstance(oc, str) else oc) or [],
                    "evidence": raw.get("evidence") or [],
                }
            return rows
    except psycopg2.errors.UndefinedTable:
        # A database that predates migrate_task_candidates.sql still runs;
        # it simply has no register yet.
        return []
    finally:
        conn.close()


def write_candidate_events(cur, user_ref, report, run_id):
    """Sightings and promotions from one reconcile run.

    Append-only, like the signal pool: 'we heard this again' and 'this went
    to a person to rule on' are both facts worth keeping, and the projection
    reads the latest to decide whether to offer it again.
    """
    n = 0
    for s in report.get("candidate_sightings") or []:
        cur.execute("""
            INSERT INTO task_candidate_events
              (candidate_id, user_ref, event, title, signal_id,
               speaker_ref, run_id)
            VALUES (%s,%s,'sighted',%s,%s,%s,%s)""",
            (s["candidate_id"], user_ref, s["title"], s["signal_id"],
             s.get("speaker_ref"), run_id))
        n += 1
    for p in report.get("candidate_promotions") or []:
        cur.execute("""
            INSERT INTO task_candidate_events
              (candidate_id, user_ref, event, title, run_id)
            VALUES (%s,%s,'promoted',%s,%s)""",
            (p["candidate_id"], user_ref, p["title"], run_id))
        n += 1
    return n


def apply_auto(cur, user_ref, report):
    """Apply the proposals that skipped the queue, labelled as such.

    actor_type='auto_applied' and decision_id NULL: the schema's CHECK
    still forbids an AI-originated change claiming to be human-typed, and
    this adds no exemption to it. What it adds is a value that can be
    counted, so "how much of this board did nobody confirm?" has an answer
    and a revocation is a WHERE clause rather than an archaeology project.
    """
    n = 0
    for a in report.get("auto_applied") or []:
        payload = dict(a["payload"] or {})
        cur.execute(
            "SELECT apply_task_event(%s,%s,'status_changed','auto_applied',"
            "NULL,%s::jsonb)",
            (a["target_task_id"], user_ref, json.dumps({
                "from_status": payload.get("from_status"),
                "to_status": payload.get("to_status"),
                "auto_applied": True,
                "source_signals": a.get("source_signals") or [],
                "rationale": a.get("rationale"),
            })))
        n += 1
    return n


def apply_aliases(cur, user_ref, annotations):
    """Names people used for work already on the board.

    Applied on sight rather than on acceptance: matching should improve with
    every meeting, not only with every review session.
    """
    n = 0
    for ann in annotations or []:
        if ann.get("kind") != "alias":
            continue
        cur.execute(
            "SELECT apply_task_event(%s, %s, 'alias_added', "
            "'system', NULL, %s::jsonb)",
            (ann["task_id"], user_ref,
             json.dumps({"alias": ann["alias"],
                         "source_signal_id": ann["source_signal_id"]})))
        n += 1
    return n


def write_packet(dsn, packet, consumed_signal_ids, annotations,
                 run_id, expires_hours=48, report=None):
    """Persists the packet and its side effects atomically. Supersedes any
    open packets for the user first (their undecided signals rejoin the
    pool). Packets with zero proposals are NOT inserted -- silence is the
    output -- but the supersede still runs, matching the orchestrator's
    tick order. Returns counts."""
    conn = psycopg2.connect(dsn)
    out = {"proposals": 0, "taken": 0, "aliases": 0,
           "superseded": [], "returned_signals": 0, "candidate_events": 0,
           "auto_applied": 0}
    try:
        with conn:
            with conn.cursor() as cur:
                stale, returned = supersede_open_packets(
                    cur, packet["user_ref"], run_id,
                    exclude_packet_id=packet["packet_id"])
                out["superseded"] = stale
                out["returned_signals"] = returned
                if report is not None:
                    out["candidate_events"] = write_candidate_events(
                        cur, packet["user_ref"], report, run_id)
                # Aliases land here, ahead of every early return below.
                # They used to be written after the packet insert, so a run
                # that proposed nothing -- the common case, since most
                # signals are mentions -- taught the matcher nothing at all,
                # and an idempotent replay skipped them too. An alias is not
                # a board fact and needs no decision: it is a name somebody
                # used for work that already exists, and knowing it makes
                # the NEXT meeting match better.
                out["aliases"] += apply_aliases(
                    cur, packet["user_ref"], annotations)
                # Auto-applied changes land here too, ahead of the empty
                # packet return: a run whose only output applied itself
                # still has to actually apply it.
                if report is not None:
                    out["auto_applied"] = apply_auto(
                        cur, packet["user_ref"], report)
                    for a in report.get("auto_applied") or []:
                        for sid in a.get("source_signals") or []:
                            cur.execute("""
                                INSERT INTO signal_pool_events
                                  (signal_id, user_ref, event, run_id)
                                VALUES (%s,%s,'taken',%s)""",
                                (sid, packet["user_ref"], run_id))
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
        return out
    finally:
        conn.close()
