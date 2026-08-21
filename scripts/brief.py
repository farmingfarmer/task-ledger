#!/usr/bin/env python3
"""The meeting that remembers: what this room said, and what became of it.

    DB_DSN=... python3 scripts/brief.py                 # list known meetings
    DB_DSN=... python3 scripts/brief.py scrums          # brief for that series

Read-only. Run it before a recurring meeting and it answers the question
the first ten minutes usually burn on: what did we say last time, and did
any of it happen? Promises born in this room with their current state;
what moved since the last occurrence and what went quiet; work mentioned
here that nobody tracked, with the days it has left before the register
lets it fade; blockers still open. Every line is derived from receipts --
nothing here is a model's opinion of the meeting.

The recurring meeting is recovered from source_ref (serieslib): the slug
is the room, the dates are the occurrences. "This standup, last time" was
always in the data; it just never had a reader.
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for _s in ("reconciler", "scripts"):
    _p = os.path.join(ROOT, _s)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import psycopg2                                         # noqa: E402
import psycopg2.extras                                  # noqa: E402
import serieslib                                        # noqa: E402


def load_series(cur):
    """slug -> {"dates": sorted set, "batches": [batch_id]}"""
    cur.execute("""SELECT batch_id, source_ref, window_start
                   FROM extraction_batches ORDER BY window_start""")
    out = {}
    for r in cur.fetchall():
        slug, date = serieslib.parse(r["source_ref"])
        s = out.setdefault(slug, {"dates": set(), "batches": []})
        if date:
            s["dates"].add(date)
        s["batches"].append(r["batch_id"])
    return out


def main():
    ap = argparse.ArgumentParser(
        description="What this recurring meeting said, and what became of "
                    "it. Read-only.")
    ap.add_argument("meeting", nargs="*",
                    help="part of the meeting's name; empty lists them")
    args = ap.parse_args()
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset.")
        return 2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as exc:                            # noqa: BLE001
        print("database unreachable ({0}) -- start it (deploy/windows/"
              "pg_start.bat on Windows) and re-run."
              .format(exc.__class__.__name__))
        return 2
    try:
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            series = load_series(cur)
            if not args.meeting:
                print("Meetings the ledger has heard:\n")
                for slug, s in sorted(series.items(),
                                      key=lambda kv: -len(kv[1]["dates"])):
                    dates = sorted(s["dates"])
                    print("  {0:<44} {1} occurrence(s), {2} .. {3}".format(
                        serieslib.pretty(slug)[:44], max(len(dates), 1),
                        dates[0] if dates else "?",
                        dates[-1] if dates else "?"))
                print("\n  tl brief <part of a name> for the one-pager.")
                return 0

            want = " ".join(args.meeting).lower().replace(" ", "-")
            hits = [(slug, s) for slug, s in series.items()
                    if want in slug.lower()]
            if not hits:
                print("No meeting matches “{0}”. Run with no argument to "
                      "see the list.".format(" ".join(args.meeting)))
                return 1
            if len(hits) > 1:
                print("“{0}” matches {1} meetings -- be more specific:"
                      .format(" ".join(args.meeting), len(hits)))
                for slug, _s in hits:
                    print("  " + serieslib.pretty(slug))
                return 1
            slug, s = hits[0]
            dates = sorted(s["dates"])
            last = dates[-1] if dates else None
            bids = s["batches"]

            cur.execute("""SELECT DISTINCT author_ref FROM signals
                           WHERE batch_id = ANY(%s)
                             AND author_ref IS NOT NULL""", (bids,))
            people = sorted(r["author_ref"].replace("aad-", "")
                            for r in cur.fetchall())

            print("=" * 64)
            print("BEFORE {0}".format(
                serieslib.pretty(slug).upper()))
            print("{0} occurrence(s) on record · last {1} · voices: {2}"
                  .format(len(dates), last or "?", ", ".join(people)))
            print("=" * 64)

            # Work born from sentences spoken in this room -- via the
            # decision that accepted it, or the wording that taught it.
            cur.execute("""
                SELECT DISTINCT t.task_id, t.title, t.status, t.owner_ref,
                       t.deadline
                FROM tasks_current t
                WHERE t.merged_into IS NULL AND (
                  t.task_id IN (
                    SELECT e.task_id
                    FROM proposal_signals ps
                    JOIN signals s2 USING (signal_id)
                    JOIN decisions d ON d.proposal_id = ps.proposal_id
                                    AND d.decision = 'accept'
                    JOIN task_events e ON e.decision_id = d.decision_id
                    WHERE s2.batch_id = ANY(%s))
                  OR t.task_id IN (
                    SELECT a.task_id FROM task_aliases a
                    JOIN signals s3 ON s3.signal_id = a.source_signal_id
                    WHERE s3.batch_id = ANY(%s)))
                ORDER BY t.status, t.title""", (bids, bids))
            born = cur.fetchall()
            moved, quiet = [], []
            for t in born:
                cur.execute("""
                    SELECT event_type, occurred_at FROM task_events
                    WHERE task_id = %s AND event_type <> 'created'
                    ORDER BY event_id DESC LIMIT 1""", (t["task_id"],))
                ev = cur.fetchone()
                if ev and last and str(ev["occurred_at"])[:10] >= last:
                    moved.append((t, ev))
                elif t["status"] in ("completed", "cancelled"):
                    moved.append((t, ev))
                else:
                    quiet.append((t, ev))

            if moved:
                print("\nMOVED since this room last met")
                for t, ev in moved:
                    print("  {0:<46} {1}{2}".format(
                        t["title"][:46], t["status"],
                        "  ({0} {1})".format(
                            ev["event_type"],
                            str(ev["occurred_at"])[:10]) if ev else ""))
            if quiet:
                print("\nPROMISED HERE, gone quiet -- worth thirty seconds"
                      " each")
                for t, ev in quiet:
                    print("  {0:<46} {1}, owner {2}".format(
                        t["title"][:46], t["status"],
                        (t["owner_ref"] or "?").replace("aad-", "")))
            if not born:
                print("\nNo confirmed work has come from this meeting yet.")

            # Mentioned in this room, tracked by nobody, and on the clock.
            try:
                import reconciler
                window = (reconciler.load_policy()
                          .get("candidate_promotion") or {}).get(
                              "window_days", 14)
            except Exception:                           # noqa: BLE001
                window = 14
            cur.execute("""
                SELECT DISTINCT c.title, c.user_ref, c.sightings,
                       c.last_seen
                FROM task_candidate_events e
                JOIN signals s4 ON s4.signal_id = e.signal_id
                JOIN v_task_candidates c
                  ON c.candidate_id = e.candidate_id
                 AND c.user_ref = e.user_ref
                WHERE s4.batch_id = ANY(%s) AND c.state = 'sighted'
                ORDER BY c.last_seen""", (bids,))
            fading = cur.fetchall()
            if fading:
                print("\nMENTIONED HERE, TRACKED BY NOBODY")
                now = datetime.datetime.now(datetime.timezone.utc)
                for c in fading:
                    left = None
                    if c["last_seen"] is not None:
                        left = window - (now - c["last_seen"]).days
                    tail = ("fades in {0} day(s)".format(left)
                            if left is not None and left <= 5
                            else "{0} sighting(s)".format(c["sightings"]))
                    print("  {0:<46} {1} · {2}".format(
                        c["title"][:46],
                        c["user_ref"].replace("aad-", ""), tail))

            cur.execute("""
                SELECT t.title, b.description, b.blocking_party
                FROM task_blockers b JOIN tasks_current t USING (task_id)
                WHERE b.resolved_event_id IS NULL
                  AND t.task_id IN (
                    SELECT e.task_id
                    FROM proposal_signals ps
                    JOIN signals s5 USING (signal_id)
                    JOIN decisions d ON d.proposal_id = ps.proposal_id
                    JOIN task_events e ON e.decision_id = d.decision_id
                    WHERE s5.batch_id = ANY(%s))""", (bids,))
            blocked = cur.fetchall()
            if blocked:
                print("\nSTILL BLOCKED")
                for b in blocked:
                    print("  {0:<46} on {1}".format(
                        b["title"][:46],
                        (b["blocking_party"] or b["description"]
                         or "?").replace("aad-", "")))

            print("\n" + "-" * 64)
            print("Every line above traces to a sentence somebody said in "
                  "this meeting.")
            print("tl recall \"<words>\" quotes it back, with the receipt.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
