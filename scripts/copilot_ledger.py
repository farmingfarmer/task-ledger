#!/usr/bin/env python3
"""Build a second ledger from Copilot's claims, for side-by-side comparison.

    DB_DSN=... python3 scripts/copilot_ledger.py copilot_claims_raw.txt
    DB_DSN=... python3 scripts/copilot_ledger.py claims.txt --apply

Dry run by default.

The dashboard already knows how to show two ledgers and switch between
them -- that is what TL_COPILOT_DSN and the "Ledger" control in the
header are for. What was missing was one command to build the second
one, so the comparison stayed a thought experiment.

This creates a SEPARATE database, loads the schema, imports the claims,
and reconciles every person who has any -- so both ledgers have been
through the same gates, the same corroboration and the same matching,
and the only difference between them is who did the extraction. That is
the point: comparing Copilot's raw output to a reconciled board would
flatter whichever side you were hoping for.

It never touches the real ledger, and refuses if pointed at it.

--bootstrap is the DEFAULT here and off in the underlying ingester,
which is the right way round for each. A Copilot export is mostly
progress and blockers on work already under way; against an empty
database those have nothing to attach to, so twenty claims yield one
proposal. A comparison ledger is empty by definition.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))


def dsn_parts(dsn):
    return dict(kv.split("=", 1) for kv in (dsn or "").split() if "=" in kv)


def swap_db(dsn, newname):
    parts = dsn_parts(dsn)
    parts["dbname"] = newname
    return " ".join("{0}={1}".format(k, v) for k, v in parts.items())


def psql_tool(name):
    import shutil
    pgbin = os.environ.get("PGBIN")
    if pgbin:
        for cand in (os.path.join(pgbin, name + ".exe"),
                     os.path.join(pgbin, name)):
            if os.path.exists(cand):
                return cand
    return shutil.which(name)


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, **kw).returncode


def main():
    ap = argparse.ArgumentParser(
        description="Build a comparison ledger from Copilot claims.")
    ap.add_argument("claims", help="the file Copilot's JSON was saved to")
    ap.add_argument("--dbname", default="taskintel_copilot")
    ap.add_argument("--label", default="copilot-claims")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset -- it names the server, and the comparison "
              "database is created beside your real one.")
        return 2
    real = dsn_parts(dsn).get("dbname")
    if args.dbname == real:
        print("refusing: --dbname is your REAL ledger ({0}). The comparison "
              "goes in its own database.".format(real))
        return 2
    if not os.path.exists(args.claims):
        print("no such file: {0}".format(args.claims))
        return 2

    target = swap_db(dsn, args.dbname)
    createdb, dropdb, psql = (psql_tool("createdb"), psql_tool("dropdb"),
                              psql_tool("psql"))
    if not all((createdb, dropdb, psql)):
        # Name the place, not the variable. "Set PGBIN" tells somebody
        # who already knows; the path tells everybody.
        print("could not find createdb/dropdb/psql.")
        print("  On Windows they ship with the portable Postgres:")
        print("     set \"PGBIN=%USERPROFILE%\\pgsql-portable\\pgsql\\bin\"")
        print("  Then re-run. (tl.bat sets this for you when the portable")
        print("  install is in its usual place.)")
        return 2

    # Checked BEFORE anything is dropped. The first version asked after
    # recreating the database, so a stopped Ollama cost the previous
    # comparison ledger and left an empty one in its place -- destroying
    # something to discover a dependency was missing. The tick learned
    # this twice; this script had to learn it again.
    host = os.environ.get("OLLAMA_HOST_URL", "http://localhost:11434")
    try:
        import urllib.request
        urllib.request.urlopen(host + "/api/tags", timeout=5).close()
    except Exception:                                   # noqa: BLE001
        print("\nOllama is not answering at {0}.".format(host))
        print("  Reconciling needs it for embeddings, so this would import")
        print("  the claims and then fail once per person.")
        print("  Start it in its own window, then run this again:")
        print("     start \"ollama\" ollama serve")
        return 2

    print("comparison ledger '{0}' (beside your real '{1}')".format(
        args.dbname, real))
    print("  1. drop and recreate it -- it exists only for this comparison")
    print("  2. load schema.sql + access_control.sql")
    print("  3. import {0} (bootstrap: claims may create work)".format(
        os.path.basename(args.claims)))
    print("  4. reconcile everyone who has claims, through the same gates")
    if not args.apply:
        print("\nDRY RUN -- nothing done. Re-run with --apply.")
        return 0

    host = dsn_parts(dsn).get("host", "localhost")
    port = dsn_parts(dsn).get("port", "5432")
    base = ["-h", host, "-p", port]
    run([dropdb] + base + ["--if-exists", args.dbname])
    if run([createdb] + base + [args.dbname]) != 0:
        print("createdb failed.")
        return 1
    for rel in ("storage/schema.sql", "access/access_control.sql"):
        if run([psql] + base + ["-q", "-v", "ON_ERROR_STOP=1",
                                "-d", args.dbname, "-f",
                                os.path.join(ROOT, rel)]) != 0:
            print("failed loading {0}".format(rel))
            return 1

    print("\nimporting claims...")
    if run([sys.executable, os.path.join(ROOT, "scripts",
                                         "copilot_claims_ingest.py"),
            os.path.abspath(args.claims), "--dsn", target,
            "--label", args.label, "--bootstrap"]) != 0:
        print("import failed -- run copilot_claims_ingest.py --dry-run on "
              "that file to see what it made of the JSON.")
        return 1

    # Same gates, same corroboration, same matching as the real ledger --
    # otherwise this compares Copilot's raw output against a reconciled
    # board, which is not a comparison of extractors at all.
    import psycopg2
    conn = psycopg2.connect(target)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT user_ref FROM v_unconsumed_signals")
        people = sorted(r[0] for r in cur.fetchall())
    conn.close()
    print("\nreconciling {0} person/people through the same gates..."
          .format(len(people)))
    embed = os.environ.get("TL_EMBED_MODEL", "nomic-embed-text")
    judge = os.environ.get("TL_JUDGE_MODEL")
    failed = []
    for who in people:
        cmd = [sys.executable, os.path.join(ROOT, "reconciler",
                                            "run_reconcile.py"),
               "--user", who, "--dsn", target, "--write",
               "--embed-model", embed]
        if judge:
            cmd += ["--judge-model", judge]
        # stderr is kept: a reconcile that dies should say why here, not
        # scroll past as somebody else's stack trace.
        rc = run(cmd, stdout=subprocess.DEVNULL)
        print("   {0}{1}".format(who, "" if rc == 0 else "   FAILED"))
        if rc != 0:
            failed.append(who)

    if failed:
        print("\n{0} of {1} reconcile(s) failed, so this ledger is "
              "half-built and would compare badly for reasons that have "
              "nothing to do with Copilot.".format(len(failed), len(people)))
        print("Fix what the error above says and run this again -- it "
              "rebuilds from scratch, so a second run costs nothing.")
        return 1

    print("\nBuilt. To see both side by side:\n")
    print('   set "TL_COPILOT_DSN={0}"'.format(target))
    print("   set TL_VIEWER_PICKER=1")
    print("   set TL_HANDS_ON=1")
    print("   tl.bat dashboard\n")
    print("A 'Ledger' control appears in the header: Real ledger vs "
          "Copilot claims.")
    print("Pick the same person on each and compare what they are asked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
