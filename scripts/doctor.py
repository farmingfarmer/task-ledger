#!/usr/bin/env python3
"""Task Ledger preflight: is this machine ready?

Run after any fresh setup (macOS or Windows):

    python3 scripts/doctor.py            # uses DB_DSN if set

Checks environment, dependencies, database, schema, and pipeline state.
Ollama is probed but its absence is a WARN, not a FAIL -- Phase A (storage,
reconcile-offline, review, dashboard) needs no model. Exit code 0 unless a
FAIL occurred. Python 3.8+, stdlib + project deps only.
"""

import glob
import importlib
import json
import os
import sys
import urllib.request

RESULTS = {"PASS": 0, "WARN": 0, "FAIL": 0}


def report(level, label, detail=""):
    RESULTS[level] += 1
    pad = {"PASS": "  ok  ", "WARN": " warn ", "FAIL": " FAIL "}[level]
    print("[{0}] {1}{2}".format(pad, label,
                                (" -- " + detail) if detail else ""))


def check_python():
    v = sys.version_info
    if v >= (3, 8):
        report("PASS", "python {0}.{1}.{2}".format(v.major, v.minor,
                                                   v.micro))
    else:
        report("FAIL", "python too old",
               "need 3.8+, found {0}.{1}".format(v.major, v.minor))


def check_deps():
    for mod in ("psycopg2", "jsonschema", "yaml", "flask"):
        try:
            importlib.import_module(mod)
            report("PASS", "dependency: " + mod)
        except ImportError:
            report("FAIL", "dependency missing: " + mod,
                   "pip install --user -r requirements.txt")


def check_db():
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        report("WARN", "DB_DSN not set",
               "export it (mac: port 5432 user $(whoami); "
               "windows: port 5433 user %USERNAME%)")
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(dsn)
    except Exception as e:
        report("FAIL", "database unreachable", str(e).strip()[:90])
        return None
    report("PASS", "database reachable")
    return conn


def check_schema(conn):
    need = ("tasks_current", "signals", "signal_pool_events",
            "review_packets", "proposals", "decisions", "task_events",
            "access_grant_events")
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables "
                    "WHERE schemaname='public'")
        have = {r[0] for r in cur.fetchall()}
        missing = [t for t in need if t not in have]
        if missing:
            report("FAIL", "schema incomplete",
                   "missing " + ", ".join(missing) +
                   " (load storage/schema.sql + access/access_control.sql)")
            return
        report("PASS", "schema present ({0} core tables)".format(len(need)))
        # Pending migrations fail at write time with an UndefinedColumn deep
        # in the store, which is a poor way to learn about them.
        # Both the column AND the view: v_unconsumed_signals is `SELECT s.*`,
        # whose star was expanded when the view was created, so a database
        # can have the column and still be missing it downstream.
        cur.execute("""
            SELECT count(*) FILTER (WHERE table_name = 'signals')     AS col,
                   count(*) FILTER (WHERE table_name =
                                    'v_unconsumed_signals')           AS view
            FROM information_schema.columns
            WHERE column_name = 'author_ref'""")
        col, view = cur.fetchone()
        if not col:
            report("WARN", "migration pending: signals.author_ref",
                   "extraction writes will fail; apply "
                   "storage/migrate_author_ref.sql")
        elif not view:
            report("WARN", "stale view: v_unconsumed_signals",
                   "column present but the view predates it; re-apply "
                   "storage/migrate_author_ref.sql")
        else:
            report("PASS", "migrations applied",
                   "signals.author_ref present in table and view")
        # A database predating meeting_recap rejects the batch at write
        # time with a CHECK violation deep in the store, which is a poor
        # way to learn a migration is outstanding.
        cur.execute("""
            SELECT count(*) FROM pg_constraint
            WHERE conname = 'extraction_batches_source_type_check'
              AND pg_get_constraintdef(oid) LIKE '%meeting_recap%'""")
        if cur.fetchone()[0]:
            report("PASS", "migrations applied", "meeting_recap accepted")
        else:
            report("WARN", "migration pending: meeting_recap source type",
                   "recap ingestion will fail on write; apply "
                   "storage/migrate_meeting_recap.sql")
        # Without the register, every mention that matches no task is
        # discarded again -- silently, since a dropped signal is not an
        # error. That is the failure mode most likely to look like "the
        # model found nothing" when the model found plenty.
        cur.execute("SELECT to_regclass('public.v_task_candidates')")
        if cur.fetchone()[0]:
            report("PASS", "migrations applied", "candidate register present")
        else:
            report("WARN", "migration pending: candidate register",
                   "mentions of untracked work will be discarded rather "
                   "than accumulated; apply "
                   "storage/migrate_task_candidates.sql")
        cur.execute("SELECT to_regclass('public.v_task_progress')")
        if not cur.fetchone()[0]:
            report("WARN", "migration pending: derived progress",
                   "subtask rollup unavailable; apply "
                   "storage/migrate_task_progress.sql")
        cur.execute("SELECT count(*) FROM v_unconsumed_signals")
        pool = cur.fetchone()[0]
        cur.execute("""
            WITH latest AS (SELECT DISTINCT ON (packet_id) packet_id, state
              FROM packet_state_events ORDER BY packet_id, event_id DESC)
            SELECT count(*) FROM latest
            WHERE state IN ('draft','delivered','partially_decided')""")
        open_packets = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM tasks_current")
        tasks = cur.fetchone()[0]
        report("PASS", "pipeline state",
               "{0} pooled signals, {1} open packet(s), {2} tasks".format(
                   pool, open_packets, tasks))
        cur.execute("""
            SELECT user_ref, signal_type, count(*)
            FROM v_unconsumed_signals GROUP BY 1, 2 ORDER BY 1, 2""")
        rows = cur.fetchall()
        if rows:
            detail = "; ".join("{0}:{1} x{2}".format(u, t, c)
                               for u, t, c in rows[:8])
            report("PASS", "pool detail", detail)
        cur.execute("SHOW server_version")
        report("PASS", "postgres " + cur.fetchone()[0])
        if tasks == 0 and pool == 0:
            report("WARN", "board is empty",
                   "python3 scripts/demo_offline.py seeds a walkthrough")


