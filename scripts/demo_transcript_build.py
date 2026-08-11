#!/usr/bin/env python3
"""Build a demo board from Teams transcripts, one meeting at a time.

    python3 testdata/synth_transcripts.py --out ~/tl-demo
    python3 scripts/demo_transcript_build.py ~/tl-demo/transcripts.txt \\
        --dsn "$TL_DEMO_DSN"

WHY THIS IS NOT demo_build.py. That one replays committed extraction
fixtures, which is right when the point is a board that travels through
git without a model. This drives the LIVE chain from a raw transcript --
parse, extract, reconcile, decide -- because the thing being demonstrated
is that a meeting becomes a board, and a fixture cannot show that.

ORDER IS THE WHOLE POINT, and it is the part that is easy to get wrong.
Extract every meeting, then reconcile once, and the demo comes out empty
of everything interesting: a completion claim needs a task to complete,
a blocker needs a task to block, and on a cold board neither exists. Both
are then dropped as no_open_target -- silently, since a dropped signal is
not an error. So each meeting is carried the whole way -- extracted,
reconciled, DECIDED onto boards -- before the next one is read. Meeting
two then lands its progress against tasks meeting one created, which is
also exactly how the real cadence tick behaves day to day.

The last meeting is left entirely undecided -- its proposals ARE the review
queue -- while the earlier ones are decided out, because a demo needs a
board with history AND something still waiting on a person. Decisions are
seeded in other people's names, which is
defensible only on invented work -- so this refuses to run against the
real ledger unless told twice.

Python 3.8 compatible.
"""

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import psycopg2                     # noqa: E402
import psycopg2.extras              # noqa: E402
import demo_build                   # noqa: E402


def sh(cmd, quiet=True):
    """Run a real CLI the way a person would, so the demo exercises the
    same entry points the documentation names."""
    res = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    if res.returncode != 0:
        sys.stderr.write(res.stdout.decode("utf-8", "replace")[-2000:])
        sys.stderr.write(res.stderr.decode("utf-8", "replace")[-2000:])
        raise SystemExit("failed: {0}".format(" ".join(cmd[:3])))
    return res.stdout.decode("utf-8", "replace")


def windows_in(path):
    """Every ingested window, oldest meeting first."""
    out = []
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json") or name == "golden.json":
            continue
        full = os.path.join(path, name)
        try:
            with open(full, encoding="utf-8") as f:
                spec = json.load(f)
            job = spec["job"]
        except (ValueError, KeyError):
            continue
        out.append((job.get("window_start") or "", full, name))
    out.sort()
    return out


def pooled_users(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_ref FROM v_unconsumed_signals"
                        " ORDER BY user_ref")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def board_counts(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks_current"
                        " WHERE merged_into IS NULL")
            tasks = cur.fetchone()[0]
            cur.execute("""
                SELECT count(*) FROM proposals p
                LEFT JOIN decisions d ON d.proposal_id = p.proposal_id
                WHERE d.decision_id IS NULL""")
            undecided = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM v_unconsumed_signals")
            held = cur.fetchone()[0]
            return tasks, undecided, held
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("source", help="a transcripts .txt, or a directory of "
                                   "already-ingested window JSONs")
    ap.add_argument("--dsn", default=os.environ.get("TL_DEMO_DSN")
                    or os.environ.get("DB_DSN"))
    ap.add_argument("--work", default=None,
                    help="where ingested windows land (default: beside the "
                         "transcript)")
    ap.add_argument("--directory", help="JSON display name -> ref, for "
                                        "people who never speak")
    ap.add_argument("--model", default="mistral-small3.1")
    ap.add_argument("--embed-model", default="nomic-embed-text")
    ap.add_argument("--judge-model", default="mistral-small3.1")
    ap.add_argument("--decide-last", action="store_true",
                    help="also decide the final meeting, leaving no review "
                         "queue (default: its proposals are the queue)")
    ap.add_argument("--i-know-what-this-is", action="store_true",
                    dest="override")
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("set TL_DEMO_DSN or pass --dsn")
    name = demo_build.refuse_if_real(args.dsn, args.override)

    if os.path.isdir(args.source):
        work = args.source
    else:
        work = args.work or os.path.join(os.path.dirname(
            os.path.abspath(args.source)) or ".", "windows")
        cmd = [sys.executable, os.path.join(ROOT, "scripts",
                                            "transcript_ingest.py"),
               args.source, "--out", work]
        if args.directory:
            cmd += ["--directory", args.directory]
        sys.stdout.write(sh(cmd))

    windows = windows_in(work)
    if not windows:
        raise SystemExit("no windows found in " + work)
    print("building {0} from {1} meeting(s), oldest first".format(
        name, len(windows)))

    for i, (start, full, fname) in enumerate(windows):
        last = (i == len(windows) - 1)
        print("\n[{0}/{1}] {2}".format(i + 1, len(windows), fname))
        sh([sys.executable, os.path.join(ROOT, "extraction", "run_local.py"),
            "--transcript", full, "--model", args.model,
            "--write-db", args.dsn])

        users = pooled_users(args.dsn)
        if not users:
            print("  nothing pooled -- already built? re-running is safe.")
            continue
        for user in users:
            sh([sys.executable, os.path.join(ROOT, "reconciler",
                                             "run_reconcile.py"),
                "--user", user, "--dsn", args.dsn,
                "--embed-model", args.embed_model,
                "--judge-model", args.judge_model, "--write"])

        # Decided before the next meeting is read: that is what gives the
        # next meeting's completions and blockers a board to land on.
        #
        # Except the last one, which is left entirely undecided -- that IS
        # the review queue. decide_for reserves a SHARE of each packet
        # (len(props)//3), which rounds to nothing on packets this size, so
        # asking it to hold a few back quietly held none and the demo came
        # out with an empty queue and no human decision left to show.
        total = {"accepted": 0, "rejected": 0, "left": 0}
        if last and not args.decide_last:
            _, undecided, _ = board_counts(args.dsn)
            total["left"] = undecided
        else:
            conn = psycopg2.connect(args.dsn)
            try:
                for user in users:
                    s = demo_build.decide_for(conn, user, 0)
                    for k in total:
                        total[k] += s[k]
            finally:
                conn.close()
        tasks, undecided, held = board_counts(args.dsn)
        print("  {0} accepted, {1} rejected, {2} left waiting"
              " | board {3} task(s), {4} undecided, {5} held".format(
                  total["accepted"], total["rejected"], total["left"],
                  tasks, undecided, held))

    conn = psycopg2.connect(args.dsn)
    try:
        users = sorted(set(pooled_users(args.dsn)) | set(_board_users(conn)))
        grants = demo_build.seed_grants(conn, users)
    finally:
        conn.close()
    tasks, undecided, held = board_counts(args.dsn)
    print("\ndemo ready: {0} task(s), {1} awaiting review, {2} claim(s) "
          "held for corroboration, {3} grant(s)".format(
              tasks, undecided, held, grants))
    print("  python3 cli/review.py --as <user> --dsn \"{0}\"".format(args.dsn))
    return 0


def _board_users(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT user_ref FROM tasks_current")
        return [r[0] for r in cur.fetchall()]


if __name__ == "__main__":
    sys.exit(main())
