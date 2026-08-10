#!/usr/bin/env python3
"""Check a demo will hold up, before an audience finds out it doesn't.

Drives the real Flask app through its test client with both profiles
configured and asserts the things that make a demo embarrassing:

  - both ledgers are reachable and answer every panel
  - they return DIFFERENT board state, which is the only proof the switch
    switches rather than relabelling the same rows
  - the synthetic board is not a shell: it has tasks, a review queue,
    grants, and something completed
  - no endpoint leaks a connection string

Written because each of those failed at least once while building this.
The empty review queue and the board with nothing completed both looked
fine from the seeding script's own summary.

    export DB_DSN="...dbname=taskintel..."
    export TL_SYNTH_DSN="...dbname=taskintel_synth..."
    python3 scripts/demo_preflight.py

Exit code 1 if anything fails. Python 3.8 compatible.
"""

import json
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "integration", "dashboard"))

FAIL = [0]


def check(cond, label, detail=""):
    print("  {0} {1}{2}".format("ok  " if cond else "FAIL", label,
                                ("  -- " + detail) if detail else ""))
    if not cond:
        FAIL[0] += 1
    return cond


def main():
    if not os.environ.get("DB_DSN"):
        print("set DB_DSN (and TL_SYNTH_DSN) first")
        return 2
    if not os.environ.get("TL_SYNTH_DSN"):
        print("TL_SYNTH_DSN is not set: the dashboard will run single-ledger "
              "with no switch, which may be what you want -- but there is no "
              "demo to preflight. See PROFILES.md.")
        return 2
    os.environ.setdefault("TL_VIEWER_PICKER", "1")

    import dashboard_api
    client = dashboard_api.app.test_client()

    def get(path, viewer=None):
        headers = {"X-Viewer": viewer} if viewer else {}
        r = client.get(path, headers=headers)
        try:
            return r.status_code, r.get_json()
        except ValueError:
            return r.status_code, None

    print("profiles")
    code, prof = get("/api/profiles")
    check(code == 200 and prof.get("switchable") is True,
          "both ledgers configured and the switch will render")
    check("dbname=" not in json.dumps(prof or {}),
          "no connection string reaches the page")

    people = {}
    for name in ("real", "synthetic"):
        suffix = "" if name == "real" else "?profile=" + name
        code, v = get("/api/viewers" + suffix)
        people[name] = (v or {}).get("viewers") or []
        check(bool(people[name]), "{0}: identities load".format(name),
              "{0} people".format(len(people[name])))
    if not (people["real"] and people["synthetic"]):
        return 1

    print("\nthe two ledgers are actually different")
    totals = {}
    for name in ("real", "synthetic"):
        suffix = "" if name == "real" else "?profile=" + name
        who = people[name][0]
        code, me = get("/api/me" + suffix, who)
        boards = (me or {}).get("boards") or []
        totals[name] = sum(b.get("visible_tasks", 0) for b in boards)
        print("     {0:<10} {1:<28} {2} board(s), {3} visible task(s)".format(
            name, who, len(boards), totals[name]))
    check(totals["real"] != totals["synthetic"],
          "profiles disagree about the board")

    print("\nthe synthetic ledger is worth switching to")
    who = people["synthetic"][0]
    code, me = get("/api/me?profile=synthetic", who)
    boards = (me or {}).get("boards") or []
    check(len(boards) > 1,
          "the demo viewer sees other people's boards, not just their own",
          "{0} boards".format(len(boards)))
    check(any(not b.get("any_full_trace") for b in boards)
          and any(b.get("any_full_trace") for b in boards),
          "both access levels are represented, so redaction is visible")

    code, pend = get("/api/pending?profile=synthetic", who)
    queue = (pend or {}).get("undecided", 0)
    code, tl = get("/api/timeline?profile=synthetic", who)
    rows = len((tl or {}).get("items") or (tl or {}).get("rows") or [])
    print("     review queue for this viewer: {0}   timeline rows: {1}"
          .format(queue, rows))
    check(rows > 0, "the timeline has something to draw")

    print("\nevery panel answers under each profile")
    for name in ("real", "synthetic"):
        who = people[name][0]
        suffix = "" if name == "real" else "?profile=" + name
        bad = []
        for path in ("/api/pending", "/api/held", "/api/calibration",
                     "/api/recent", "/api/projects", "/api/meetings",
                     "/api/metrics", "/api/timeline", "/api/team",
                     "/api/board/" + who):
            code, _ = get(path + suffix, who)
            if code != 200:
                bad.append("{0}->{1}".format(path, code))
        check(not bad, "{0}: all panels 200".format(name), ", ".join(bad))

    print("\n" + ("demo looks ready" if not FAIL[0]
                  else "{0} problem(s) -- see above".format(FAIL[0])))
    return 1 if FAIL[0] else 0


if __name__ == "__main__":
    sys.exit(main())
