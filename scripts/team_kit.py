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
import os
import shutil
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

START_HERE = """# If something goes wrong

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
    ap.add_argument("--no-ledger", action="store_true",
                    help="ship the code only -- for someone who was not in "
                         "the meetings")
    args = ap.parse_args()

    dsn = os.environ.get("DB_DSN")
    parts = dict(kv.split("=", 1) for kv in (dsn or "").split() if "=" in kv)
    db = parts.get("dbname", "?")
    dump = find_tool("pg_dump")

    print("kit for one teammate -> {0}".format(args.out))
    print("  1. task-ledger.bundle   the repository, full history")
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
    else:
        print("  2. ledger.sql           a copy of '{0}' -- REAL MEETING "
              "CONTENT".format(db))
    print("  3. START-HERE.md        one page, for someone new to all of it")
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
    bundle = os.path.join(args.out, "task-ledger.bundle")
    if not run(["git", "bundle", "create", bundle, "--all"]):
        print("git bundle failed -- is this a git checkout?")
        return 1

    if not args.no_ledger and dsn and dump:
        target = os.path.join(args.out, "ledger.sql")
        cmd = [dump, "-h", parts.get("host", "localhost"),
               "-p", parts.get("port", "5432"), "-d", db, "-f", target]
        if parts.get("user"):
            cmd += ["-U", parts["user"]]
        if subprocess.run(cmd).returncode != 0:
            print("pg_dump failed -- kit is incomplete, stopping.")
            return 1

    # The launcher goes at the TOP of the kit, alone, because the first
    # thing a colleague sees should be one obviously-clickable file and
    # not a directory that needs explaining. The written instructions
    # come second and exist for the person who wants them; the product
    # itself greets whoever just double-clicks.
    launcher = os.path.join(ROOT, "deploy", "windows",
                            "START-TASK-LEDGER.bat")
    if os.path.exists(launcher):
        shutil.copy(launcher, os.path.join(args.out,
                                           "START-TASK-LEDGER.bat"))
    with open(os.path.join(args.out, "if-something-goes-wrong.md"), "w",
              encoding="utf-8") as f:
        f.write(START_HERE)

    print("\nkit ready: {0}".format(args.out))
    for name in sorted(os.listdir(args.out)):
        size = os.path.getsize(os.path.join(args.out, name))
        print("  {0:<24} {1:>8} KB".format(name, size // 1024))
    if not args.no_ledger:
        print("\nledger.sql holds real meeting content. Give this kit only "
              "to people who were in those meetings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
