#!/usr/bin/env python3
"""Package the whole system, plus a copy of the ledger, for one teammate.

    DB_DSN=... python3 scripts/team_kit.py                 # plan only
    DB_DSN=... python3 scripts/team_kit.py --apply
    DB_DSN=... python3 scripts/team_kit.py --apply --out ~/Desktop/kit

Produces a folder holding three things and nothing else:

  task-ledger.bundle   the entire repository, history included -- this
                       project's commit messages ARE its documentation,
                       so a zip of the working tree would hand over the
                       code and throw away the reasoning
  ledger.sql           a dump of the ledger, so everyone opens the same
                       meetings and can compare what they each answered
  START-HERE.md        one page, written for somebody who has never seen
                       any of this

WHY A COPY EACH rather than one server everyone browses. The system's
first promise is that everything runs locally and nothing is sent
anywhere; standing up a shared host to demo it would break the exact
guarantee the demo is meant to prove. A copy each also means a wrong
answer costs nothing -- there is no shared state to spoil, and anyone
can start over by restoring the dump again.

THE DUMP CONTAINS REAL MEETING CONTENT. Give it only to people who were
in those meetings: they already heard every word, so it discloses
nothing, and judging the system against their own memory of the room is
the most useful thing they can do with it. Everyone else gets
./tl demo-set, which invents its own meetings.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

START_HERE = r"""# If something goes wrong

You should not need this page. Double-click **START-TASK-LEDGER.bat**
and the app opens in your browser, greets you, and asks which of the
names is you. Everything below is for the case where that does not
happen.

## "Python was not found"

Install Python 3.10 or newer from python.org. During setup tick **"Add
python.exe to PATH"** — that box is the whole problem when this
message appears. Then double-click the launcher again.

## "PostgreSQL was not found"

Ask whoever sent you this kit for the portable PostgreSQL folder, unzip
it to `%USERPROFILE%\pgsql-portable`, and run the launcher again. It
does not need installing or admin rights.

## The window opens and closes immediately

Open a Command Prompt, drag the launcher into it, press Enter. The
error stays on screen instead of vanishing with the window. Send us
whatever it says.

## The page is full of red errors, or panels say 500

Close the black window, delete the file `setup-log.txt` if it is there,
and double-click the launcher again — it checks the database really
works now and rebuilds it from scratch if not. If it still fails it
will stop with a message and point at `setup-log.txt`; send us that
file.

## The browser opens but the page is blank

Give it a few seconds and refresh — the database can take a moment on
first run. If it stays blank, the black window will have a line
explaining why.

## Nothing is waiting for me

That is a real answer, not a fault: the system had nothing to ask you
this round. Try the "Viewing as" dropdown to look at a colleague's
view, or just close it.

## What we are actually asking of you

Fifteen minutes. Answer your cards, then tell us:

- What did it get **wrong** about your work — invented, misattributed,
  missed entirely?
- Would you want this running every week? What would have to change?
- What made you distrust it?

Rejecting cards is more useful to us than accepting them. There is no
scorecard of people and never will be — it tracks work, not workers.

## For the curious

