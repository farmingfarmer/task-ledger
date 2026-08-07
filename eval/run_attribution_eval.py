"""Build and score the attribution eval set from real rejections.

    # 1. build the worksheet (writes OUTSIDE the repo, refuses otherwise)
    python3 eval/run_attribution_eval.py build --dsn "$DB_DSN" \
        --out ~/task-ledger-eval/attribution.json

    # 2. fill correct_owner_ref on each case, in any editor

    # 3. measure
    python3 eval/run_attribution_eval.py score \
        --worksheet ~/task-ledger-eval/attribution.json

The build step reads rejections from the ledger and their utterances from
the processed inbox, because evidence in the ledger is a pointer and a
hash -- the text was never copied in. The worksheet therefore contains
real colleague speech and must live outside the working tree; the builder
enforces that rather than trusting anyone to remember it.

Python 3.8 compatible. Requires psycopg2 for `build`; `score` is stdlib.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribution_eval as ev      # noqa: E402

REPO = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

SQL = """
SELECT p.proposal_id, d.reject_reason, d.decided_by,
       p.payload->>'owner_ref'            AS extracted_owner,
       p.confidence,
       s.signal_id, s.title, s.description,
       (s.owner_candidates->0->>'basis')  AS basis,
       array_agg(ev.resource_id ORDER BY ev.ordinal) AS resource_ids
FROM decisions d
JOIN proposals p         ON p.proposal_id = d.proposal_id
JOIN proposal_signals ps ON ps.proposal_id = p.proposal_id
JOIN signals s           ON s.signal_id = ps.signal_id
LEFT JOIN evidence_refs ev ON ev.signal_id = s.signal_id
WHERE d.decision = 'reject' AND d.reject_reason = ANY(%s)
GROUP BY p.proposal_id, d.reject_reason, d.decided_by, p.payload,
         p.confidence, s.signal_id, s.title, s.description, s.owner_candidates
