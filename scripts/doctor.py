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
            # Existence is not enough. The first version of this view let a
            # later sighting overwrite 'promoted', which silently produces a
            # second proposal for work already sitting in a review queue --
            # invisible from the outside, since both the view and the tick
            # look entirely healthy while doing it.
            cur.execute("""
                SELECT pg_get_viewdef('public.v_task_candidates'::regclass)
                       LIKE '%%dismissed%%accepted%%'""")
            if cur.fetchone()[0]:
                report("PASS", "migrations applied",
                       "candidate register present, decisions are sticky")
            else:
                report("WARN", "migration pending: sticky candidate state",
                       "a new sighting reopens a promoted candidate, so "
                       "mentioning promoted work again raises a duplicate "
                       "proposal; apply "
                       "storage/migrate_candidate_state_sticky.sql")
        else:
            report("WARN", "migration pending: candidate register",
                   "mentions of untracked work will be discarded rather "
                   "than accumulated; apply "
                   "storage/migrate_task_candidates.sql")
        # What the register holds, against the bar it is being held to.
        # "N candidate(s), 0 promoted" is the least informative true
        # sentence this system produces: it reads identically whether the
        # bar is too high for the data, the cards cannot be cited, or the
        # policy edit never saved. Answering it took a hand-written psql
        # query three separate times, so it belongs here.
        cur.execute("SELECT to_regclass('public.v_task_candidates')")
        if cur.fetchone()[0]:
            import yaml as _yaml
            _root = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), ".."))
            with open(os.path.join(_root, "policy",
                                   "matching_policy.yaml")) as _f:
                _cp = ((_yaml.safe_load(_f) or {}).get("candidate_promotion")
                       or {})
            need_s = _cp.get("min_sightings")
            need_p = _cp.get("min_distinct_speakers")
            win = _cp.get("window_days")
            # Counted over the window the reconciler actually uses. A
            # "standing" total that includes candidates nothing will ever
            # look at again is not a measure of the register, it is a
            # measure of how long it has been running -- and "best standing
            # candidate" was free to report an expired one, making the
            # comparison against the bar meaningless.
            cur.execute("""
                WITH live AS (
                  SELECT DISTINCT candidate_id, user_ref
                  FROM task_candidate_events
                  WHERE event = 'sighted'
                    AND (%s IS NULL
                         OR occurred_at >= now() - make_interval(days => %s))
                )
                SELECT count(*) FILTER (WHERE c.state = 'sighted'
                                          AND l.candidate_id IS NOT NULL),
                       count(*) FILTER (WHERE c.state = 'sighted'
                                          AND l.candidate_id IS NULL),
                       count(*) FILTER (WHERE c.state = 'promoted'),
                       COALESCE(max(c.sightings) FILTER
                                (WHERE c.state = 'sighted'
                                   AND l.candidate_id IS NOT NULL), 0),
                       COALESCE(max(c.speakers) FILTER
                                (WHERE c.state = 'sighted'
                                   AND l.candidate_id IS NOT NULL), 0)
                FROM v_task_candidates c
                LEFT JOIN live l ON l.candidate_id = c.candidate_id
                                AND l.user_ref = c.user_ref""",
                (win, win))
            standing, expired, promoted, top_s, top_p = cur.fetchone()
            joiner = "or" if _cp.get("mode") == "any" else "and"
            reach = (standing and (top_s >= (need_s or 0)
                                   or top_p >= (need_p or 0))
                     if joiner == "or" else
                     standing and (top_s >= (need_s or 0)
                                   and top_p >= (need_p or 0)))
            detail = ("{0} standing, {1} promoted; best standing candidate "
                      "has {2} sighting(s)/{3} speaker(s) against a bar of "
                      "{4} {5} {6} within {7} day(s)".format(
                          standing, promoted, top_s, top_p, need_s, joiner,
                          need_p, win))
            if expired:
                detail += "; {0} aged out of the window".format(expired)
            # Every candidate starts at one sighting, so "best is 1" is
            # simply a quiet register and not a misconfiguration -- warning
            # about it would fire forever on any healthy ledger that has
            # already promoted everything eligible. What is worth flagging
            # is corroboration piling up just under the bar: several things
            # said more than once that the rule keeps refusing, which is a
            # bar set above what this corpus can produce rather than a
            # corpus with nothing to say.
            accruing = top_s > 1 or top_p > 1
            if standing and not reach and accruing:
                report("WARN", "candidate register: the bar is above the "
                       "evidence", detail + " -- corroboration is "
                       "accumulating and none of it reaches either bar, so "
                       "the next tick promotes nothing however often it runs")
            else:
                report("PASS", "candidate register", detail)
        cur.execute("""
            SELECT count(*) FROM task_events
            WHERE actor_type = 'auto_applied'""")
        auto_n = cur.fetchone()[0]
        if auto_n:
            report("WARN", "{0} board change(s) applied without "
                   "approval".format(auto_n),
                   "the auto_apply gate is on; every one is revertible from "
                   "the review popup and countable by actor_type")
        # The author backfill needs the narrowed guard, and the strict one
        # refuses it mid-run with "signals is append-only" -- after the
        # operator has already decided the database was healthy. Checked by
        # reading the function body, because the only functional test is a
        # write to an append-only table.
        cur.execute("""
            SELECT count(*) FROM pg_proc
            WHERE proname = 'forbid_mutation'
              AND pg_get_functiondef(oid) LIKE '%fillable%'""")
        if cur.fetchone()[0]:
            report("PASS", "migrations applied",
                   "append-only guard permits the author fill")
        else:
            # A missing guard only matters if something needs filling. A
            # ledger extracted entirely after author_ref existed has no gap
            # and no reason to widen its own invariant -- warning it anyway
            # printed "0 signal(s) have no author ... so promotion cannot
            # use breadth", which is both untrue and self-contradicting, and
            # sent an operator hunting for a migration they did not need.
            cur.execute("""
                SELECT count(*) FROM signals WHERE author_ref IS NULL""")
            n = cur.fetchone()[0]
            if n:
                report("WARN", "migration pending: author backfill",
                       "{0} signal(s) have no author and the guard refuses "
                       "to fill them, so those candidates can only promote "
                       "on persistence; apply "
                       "storage/migrate_author_backfill.sql".format(n))
            else:
                report("PASS", "append-only guard is strict",
                       "no signal is missing a speaker, so the backfill "
                       "exemption is not needed here")
        # Breadth needs a speaker. Signals extracted before author_ref
        # existed cannot supply one, and the shortfall is invisible in the
        # candidate counts -- it looks like nothing was said twice.
        cur.execute("""
            SELECT count(*) FILTER (WHERE author_ref IS NULL), count(*)
            FROM signals""")
        noauth, total = cur.fetchone()
        # Reported, not warned about. Once the guard is in place and the
        # backfill has run, a remaining null means the window is no longer
        # on disk -- a permanent fact about history, not an outstanding
        # task. A warning that can never be cleared is how an operator
        # learns to skim past warnings.
        report("PASS", "speaker coverage",
               "{0} of {1} signal(s) have a recorded speaker"
               "{2}".format(total - noauth, total,
                            "; the rest predate author_ref and promote on "
                            "persistence alone" if noauth else ""))
        cur.execute("SELECT to_regclass('public.embedding_cache')")
        if cur.fetchone()[0]:
            cur.execute("SELECT count(*) FROM embedding_cache")
            report("PASS", "embedding cache present",
                   "{0} vector(s) memoised -- repeated texts cost no "
                   "model calls".format(cur.fetchone()[0]))
        else:
            report("WARN", "migration pending: embedding cache",
                   "every tick re-embeds every unchanged title for every "
                   "user; apply storage/migrate_embedding_cache.sql")
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