It runs entirely on your machine; nothing is sent anywhere. If you want
to see how it works, the repository is in `task-ledger.bundle`
(`git clone task-ledger.bundle`), and `docs/ONBOARDING.md` inside it
explains the whole pipeline.
"""


def _dir_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total // (1024 * 1024)


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT).returncode == 0


def find_tool(name):
    pgbin = os.environ.get("PGBIN")
    if pgbin:
        for cand in (os.path.join(pgbin, name + ".exe"),
                     os.path.join(pgbin, name)):
            if os.path.exists(cand):
                return cand
    return shutil.which(name)


def main():
    ap = argparse.ArgumentParser(
        description="Package the system plus a ledger copy for a teammate.")
    ap.add_argument("--out", default=os.path.join(
        os.path.expanduser("~"), "task-ledger-kit"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--copilot-dsn", default=os.environ.get("TL_COPILOT_DSN"),
                    help="a SECOND ledger built from the same meetings by "
                         "another extractor. Shipped as ledger-b.sql and "
                         "shown blind beside the first, so the comparison "
                         "measures the cards and not the labels.")
    ap.add_argument("--for", dest="for_person", default=None,
                    help="who this kit is for. Their dashboard greets them "
                         "by name, fixes their identity (no dropdown to "
                         "pick the wrong person in), and shows them the "
                         "steps and the return address. Matched against "
                         "the people the ledger actually knows.")
    ap.add_argument("--for-everyone", action="store_true",
                    help="build one kit per person the shipping ledger "
                         "knows (every aad-* ref with work in it), each "
                         "into ~/task-ledger-kit-<name>. The plan lists "
                         "who qualifies; --apply builds them all. "
                         "Combine with --zip to get send-ready archives.")
    ap.add_argument("--zip", action="store_true", dest="make_zip",
                    help="after a successful build, pack the kit folder "
                         "into <out>.zip, ready to send")
    ap.add_argument("--return-to", default=None,
                    help="where the answer files go back to, shown on "
                         "their final screen. Defaults to your git "
                         "name and email.")
    ap.add_argument("--blind-salt", default=os.environ.get("TL_BLIND_SALT"),
                    help="the salt that decides who sees which ledger "
                         "first. Pick one with 'tl blind <people> "
                         "--balance' and pass it here so every kit agrees "
                         "-- kits with different salts would show the same "
                         "person different arms, and the survey files "
                         "would disagree about which ledger 'A' was.")
    ap.add_argument("--no-ledger", action="store_true",
                    help="ship the code only -- for someone who was not in "
                         "the meetings")
    ap.add_argument("--ledger", choices=("copilot", "real", "both"),
                    default="copilot",
                    help="which extraction mechanism's ledger ships. The "
                         "blind pilot decided this: Copilot's summaries "
                         "won, so 'copilot' is the default -- ONE ledger, "
                         "from taskintel_copilot, no comparison arm. "
                         "'real' ships the local extractor's ledger alone; "
                         "'both' rebuilds the two-arm blind kit the pilot "
                         "used.")
    ap.add_argument("--with-runtime", action="store_true",
                    help="bundle the portable Python from "
                         "deploy/windows/build_portable_python.bat, so the "
                         "teammate needs nothing installed")
    ap.add_argument("--with-postgres", action="store_true",
                    help="bundle the portable PostgreSQL too -- a much "
                         "larger kit that needs nothing at all")
    ap.add_argument("--runtime", default=os.path.join(
        os.path.expanduser("~"), "task-ledger-runtime"))
    ap.add_argument("--postgres", default=os.path.join(
        os.path.expanduser("~"), "pgsql-portable"))
    args = ap.parse_args()

    dsn = os.environ.get("DB_DSN")
    parts = dict(kv.split("=", 1) for kv in (dsn or "").split() if "=" in kv)

    # The pilot's verdict, executed: the ledger that ships IS the one
    # built from Copilot's summaries. DB_DSN keeps naming the original
    # database because every other tool still reads it; this tool swaps
    # the dbname rather than asking the operator to maintain a second
    # session line -- that disease is cured and stays cured. --for is
    # resolved against the SAME swapped dsn, because the kit's identity
    # must exist in the ledger the kit actually carries.
    if args.ledger == "copilot" and dsn and \
            parts.get("dbname") != "taskintel_copilot":
        parts["dbname"] = "taskintel_copilot"
        dsn = " ".join("{0}={1}".format(k, v) for k, v in parts.items())
        print("  ledger: copilot extraction (the decided mechanism) -- "
              "'taskintel_copilot' ships as ledger.sql")

    # One kit per person the shipping ledger knows. Enumerate, show,
    # and (on --apply) re-run this same tool once per ref -- each build
    # is the ordinary single-kit path, so everything it checks and
    # refuses stays checked and refused.
    if args.for_everyone:
        if not dsn:
            print("--for-everyone needs DB_DSN set.")
            return 2
        try:
            import psycopg2
            conn = psycopg2.connect(dsn, connect_timeout=5)
            with conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT user_ref FROM signal_pool_events
                    UNION SELECT DISTINCT user_ref FROM tasks_current""")
                refs = sorted(r[0] for r in cur.fetchall())
            conn.close()
        except Exception as exc:                        # noqa: BLE001
            print("could not read the ledger ({0})".format(
                exc.__class__.__name__))
            return 2
        # aad-* refs are real directory people; everything else in a
        # ledger is a fixture or a test artifact and gets no kit.
        people = [r for r in refs if r.startswith("aad-")]
        skipped = [r for r in refs if not r.startswith("aad-")]
        print("kits for everyone this ledger knows ({0} people):"
              .format(len(people)))
        for r in people:
            print("   " + r)
        if skipped:
            print("  (not people, skipped: {0})".format(", ".join(skipped)))
        print("REMINDER: the ledger is the same in every kit. Hand a kit "
              "only to someone who was in the corpus meetings.")
        if not args.apply:
            print("\nDRY RUN -- nothing built. Re-run with --apply.")
            return 0
        fails = []
        for r in people:
            print("\n=== building: {0} ===".format(r))
            cmd = [sys.executable, os.path.abspath(__file__),
                   "--for", r, "--apply", "--ledger", args.ledger]
            if args.return_to:
                cmd += ["--return-to", args.return_to]
            if args.make_zip:
                cmd += ["--zip"]
            if args.with_runtime:
                cmd += ["--with-runtime", "--runtime", args.runtime]
            if args.with_postgres:
                cmd += ["--with-postgres", "--postgres", args.postgres]
            if subprocess.run(cmd).returncode != 0:
                fails.append(r)
        print("\n=== everyone: {0} built, {1} failed{2}".format(
            len(people) - len(fails), len(fails),
            " (" + ", ".join(fails) + ")" if fails else ""))
        return 1 if fails else 0

    # Resolve --for against the people the ledger actually knows. A typo
    # here would build a kit that greets nobody and locks the dashboard
    # to an identity with no cards -- an empty, confusing deliverable --
    # so an ambiguous or unknown name stops the build and lists what
    # WOULD match, instead of shipping a guess.
    kit_for = kit_for_name = None
    if args.for_person:
        if not dsn:
            print("--for needs DB_DSN set: the name is checked against "
                  "the ledger.")
            return 2
        want = args.for_person.strip().lower().replace(" ", "-")
        try:
            import psycopg2
            conn = psycopg2.connect(dsn, connect_timeout=5)
            with conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT user_ref FROM signal_pool_events
                    UNION SELECT DISTINCT user_ref FROM tasks_current""")
                refs = sorted(r[0] for r in cur.fetchall())
            conn.close()
        except Exception as exc:                        # noqa: BLE001
            print("could not read the ledger to check --for ({0})"
                  .format(exc.__class__.__name__))
            return 2
        hits = [r for r in refs if r == want or r == "aad-" + want]
        if not hits:
            hits = [r for r in refs if want in r]
        if not hits:
            # Segment matching bridges middle initials and partial
            # names: 'yesha-patel' names aad-yesha-m-patel uniquely,
            # because both segments appear in it. All query segments
            # must be whole segments of the ref -- 'al' matching
            # 'alexander' would be guessing, and a kit guesses nothing.
            toks = [t for t in want.split("-") if t and t != "aad"]
            hits = [r for r in refs
                    if all(t in r.split("-") for t in toks)]
        if len(hits) != 1:
            print("--for '{0}' matches {1} people{2}".format(
                args.for_person, len(hits),
                ":" if hits else " in this ledger. It knows:"))
            for r in (hits or refs):
                print("   " + r)
            print("Say which one, exactly.")
            return 2
        kit_for = hits[0]
        kit_for_name = " ".join(
            w.capitalize() for w in kit_for.replace("aad-", "").split("-"))
        if args.out == os.path.join(os.path.expanduser("~"),
                                    "task-ledger-kit"):
            args.out = os.path.join(
                os.path.expanduser("~"),
                "task-ledger-kit-" + kit_for.replace("aad-", ""))

    return_to = args.return_to
    if kit_for and not return_to:
        try:
            gname = subprocess.check_output(
                ["git", "config", "user.name"],
                stderr=subprocess.DEVNULL).decode().strip()
            gmail = subprocess.check_output(
                ["git", "config", "user.email"],
                stderr=subprocess.DEVNULL).decode().strip()
            return_to = "{0} ({1})".format(gname, gmail) if gmail else gname
        except Exception:                               # noqa: BLE001
            return_to = None
    db = parts.get("dbname", "?")
    dump = find_tool("pg_dump")

    # Refuse to ship an empty ledger. A kit built from a rebuilt-but-not-
    # yet-ingested database packages nothing, says "REAL MEETING CONTENT"
    # while doing it, and lands on a colleague's desk as a system with no
    # data and invented names. That happened, and the tool was cheerful
    # throughout; being cheerful about shipping nothing is the bug.
    # A fresh window that skipped the `set CPDSN` line hands us the
    # LITERAL text "%CPDSN%" -- cmd does not expand unset variables, it
    # passes them through. Treat any %...% literal as unset instead of
    # dying on it downstream with a '?' nobody can act on.
    if args.copilot_dsn and re.match(r"^%.*%$", args.copilot_dsn.strip()):
        print("  (--copilot-dsn arrived as the unexpanded text '{0}' -- "
              "the set line never ran in this window; treating it as "
              "unset)".format(args.copilot_dsn.strip()))
        args.copilot_dsn = None
    # And when it IS unset, do what the dashboard already does: the
    # comparison ledger has a conventional name on the same server.
    # Probe for it, announce, use it. The session line stops existing.
    if args.ledger != "both":
        # A single-mechanism kit must not quietly grow a second arm just
        # because the comparison database still exists on this server.
        # The comparison ended; only --ledger both rebuilds it.
        if args.copilot_dsn:
            print("  (--copilot-dsn ignored: --ledger {0} ships one "
                  "ledger. Pass --ledger both for a blind comparison "
                  "kit.)".format(args.ledger))
        args.copilot_dsn = None
    if args.ledger == "both" and not args.no_ledger \
            and not args.copilot_dsn and dsn:
        _p = dict(kv.split("=", 1) for kv in dsn.split() if "=" in kv)
        _p["dbname"] = "taskintel_copilot"
        _cand = " ".join("{0}={1}".format(k, v) for k, v in _p.items())
        try:
            import psycopg2
            psycopg2.connect(_cand, connect_timeout=3).close()
            args.copilot_dsn = _cand
            print("  second ledger: found taskintel_copilot on the same "
                  "server -- shipping it blind (pass --copilot-dsn to "
                  "override, --no-ledger to skip)")
        except Exception:                               # noqa: BLE001
            print("  second ledger: no taskintel_copilot on this server "
                  "-- shipping ONE ledger, no blind comparison")

    content = None
    if dsn and not args.no_ledger:
        try:
            import psycopg2
            conn = psycopg2.connect(dsn, connect_timeout=5)
            with conn, conn.cursor() as cur:
                cur.execute("SELECT (SELECT count(*) FROM signals), "
                            "(SELECT count(*) FROM tasks_current)")
                content = cur.fetchone()
            conn.close()
        except Exception as exc:                        # noqa: BLE001
            print("could not read the ledger ({0}) -- is the database "
                  "running?".format(exc.__class__.__name__))
            if args.ledger == "copilot":
                print("  (--ledger copilot ships '{0}'. If it does not "
                      "exist yet, build it first:\n"
                      "     tl copilot copilot_claims_raw.txt --apply)"
                      .format(parts.get("dbname", "taskintel_copilot")))
            return 2

    print("kit for one teammate -> {0}".format(args.out))
    print("  1. app/                 the runnable code")
    print("     task-ledger.bundle   the repository's full history, for "
          "the curious")
    if args.no_ledger:
        print("  2. (no ledger -- code only)")
    elif not dsn:
        print("  2. BLOCKED: DB_DSN unset, so there is no ledger to copy")
        if args.apply:
            return 2
    elif not dump:
        print("  2. BLOCKED: pg_dump not found (set PGBIN or PATH)")
        if args.apply:
            return 2
    elif content == (0, 0):
        print("  2. BLOCKED: '{0}' holds 0 signals and 0 tasks.".format(db))
        print("     A kit from this ledger contains no meetings, and the")
        print("     dashboard would open with nothing to review.")
        if args.ledger == "copilot":
            print("     Build it from the Copilot export first:")
            print("       tl copilot copilot_claims_raw.txt --apply")
        else:
            print("     Ingest transcripts and run a tick first:")
            print("       tl transcripts <your transcripts file>")
            print("       tl tick")
        print("     (--no-ledger builds a code-only kit deliberately.)")
        return 2
    else:
        print("  2. ledger.sql           a copy of '{0}' -- {1} signal(s), "
              "{2} task(s), REAL MEETING CONTENT".format(
                  db, content[0], content[1]))
    # The second arm, counted and named in the PLAN. Doing the dump but
    # not saying so meant a dry run described a one-ledger kit and
    # --apply built a two-ledger one, which is the one thing a dry run
    # exists to prevent.
    bcontent = None
    if args.copilot_dsn and not args.no_ledger:
        bparts = dict(kv.split("=", 1)
                      for kv in args.copilot_dsn.split() if "=" in kv)
        bdb = bparts.get("dbname", "?")
        try:
            import psycopg2
            conn = psycopg2.connect(args.copilot_dsn, connect_timeout=5)
            with conn, conn.cursor() as cur:
                cur.execute("SELECT (SELECT count(*) FROM signals), "
                            "(SELECT count(*) FROM tasks_current)")
                bcontent = cur.fetchone()
            conn.close()
        except Exception as exc:                        # noqa: BLE001
            print("  3. BLOCKED: cannot read the second ledger '{0}' ({1}). "
                  "Fix TL_COPILOT_DSN, or drop --copilot-dsn to ship one "
                  "ledger.".format(bdb, exc.__class__.__name__))
            return 2
        if bcontent == (0, 0):
            print("  3. BLOCKED: the second ledger '{0}' holds 0 signals. "
                  "Half a comparison is worse than none.".format(bdb))
            return 2
        print("  3. ledger-b.sql         a copy of '{0}' -- {1} signal(s), "
              "{2} task(s)".format(bdb, bcontent[0], bcontent[1]))
        print("     shown BLIND: the two are Ledger A and Ledger B, "
              "assigned per person,")
        print("     and which is which is never shown to the reviewer.")
    if kit_for:
        print("  personal: greets {0} by name, fixes their identity, and "
              "routes answers to {1}".format(
                  kit_for_name, return_to or "(no --return-to and no git "
                  "identity -- their screen will say 'whoever gave you "
                  "this kit')"))
    print("  {0}. START-TASK-LEDGER.bat  the one file a teammate opens"
          .format(4 if bcontent else 3))
    # Bundling turns "install Python, install Postgres, then double-click"
    # into "double-click". The size is the entire cost, and it is paid
    # once by whoever builds the kit rather than repeatedly by every
    # person asked to evaluate the thing.
    for want, path, label in (
            (args.with_runtime, args.runtime, "python-runtime"),
            (args.with_postgres, args.postgres, "postgres")):
        if not want:
            continue
        if not os.path.isdir(path):
            print("  !  {0} not found at {1}".format(label, path))
            if label == "python-runtime":
                print("     build it first: "
                      "deploy\\windows\\build_portable_python.bat")
            if args.apply:
                return 2
        else:
            print("  +  {0:<16} {1} MB, bundled -- nothing to install"
                  .format(label, _dir_mb(path)))
    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return 0

    # A bundle carries COMMITTED history. Building a kit with unsaved
    # work hands teammates a version that silently lacks it -- caught in
    # testing, where a kit built mid-change shipped a dashboard that
    # refused the very hands-on mode the kit existed to demonstrate.
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("\nWARNING: uncommitted changes -- the bundle will NOT "
              "contain them:")
        for line in dirty.splitlines()[:8]:
            print("   " + line)
        print("   commit first, or the kit ships without your latest work.")

    os.makedirs(args.out, exist_ok=True)
    # Only names this tool has ever produced -- never a blanket wipe, in
    # case somebody points --out at a folder holding something else.
    for stale in ("START-HERE.md", "if-something-goes-wrong.md",
                  "START-TASK-LEDGER.bat", "ledger.sql",
                  "task-ledger.bundle"):
        old_path = os.path.join(args.out, stale)
        if os.path.exists(old_path):
            os.remove(old_path)
    bundle = os.path.join(args.out, "task-ledger.bundle")
    if not run(["git", "bundle", "create", bundle, "--all"]):
        print("git bundle failed -- is this a git checkout?")
        return 1

    # The runnable code, extracted. The bundle is for whoever wants the
    # history; it is useless to everyone else, because unpacking it needs
    # git -- one more thing to install, in a kit whose entire purpose is
    # that nothing needs installing. git archive takes exactly the
    # tracked files, so nothing local or accidental travels.
    app = os.path.join(args.out, "app")
    if os.path.isdir(app):
        shutil.rmtree(app)
    os.makedirs(app)
    tar = os.path.join(args.out, "_app.tar")
    if not run(["git", "archive", "--format=tar", "-o", tar, "HEAD"]):
        print("git archive failed -- stopping.")
        return 1
    import tarfile
    with tarfile.open(tar) as tf:
        tf.extractall(app)
    os.remove(tar)
    # The stamp the dashboard header shows. In the repo it comes from
    # git; a kit is a git-archive export with no .git, which left the
    # header reading "build unknown" on exactly the machines where the
    # build matters most -- a teammate's, mid bug report.
    try:
        stamp = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %cd",
             "--date=format:%Y-%m-%d %H:%M"],
            stderr=subprocess.DEVNULL).decode().strip()
        with open(os.path.join(app, "BUILD_ID"), "w") as f:
            f.write(stamp + "\n")
    except Exception:                                   # noqa: BLE001
        pass

    if not args.no_ledger and dsn and dump:
        target = os.path.join(args.out, "ledger.sql")
        # --no-owner --no-privileges: a plain dump bakes "ALTER ...
        # OWNER TO <builder's username>" into every object, and the
        # recipient's Postgres has no such role -- so the restore died
        # at the first ownership statement, on every machine except the
        # one that built the kit. First seen the first time a restore
        # ever ran under a different username; the objects belong to
        # whoever restores them, which is the point of a kit.
        cmd = [dump, "--no-owner", "--no-privileges",
               "-h", parts.get("host", "localhost"),
               "-p", parts.get("port", "5432"), "-d", db, "-f", target]
        if parts.get("user"):
            cmd += ["-U", parts["user"]]
        if subprocess.run(cmd).returncode != 0:
            print("pg_dump failed -- kit is incomplete, stopping.")
            return 1

    # The second arm. Same meetings, other extractor -- and it is either
    # in the kit and working or absent and unmentioned. A comparison that
    # half-ships is worse than none: the person doing it cannot tell
    # which half they are looking at, and every answer they give is
    # about a ledger nobody can identify afterwards.
    if not args.no_ledger and args.copilot_dsn and dump:
        bparts = dict(kv.split("=", 1)
                      for kv in args.copilot_dsn.split() if "=" in kv)
        bdb = bparts.get("dbname")
        if not bdb:
            print("--copilot-dsn has no dbname -- stopping.")
            return 1
        if bdb == db:
            print("--copilot-dsn names the same database as the first "
                  "ledger; there would be nothing to compare. Stopping.")
            return 1
        target = os.path.join(args.out, "ledger-b.sql")
        cmd = [dump, "--no-owner", "--no-privileges",
               "-h", bparts.get("host", "localhost"),
               "-p", bparts.get("port", "5432"), "-d", bdb, "-f", target]
        if bparts.get("user"):
            cmd += ["-U", bparts["user"]]
        if subprocess.run(cmd).returncode != 0:
            print("pg_dump of the second ledger failed -- kit would ship "
                  "a comparison with one arm. Stopping.")
            return 1
        print("  second ledger: {0} -> ledger-b.sql (shown blind as "
              "Ledger A / Ledger B)".format(bdb))
        # The salt travels WITH the kit, or the blind falls apart quietly:
        # every machine would default to the same shipped constant --
        # which is consistent but unbalanced -- and any operator who
        # re-salted locally would produce survey files that disagree with
        # everyone else's about which ledger "A" was.
        with open(os.path.join(args.out, "blind-salt.txt"), "w") as f:
            f.write((args.blind_salt or "task-ledger-blind-v1") + "\n")
        if not args.blind_salt:
            print("  NOTE: no --blind-salt given, shipping the default. "
                  "Check the split first:")
            print("     tl blind <the people getting kits> --balance")

    # The launcher goes at the TOP of the kit, alone, because the first
    # thing a colleague sees should be one obviously-clickable file and
    # not a directory that needs explaining. The written instructions
    # come second and exist for the person who wants them; the product
    # itself greets whoever just double-clicks.
    if kit_for:
        with open(os.path.join(args.out, "kit-config.json"), "w") as f:
            json.dump({"for": kit_for, "for_name": kit_for_name,
                       "return_to": return_to,
                       "ledger": args.ledger}, f, indent=1)

    launcher = os.path.join(ROOT, "deploy", "windows",
                            "START-TASK-LEDGER.bat")
    if os.path.exists(launcher):
        shutil.copy(launcher, os.path.join(args.out,
                                           "START-TASK-LEDGER.bat"))
    # The freshness choice, shipped beside the persistence default: the
    # launcher keeps a local ledger forever (answers survive upgrades),
    # so a kit carrying NEWER meetings needs an explicit, labeled way to
    # load them. It confirms, states the cost, and never touches
    # my-answers.
    refresher = os.path.join(ROOT, "deploy", "windows",
                             "REFRESH-LEDGER.bat")
    if os.path.exists(refresher):
        shutil.copy(refresher, os.path.join(args.out,
                                            "REFRESH-LEDGER.bat"))
    for want, path, name in (
            (args.with_runtime, args.runtime, "python-runtime"),
            (args.with_postgres, args.postgres, "postgres")):
        if want and os.path.isdir(path):
            # As ONE inner zip, not ~30,000 loose files. Loose, they get
            # zipped into the outer archive anyway -- and then Windows
            # Explorer on the receiving end extracts them one at a time
            # while Defender scans each: no progress bar, up to an hour,
            # and the first teammate to receive a kit reported a frozen
            # laptop. Nested, the outer ZIP is a dozen entries and
            # extracts instantly; the launcher unpacks the inner zips
            # locally with bsdtar on first run, past both slow paths.
            dest = os.path.join(args.out, name)
            if os.path.isdir(dest):
                shutil.rmtree(dest)     # superseded by the zip
            # Compressing 858 MB single-threaded takes minutes and the
            # bundles are byte-identical across every kit in a batch --
            # so the zip is built ONCE, cached beside the bundle it was
            # made from, and copied into each kit. Five kits: one pack,
            # four file copies. The cache is invalidated by deleting it,
            # which is also how a bundle update ships: rebuild the
            # bundle, delete the .kitcache.zip beside it.
            # Root the archive at the directory that actually CONTAINS
            # bin/. The portable Postgres unpacks as pgsql-portable\pgsql\
            # bin, and zipping from the outer folder shipped
            # postgres\pgsql\bin -- one level deeper than the launcher
            # looks. Invisible on the build machine, where the launcher
            # silently falls back to the local install, and fatal on a
            # teammate's, which has no local install to fall back to.
            src_root = path
            if name == "postgres" and not os.path.isdir(
                    os.path.join(src_root, "bin")):
                for sub in sorted(os.listdir(src_root)):
                    cand = os.path.join(src_root, sub)
                    if os.path.isdir(os.path.join(cand, "bin")):
                        src_root = cand
                        break
                else:
                    print("  !  {0}: no bin/ found under {1} -- not a "
                          "usable bundle. Stopping.".format(name, path))
                    return 2
            # v2: the layout fix above changes the arcnames, so caches
            # packed before it must not be reused.
            cache = path.rstrip("\\/") + ".kitcache-v2.zip"
            if not os.path.exists(cache):
                print("  packing {0} ({1} MB -- a few minutes, ONCE; "
                      "later kits reuse it)...".format(name, _dir_mb(path)),
                      flush=True)
                import zipfile
                tmp = cache + ".part"
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for droot, _dirs, files in os.walk(src_root):
                        for fn in files:
                            full = os.path.join(droot, fn)
                            rel = os.path.relpath(full, src_root)
                            zf.write(full, os.path.join(name, rel))
                os.replace(tmp, cache)   # never leave a half-written cache
            else:
                print("  {0}: reusing the packed copy from the last "
                      "build".format(name))
            shutil.copy(cache, dest + ".zip")

    with open(os.path.join(args.out, "if-something-goes-wrong.md"), "w",
              encoding="utf-8") as f:
        f.write(START_HERE)

    if args.make_zip:
        print("  packing {0}.zip ...".format(os.path.basename(args.out)))
        base = shutil.make_archive(args.out, "zip",
                                   root_dir=os.path.dirname(args.out),
                                   base_dir=os.path.basename(args.out))
        print("  send-ready: {0} ({1} MB)".format(
            base, os.path.getsize(base) // (1024 * 1024)))

    print("\nkit ready: {0}".format(args.out))
    for name in sorted(os.listdir(args.out)):
        full = os.path.join(args.out, name)
        if os.path.isdir(full):
            print("  {0:<24} {1:>8} MB".format(name + "/", _dir_mb(full)))
        else:
            print("  {0:<24} {1:>8} KB".format(
                name, os.path.getsize(full) // 1024))
    if not args.no_ledger:
        print("\nledger.sql holds real meeting content. Give this kit only "
              "to people who were in those meetings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