ORDER BY p.proposal_id
"""


def cmd_build(args):
    import psycopg2
    import psycopg2.extras

    try:
        out = ev.assert_outside_repo(args.out, REPO)
    except ValueError as e:
        sys.stderr.write(str(e) + "\n")
        raise SystemExit(2)
    conn = psycopg2.connect(args.dsn)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(SQL, (args.reasons,))
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["resource_ids"] = [x for x in (r.get("resource_ids") or []) if x]
        if r.get("confidence") is not None:
            r["confidence"] = float(r["confidence"])

    inbox = os.path.expanduser(args.inbox)
    index = ev.index_inbox(ev.inbox_paths(inbox))
    cases = ev.build_cases(rows, index)
    sheet = ev.worksheet(cases, meta={
        "reasons": args.reasons, "inbox": inbox,
        "utterances_indexed": len(index),
        "note": "Real colleague speech. Do not commit; do not paste into "
                "code, comments, or commit messages."})

    missing = sum(1 for c in cases
                  for u in c["utterances"] if not u.get("text"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(sheet, f, indent=2)
    sys.stderr.write(
        "wrote {0}\n  {1} case(s) from {2} rejection reason(s)\n"
        "  {3} utterance(s) indexed from {4}\n".format(
            out, len(cases), len(args.reasons), len(index), inbox))
    if missing:
        sys.stderr.write("  {0} cited utterance(s) not found in the inbox "
                         "(archived windows)\n".format(missing))
    sys.stderr.write("  next: fill correct_owner_ref on each case, then "
                     "score\n")
    return 0


def cmd_score(args):
    with open(os.path.expanduser(args.worksheet)) as f:
        sheet = json.load(f)
    result = ev.score(sheet)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return 0
    print("Attribution eval -- {0} case(s), {1} labelled".format(
        result["cases"], result["labelled"]))
    if result["unlabelled"]:
        print("  {0} still unlabelled".format(result["unlabelled"]))
    if result.get("no_owner_stated"):
        print("  {0} name nobody at all (a routing finding, not an "
              "attribution one)".format(result["no_owner_stated"]))
    print("")
    print("  speaker identity would fix:      {0} ({1}%)".format(
        result["speaker_would_fix"], result["speaker_would_fix_pct"]))
    print("  owner is NOT the speaker:        {0}".format(
        result["speaker_is_not_owner"]))
    print("  no speaker recorded:             {0}".format(
        result["no_speaker_recorded"]))
    print("  blamed someone else entirely:    {0}".format(
        result["blamed_someone_other_than_the_speaker"]))
    print("  self-referential but unattributed: {0}".format(
        result["self_referential_but_unattributed"]))
    if result["by_extracted_basis"]:
        print("  by attribution basis claimed:    " + ", ".join(
            "{0} x{1}".format(k, v)
            for k, v in sorted(result["by_extracted_basis"].items())))
    print("")
    print(result["verdict"])
    return 0


def cmd_golden(args):
    """Score the CURRENT extractor against planted labels.

    Reads the signals a real cadence tick wrote to a database and compares
    them with the golden sidecars the generator planted, so the loop is
    measure -> change -> measure again with nobody labelling anything in
    between.
    """
    import glob
    import psycopg2
    import psycopg2.extras

    gold_dir = os.path.expanduser(args.golden)
    windows = []
    conn = psycopg2.connect(args.dsn)
    try:
        conn.set_session(readonly=True, autocommit=True)
        for path in sorted(glob.glob(os.path.join(gold_dir, "*.golden.json"))):
            with open(path) as f:
                golden = json.load(f)
            with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT s.signal_id, s.author_ref, s.owner_candidates,
                           array_agg(e.resource_id) AS rids
                    FROM signals s
                    JOIN extraction_batches b USING (batch_id)
                    LEFT JOIN evidence_refs e ON e.signal_id = s.signal_id
                    WHERE b.source_ref = %s
                    GROUP BY s.signal_id, s.author_ref, s.owner_candidates""",
                    (golden["source_ref"],))
                sigs = []
                for r in cur.fetchall():
                    sigs.append({
                        "author_ref": r["author_ref"],
                        "owner_candidates": r["owner_candidates"],
                        "evidence_resource_ids": [x for x in (r["rids"] or [])
                                                  if x]})
            windows.append({"golden": golden, "signals": sigs})
    finally:
        conn.close()

    result = ev.score_golden(windows)
    if args.json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return 0
    print("Attribution against planted labels")
    print("  windows scored:        {0}".format(len(windows)))
    print("  claims planted:        {0}".format(result["claims"]))
    print("  reached extraction:    {0}".format(result["extracted"]))
    print("")
    print("  owner correct:         {0}".format(result["owner_right"]))
    print("  correctly left unowned:{0}".format(result["correctly_unowned"]))
    print("  owner WRONG:           {0}".format(result["owner_wrong"]))
    print("  owner missing:         {0}".format(result["owner_missing"]))
    print("  wrongly attributed:    {0}   (sentence named nobody)".format(
        result["wrongly_owned"]))
    print("")
    for rule in sorted(result["by_rule"]):
        r = result["by_rule"][rule]
        print("  rule {0:8} n={1:3}  right {2:3}  wrong {3:3}  "
              "missing {4:3}".format(rule, r["n"], r["right"], r["wrong"],
                                     r["missing"]))
    print("")
    print("  ATTRIBUTION ACCURACY: {0}%".format(result["accuracy"]))
    if result["misses"] and args.show_misses:
        print("\n  misses:")
        for m in result["misses"][:12]:
            print("    want={0} got={1} :: {2}".format(
                m["want"], m["got"], (m["text"] or "")[:60]))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Attribution eval from real rejections.")
    sub = ap.add_subparsers(dest="command")
    sub.required = True

    b = sub.add_parser("build", help="build a labelling worksheet")
    b.add_argument("--dsn", required=True)
    b.add_argument("--out", required=True,
                   help="worksheet path, OUTSIDE the repository")
    b.add_argument("--inbox", default="~/task-ledger-inbox",
                   help="where the processed windows live")
    b.add_argument("--reasons", nargs="+", default=["wrong_owner"],
                   help="reject reasons to include")

    s = sub.add_parser("score", help="measure a filled-in worksheet")
    s.add_argument("--worksheet", required=True)
    s.add_argument("--json", action="store_true")

    g = sub.add_parser("golden",
                       help="score the current extractor against planted "
                            "labels (no human labelling needed)")
    g.add_argument("--dsn", required=True)
    g.add_argument("--golden", required=True,
                   help="directory of *.golden.json sidecars")
    g.add_argument("--json", action="store_true")
    g.add_argument("--show-misses", action="store_true")

    args = ap.parse_args()
    if args.command == "build":
        return cmd_build(args)
    if args.command == "golden":
        return cmd_golden(args)
    return cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
