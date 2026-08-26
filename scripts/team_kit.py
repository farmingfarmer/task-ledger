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

START_HERE = """# Task-ledger — start here

You are holding a complete copy of a system that turns meeting
transcripts into a tracked board of work, plus a copy of the ledger
built from **your own meetings**. Nothing in here talks to a network.
Everything you do stays on your machine, and you cannot break anyone
else's copy.

## What you need

- **Python 3.10+** and **PostgreSQL** (any recent version).
- **Ollama** only if you want to process new transcripts. To look at
  what is already here and answer your own cards, you do not need it.

## Setting it up (about five minutes)

```
git clone task-ledger.bundle task-ledger
cd task-ledger
createdb taskintel
psql -d taskintel -f ../ledger.sql
```

Windows: use `tl.bat` wherever this says `./tl`, and the Postgres tools
live under your install directory (see
`deploy/windows/WINDOWS_PORT.md`).

Then tell it where the database is and check everything:

```
export DB_DSN="host=localhost port=5432 dbname=taskintel user=$(whoami)"
./tl
```

`./tl` is the doctor. It checks every dependency and names the fix for
anything missing. If it is red, nothing else is worth trying yet.

## The part you actually want

```
export TL_VIEWER_PICKER=1
export TL_HANDS_ON=1
./tl dashboard
```

Open **http://127.0.0.1:8090** and pick your own name from the "Viewing
as" dropdown at the top right.

You will get a popup: things the system heard in your meetings and wants
you to rule on. Three kinds of question:

- **Heard in your meetings** — work somebody mentioned that is on
  nobody's board. *Yes, track it* or *Not a task*.
- **New task** — work corroborated enough that the system thinks it is
  real. Accept and it appears on your board; reject and say why.
- **Updates to work you already have** — "this looks finished", "this
  looks in progress". Accept, or reject if it got you wrong.

Every card shows the sentence it came from, who said it, and in which
meeting. Answer a few, then close the popup and look at your own page:
what you accepted is now on your board, with its receipts.

**Nothing moves until you answer, and nothing you answer is held against
you.** Rejecting is as useful to us as accepting — more, actually,
because it tells us where the system is wrong.

## Worth trying, in order of how interesting it is

```
./tl brief                       # every meeting the ledger has heard
./tl brief <part of a name>      # what that meeting said, and what came of it
./tl recall <some words>         # who said that, quoted, with the receipt
./tl handover <your name>        # everything you carry, with its history
./tl review                      # the same cards, in the terminal
```

`./tl brief` before a recurring meeting is the one most people find
hardest to give up afterwards.

## What we want back

Blunt opinions, especially negative ones:

- What did it get **wrong** about your work — invented, misattributed,
  missed entirely?
- Which of the five commands above would you actually use, and which are
  clutter?
- What would have to be true for you to want this running every week?
- What made you distrust it?

There is no wrong answer and no scorecard. The system tracks work, never
people — that is a design commitment, not a setting.

## If something breaks

Run `./tl` first; it usually names the problem and the fix. Beyond that,
`docs/ONBOARDING.md` explains how the whole pipeline works, and
`git log` is the real documentation — every commit says what broke and
why the fix looks the way it does.
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

    with open(os.path.join(args.out, "START-HERE.md"), "w",
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