def check_ollama():
    host = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=3) as r:
            models = [m["name"] for m in
                      json.loads(r.read().decode()).get("models", [])]
        report("PASS", "ollama reachable",
               ", ".join(models[:4]) if models else "no models pulled")
    except Exception:
        report("WARN", "ollama not reachable at " + host,
               "fine for Phase A; extraction/judging need it (or "
               "--ollama-host to a reachable one)")


def check_files():
    root = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))
    for rel in ("storage/schema.sql", "policy/matching_policy.yaml",
                ("contracts/extraction_schema.json",
                 "extraction_schema.json"),
                "integration/dashboard/dashboard.html"):
        cands = rel if isinstance(rel, tuple) else (rel,)
        hit = next((c for c in cands
                    if os.path.exists(os.path.join(root, c))), None)
        if hit:
            report("PASS", "file: " + hit)
        else:
            report("FAIL", "file missing: " + cands[0],
                   "incomplete checkout?")


def check_inbox():
    """Waiting windows and quarantined ones.

    A file sitting in the inbox means Ollama was down, which is normal. A
    file in quarantine/ means a window failed enough times that the tick
    gave up on it -- and since a failing window simply stayed in the inbox
    before, that state looked exactly like the normal one. It is reported
    here because nothing else would ever mention it.
    """
    inbox = os.path.expanduser(
        os.environ.get("TL_INBOX", "~/task-ledger-inbox"))
    if not os.path.isdir(inbox):
        report("WARN", "inbox not found", inbox)
        return
    waiting = glob.glob(os.path.join(inbox, "*.json"))
    quarantined = glob.glob(os.path.join(inbox, "quarantine", "*.json"))
    report("PASS", "inbox", "{0} window(s) waiting, {1} processed".format(
        len(waiting),
        len(glob.glob(os.path.join(inbox, "processed", "*.json")))))
    if quarantined:
        names = ", ".join(sorted(os.path.basename(q) for q in quarantined)[:3])
        report("WARN", "{0} window(s) quarantined".format(len(quarantined)),
               "{0} -- never extracted; see the .error.txt beside each, and "
               "move the .json back to retry".format(names))


def main():
    print("task-ledger doctor\n" + "-" * 40)
    check_python()
    check_deps()
    check_files()
    check_inbox()
    conn = check_db()
    if conn is not None:
        check_schema(conn)
        conn.close()
    check_ollama()
    print("-" * 40)
    print("{PASS} pass, {WARN} warn, {FAIL} fail".format(**RESULTS))
    sys.exit(1 if RESULTS["FAIL"] else 0)


if __name__ == "__main__":
    main()
