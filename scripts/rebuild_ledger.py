#!/usr/bin/env python3
"""Rebuild the ledger from nothing, keeping a copy of everything.

    DB_DSN=... python3 scripts/rebuild_ledger.py            # plan only
    DB_DSN=... python3 scripts/rebuild_ledger.py --apply

Dry run by default: prints exactly what it would do and stops.

WHY. Every fix shipped since 2026-08-18 -- window size 8, the not_work
gate, closure claims not proposing creates, sticky decisions, the
promotion window -- improves what happens to NEW signals. A ledger built
before them is built on extraction that lost the project names (35
utterances per model call summarises; the specifics are what it drops)
and on accepts made against that loss. There is no repair path: the
board is append-only by design, a rejected proposal cannot retract an
earlier accept, and re-extracting into the same database is a no-op by
content hash. Starting over is not admitting defeat; it is the only way
the corpus and the code are ever the same vintage.

WHAT IT KEEPS. Everything, deliberately:
  * pg_dump of the database it is about to drop (refuses to run without
    one unless --no-backup says so in words)
  * every inbox file -- waiting, processed, quarantined, the retry
    counter -- moved into inbox/archive-<stamp>/, so the old windows
    remain readable and nothing can be re-ingested by accident

WHAT IT LOADS. storage/schema.sql and access/access_control.sql only.
The validate_*.sql files are deliberately NOT run: they seed permanent
fixture rows (users 'alex' and 'priya', signals sig-1..3) into
append-only tables, which is where this ledger's two unremovable junk
users came from last time.

Close the dashboard first. The drop uses WITH (FORCE), which severs any
connection still holding the database open.
"""
import argparse
import datetime
import glob
import os
import shutil
import subprocess
import sys

import psycopg2
from psycopg2 import sql

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))


def dsn_parts(dsn):
    return dict(kv.split("=", 1) for kv in dsn.split() if "=" in kv)


def find_pg_dump():
    pgbin = os.environ.get("PGBIN")
    if pgbin:
        for name in ("pg_dump.exe", "pg_dump"):
            cand = os.path.join(pgbin, name)
            if os.path.exists(cand):
                return cand
    return shutil.which("pg_dump")


def main():
    ap = argparse.ArgumentParser(
        description="Back everything up, then rebuild the ledger empty.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true",
                    help="proceed without a pg_dump. You are saying the "
                         "current database is worth nothing.")
    args = ap.parse_args()

    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset. Run through ./tl env, or set it first.")
        return 2
    parts = dsn_parts(dsn)
    db = parts.get("dbname", "?")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    inbox = os.path.expanduser(
        os.environ.get("TL_INBOX", "~/task-ledger-inbox"))
    moves = []
    for pat in ("*.json", ".attempts.json", "processed/*.json",
                "quarantine/*", "split-originals/*.json"):
        moves.extend(glob.glob(os.path.join(inbox, pat)))
    archive = os.path.join(inbox, "archive-" + stamp)

    dump = find_pg_dump()
    backup = os.path.join(os.path.expanduser("~"),
                          "{0}-backup-{1}.sql".format(db, stamp))

    print("plan for database '{0}' (host={1} port={2}):".format(
        db, parts.get("host", "?"), parts.get("port", "?")))
    if args.no_backup:
        print("  1. NO BACKUP (--no-backup given)")
    elif dump:
        print("  1. back up to {0}".format(backup))
    else:
        print("  1. BLOCKED: pg_dump not found (set PGBIN or put it on "
              "PATH); refusing to drop anything without a backup")
        if args.apply:
            return 2
    print("  2. archive {0} inbox file(s) -> {1}".format(
        len(moves), archive))
    print("  3. drop and recreate '{0}' (WITH FORCE -- close the "
          "dashboard first)".format(db))
    print("  4. load storage/schema.sql + access/access_control.sql")
    print("     (validate_*.sql deliberately skipped: they seed permanent "
          "fixture rows)")
    if not args.apply:
        print("\nDRY RUN -- nothing done. Re-run with --apply.")
        return 0

    if not args.no_backup:
        cmd = [dump, "-h", parts.get("host", "localhost"),
               "-p", parts.get("port", "5432"), "-d", db, "-f", backup]
        if parts.get("user"):
            cmd += ["-U", parts["user"]]
        r = subprocess.run(cmd)
        if r.returncode != 0 or not os.path.exists(backup):
            print("backup FAILED -- stopping before anything destructive.")
            return 1
        print("backed up: {0} ({1} KB)".format(
            backup, os.path.getsize(backup) // 1024))

    os.makedirs(archive, exist_ok=True)
    for path in moves:
        shutil.move(path, os.path.join(archive, os.path.basename(path)))
    print("archived {0} inbox file(s)".format(len(moves)))

    maint = " ".join("{0}={1}".format(k, v) for k, v in parts.items()
                     if k != "dbname") + " dbname=postgres"
    conn = psycopg2.connect(maint)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                    .format(sql.Identifier(db)))
        cur.execute(sql.SQL("CREATE DATABASE {}")
                    .format(sql.Identifier(db)))
    conn.close()
    print("recreated '{0}'".format(db))

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        for rel in ("storage/schema.sql", "access/access_control.sql"):
            with open(os.path.join(ROOT, rel)) as f:
                cur.execute(f.read())
            print("loaded {0}".format(rel))
        # The three facts a stale schema would silently lack.
        cur.execute("SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public'")
        n = cur.fetchone()[0]
        cur.execute("SELECT pg_get_viewdef('v_task_candidates'::regclass) "
                    "LIKE '%dismissed%accepted%'")
        sticky = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_proc WHERE proname="
                    "'forbid_mutation' AND pg_get_functiondef(oid) "
                    "LIKE '%fillable%'")
        guard = bool(cur.fetchone()[0])
    conn.close()
    print("verified: {0} tables, sticky decisions {1}, author-fill guard "
          "{2}".format(n, "yes" if sticky else "NO", "yes" if guard
                       else "NO"))
    if not (sticky and guard):
        print("SCHEMA IS STALE -- git pull and re-run before ingesting.")
        return 1
    print("\nempty and current. Next:\n"
          "  tl transcripts <your transcripts_raw.txt>\n"
          "  python cadence/cadence_tick.py\n"
          "  tl review    (accept the real projects)\n"
          "  python cadence/cadence_tick.py    (progress attaches to what "
          "you accepted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
