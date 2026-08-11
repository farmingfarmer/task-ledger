#!/usr/bin/env python3
"""Build the whole demo ledger from committed fixtures. No model required.

    python3 scripts/demo_build.py --dsn "$TL_SYNTH_DSN"
    python scripts\\demo_build.py --dsn "%TL_SYNTH_DSN%"     (Windows)

WHY THIS EXISTS. The demo ledger was generated with the REAL participant
directory, which can never enter this repository -- so it could not travel
to a machine that has no copy of a real window, and the two machines in
question will never be connected. testdata/demo_windows/ is the answer:
the same generator run against an INVENTED cast, so the windows are
ordinary fixtures, and testdata/demo_batches/ holds what the real
extractor made of them. Both are committable because nobody in them
exists.

What this does, in order, all offline:

  1. writes each committed batch through signal_store_pg.write_batch --
     the same store the live pipeline uses, so ids, pooling and routing
     are identical
  2. reconciles each person offline (TokenEmbedder, no judge)
  3. decides most proposals, rejects a few with reasons, leaves a queue
  4. shares boards between people at both access levels

Steps 3 and 4 are seeded decisions on invented work, and every one of them
says so in its note. Re-runnable: content-hash ids make step 1 an upsert,
and a decided proposal is never decided twice.

WHAT IS MISSING WITHOUT A MODEL, stated plainly because a demo should not
surprise its driver: closure signals need the judge band, so with no
Ollama nothing on this board reaches 'completed'. Pass --judge-model to
get completions on a machine that has one. Everything else -- boards,
grants, blockers, the review queue, the timeline -- is identical.

Python 3.8 compatible.
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for sub in ("extraction", "reconciler", "cli", "policy"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import psycopg2                     # noqa: E402
import psycopg2.extras              # noqa: E402

import board_io_pg                  # noqa: E402
import embedders                    # noqa: E402
import reconciler as rec_mod        # noqa: E402
import review as review_cli         # noqa: E402
import signal_store_pg              # noqa: E402

BATCHES = os.path.join(ROOT, "testdata", "demo_batches")

REJECT_REASONS = ["wrong_owner", "not_a_task", "duplicate", "wrong_status"]
NOTE = "seeded demo decision on invented work"
STATUS_OPS = ("complete_task", "update_status", "add_blocker",
              "resolve_blocker", "update_deadline", "reassign")


def refuse_if_real(dsn, override):
    name = ""
    for part in dsn.split():
        if part.startswith("dbname="):
            name = part.split("=", 1)[1]
    if not override and name in ("taskintel", "taskintel_prod"):
        raise SystemExit(
            "refusing to build a demo inside '{0}': this seeds decisions "
            "in other people's names, which is fine on invented work and "
            "not on theirs. Point --dsn at a demo database.".format(name))
    return name


def load_batches(dsn):
    files = sorted(glob.glob(os.path.join(BATCHES, "*.json")))
    if not files:
        raise SystemExit(
            "no fixtures in {0}. They are committed alongside the code; a "
            "checkout should already have them.".format(BATCHES))
    total, people, bad = 0, set(), []
    for path in files:
        try:
            with open(path) as f:
                batch = json.load(f)
        except ValueError as e:
            # Naming the file matters: this runs minutes before a demo, and
            # a bare traceback says nothing about which fixture to look at.
            bad.append("{0}: {1}".format(os.path.basename(path), e))
            continue
        fallback = sorted(set(
            c["person_ref"] for s in batch["signals"]
            for c in (s.get("owner_candidates") or [])))
        res = signal_store_pg.write_batch(dsn, batch, fallback)
        total += res["signals_inserted"]
        people.update(fallback)
        print("  {0}: {1} signal(s), {2} inserted".format(
            os.path.basename(path), len(batch["signals"]),
            res["signals_inserted"]))
    for b in bad:
        print("  UNREADABLE {0}".format(b))
    if bad and not total:
        raise SystemExit(
            "every fixture was unreadable -- a checkout should not be in "
            "this state; re-pull, or regenerate with "
            "testdata/synth_meetings.py --fictional")
    return total, people


def ollama_chat(model, host):
    import json as _json
    import urllib.request as _u

    def call(messages):
        body = _json.dumps({"model": model, "messages": messages,
                            "stream": False,
                            "options": {"temperature": 0.0}}).encode("utf-8")
        req = _u.Request(host + "/api/chat", data=body,
                         headers={"Content-Type": "application/json"})
        with _u.urlopen(req, timeout=300) as r:
            return _json.loads(r.read().decode("utf-8"))["message"]["content"]
    return call


def users_with_signals(dsn):
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT user_ref FROM v_unconsumed_signals "
                        "ORDER BY user_ref")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def reconcile_for(dsn, user, run_id, judge):
    signals = board_io_pg.load_signals(dsn, user)
    if not signals:
        return None
    tasks, version = board_io_pg.load_board(dsn, user)
    rec = rec_mod.Reconciler(embed=embedders.TokenEmbedder(), judge_llm=judge)
    packet, rep = rec.reconcile(user, signals, tasks, version, run_id,
                                all_tasks=board_io_pg.load_all_boards(dsn))
    res = board_io_pg.write_packet(dsn, packet, rep["consumed"],
                                   rep["annotations"], run_id)
    return {"proposals": res["proposals"], "held": len(rep["held"])}


def decide_for(conn, user, keep_pending):
    """Accept most, reject a few, leave a queue.

    Reserves from the TAIL and never reserves an op that moves an existing
    task: undecided_proposals sorts by confidence, so holding back the
    first N would hold back the strongest -- which is how an earlier
    version of this produced a board with nothing ever completed.
    """
    stats = {"accepted": 0, "rejected": 0, "left": 0}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        for packet in review_cli.pending_packets(cur, user):
            props = review_cli.undecided_proposals(cur, packet["packet_id"])
            # Reserve a share, not a fixed count. A flat 4 held back every
            # proposal in a packet that only had two, so nothing was ever
            # decided and the board came out empty -- the queue starving
            # the board is as bad as the board starving the queue.
            keep_n = min(keep_pending, len(props) // 3)
            cut = len(props) - keep_n
            for i, prop in enumerate(props):
                if i >= cut and prop["op"] not in STATUS_OPS:
                    stats["left"] += 1
                    continue
                ok, _why = review_cli.still_valid(prop)
                if not ok:
                    continue
                if i % 6 == 2:
                    cur.execute("""
                        INSERT INTO decisions (proposal_id, packet_id,
                          decision, reject_reason, reject_note, decided_by,
                          decided_at)
                        VALUES (%s,%s,'reject',%s,%s,%s,now())
                        ON CONFLICT (proposal_id, decided_by) DO NOTHING""",
                        (prop["proposal_id"], packet["packet_id"],
                         REJECT_REASONS[i % len(REJECT_REASONS)], NOTE, user))
                    stats["rejected"] += 1
                    continue
                cur.execute("""
                    INSERT INTO decisions (proposal_id, packet_id, decision,
                      reject_note, decided_by, decided_at)
                    VALUES (%s,%s,'accept',%s,%s,now())
                    ON CONFLICT (proposal_id, decided_by) DO NOTHING
                    RETURNING decision_id""",
                    (prop["proposal_id"], packet["packet_id"], NOTE, user))
                if cur.fetchone() is None:
                    continue
                payload = dict(prop["payload"] or {})
                if review_cli.needs_owner(prop):
                    payload["owner_ref"] = user
                review_cli.apply_accept(cur, user, prop, payload)
                stats["accepted"] += 1
            review_cli.record_state(
                cur, packet["packet_id"],
                "partially_decided" if stats["left"] else "closed")
    conn.commit()
    return stats


def seed_grants(conn, users):
    """Both levels on purpose: a demo that only ever shows full_trace never
    shows the redaction working."""
    made = 0
    with conn.cursor() as cur:
        for i, owner in enumerate(users):
            for grantee, level in ((users[(i + 1) % len(users)], "full_trace"),
                                   (users[(i + 2) % len(users)], "progress")):
                if grantee == owner:
                    continue
                cur.execute(
                    "SELECT grant_access(%s,%s,'user',%s,%s,%s,NULL)",
                    (owner, grantee, owner, level, NOTE))
                made += 1
    conn.commit()
    return made


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dsn", default=os.environ.get("TL_SYNTH_DSN")
                    or os.environ.get("DB_DSN"))
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--keep-pending", type=int, default=4)
    ap.add_argument("--judge-model", default=None,
                    help="Ollama model for the judge band; without it "
                         "closure signals hold and nothing completes")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--i-know-what-this-is", action="store_true",
                    dest="override")
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("set TL_SYNTH_DSN or pass --dsn")
    name = refuse_if_real(args.dsn, args.override)
    judge = (ollama_chat(args.judge_model, args.ollama_host)
             if args.judge_model else None)

    print("loading committed extraction fixtures into {0}".format(name))
    inserted, people = load_batches(args.dsn)
    print("  {0} signal(s) inserted across {1} people".format(
        inserted, len(people)))

    users = users_with_signals(args.dsn)
    if not users:
        print("nothing pooled -- already built? re-running is safe.")
    total = {"accepted": 0, "rejected": 0, "left": 0}
    for r in range(1, args.rounds + 1):
        print("round {0}/{1}".format(r, args.rounds))
        for user in users:
            rep = reconcile_for(args.dsn, user, "demo-build-r{0}".format(r),
                                judge)
            if rep and rep["proposals"]:
                print("  {0}: {1} proposal(s), {2} held".format(
                    user, rep["proposals"], rep["held"]))
        conn = psycopg2.connect(args.dsn)
        try:
            for user in users:
                s = decide_for(conn, user, args.keep_pending)
                for k in total:
                    total[k] += s[k]
        finally:
            conn.close()

    conn = psycopg2.connect(args.dsn)
    try:
        grants = seed_grants(conn, users)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tasks_current "
                        "WHERE merged_into IS NULL")
            tasks = cur.fetchone()[0]
            cur.execute("SELECT status, count(*) FROM tasks_current "
                        "WHERE merged_into IS NULL GROUP BY 1 ORDER BY 1")
            by_status = ", ".join("{0}={1}".format(a, b)
                                  for a, b in cur.fetchall())
    finally:
        conn.close()

    print("\nboard: {0} task(s) ({1})".format(tasks, by_status))
    print("       {0} accepted, {1} rejected with reasons, {2} awaiting "
          "review, {3} grants".format(total["accepted"], total["rejected"],
                                      total["left"], grants))
    if not judge:
        print("       no judge: closure signals are held, so nothing here "
              "is completed. That is the designed degradation, not a "
              "fault -- pass --judge-model on a machine with Ollama.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
