"""Synthetic meetings with the real cast, for exercising the live pipeline.

The corpus in testdata/ measures extraction against invented people in
invented companies. This generates something different: inbox windows in
the exact format the cadence tick consumes, spoken by the people who
actually attend your meetings, in the register they actually use -- but
about work that does not exist.

Why the cast matters. Attribution is the thing the first live run got
wrong, and attribution runs through the participant directory: surname-only
speakers, first-name aliases, people who never speak in a given window.
Invented names exercise none of that. Real names with invented work
exercise all of it and disclose nothing.

WHERE THE NAMES COME FROM. Not this file. The directory is read at runtime
from a real processed window you point at, so the repository never contains
a colleague's name -- the same rule the eval worksheet obeys. The output
goes to its own inbox and is never committed.

    python3 testdata/synth_meetings.py \\
        --template ~/task-ledger-inbox/processed/<a-real-window>.json \\
        --out ~/task-ledger-synthetic-inbox --meetings 6 --seed 7

Each window ships a golden sidecar naming, for every planted claim, who
should own it and why -- so attribution can be scored automatically
instead of by hand. The planted mix is deliberately the one that broke:
self-commitments (including the elided form), explicit assignments to
someone else, third-party completion claims, requests to the room that
name nobody, blockers, vague deadlines, and pure chatter.

Python 3.8 compatible. Stdlib only.
"""

import argparse
import datetime
import hashlib
import json
import os
import random
import sys

REPO = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))

# Invented work. Deliberately in the same domain register as real standup
# talk, so the extractor faces realistic vocabulary, but none of it names
# anything that exists.
SUBJECTS = [
    "the ingest replay harness", "the staging cutover", "the metrics rollup",
    "the retention sweep", "the schema linter", "the alert routing rules",
    "the sandbox reset script", "the cost report generator",
    "the archive migration", "the load-test profile",
    "the permissions audit", "the changelog builder",
]

# (template, kind, owner_rule) -- owner_rule says who SHOULD own the claim:
#   speaker  the person talking commits themselves
#   target   an explicitly named other person
#   none     the sentence names nobody, so nothing should be attributed
LINES = [
    ("I'll take {subj} this week", "assignment", "speaker"),
    ("I'm going to pick up {subj} after standup", "assignment", "speaker"),
    ("let me own {subj} for now", "assignment", "speaker"),
    # The elided first person a transcript leaves behind.
    ("would also like to start on {subj}", "assignment", "speaker"),
    ("Will start on {subj} tomorrow", "assignment", "speaker"),
    ("{target}, can you take {subj}?", "assignment", "target"),
    ("I think {target} should own {subj}", "assignment", "target"),
    ("{target} is already deep in {subj}, so it makes sense there",
     "assignment", "target"),
    ("can we prioritise {subj} over the rest?", "proposal", "none"),
    ("let's get {subj} scoped before the end of the month",
     "proposal", "none"),
    ("someone should look at {subj} at some point", "proposal", "none"),
    ("I finished {subj} yesterday", "completion_claim", "speaker"),
    ("{subj} is done as far as I'm concerned", "completion_claim", "speaker"),
    ("I think {target} finished {subj} last week",
     "completion_claim", "target"),
    ("I'm blocked on {subj} until the access request clears",
     "blocker", "speaker"),
    ("{subj} is stuck waiting on the platform team", "blocker", "none"),
    ("I'll have {subj} ready by end of next sprint",
     "deadline_signal", "speaker"),
    ("{subj} needs to land before the audit", "deadline_signal", "none"),
    # Traps: reversals and chatter that must produce nothing.
    ("we are NOT doing {subj} this quarter", "cancellation", "none"),
    ("{subj} is no longer blocked, that cleared on Friday",
     "blocker_resolved", "none"),
    ("good work on the demo yesterday, that landed well", "chatter", "none"),
    ("has anyone got a lunch recommendation near the office",
     "chatter", "none"),
    ("sorry, I dropped off for a second, could you repeat that",
     "chatter", "none"),
]


def load_cast(template_path):
    """People and format, from a real window. Nothing else is copied: not
    a subject, not a sentence, not a task."""
    with open(template_path) as f:
        spec = json.load(f)
    directory = spec.get("directory") or {}
    speakers = {}
    for item in spec.get("items") or []:
        ref = item.get("author_ref")
        if ref and ref not in speakers:
            speakers[ref] = item.get("author_name") or ref
    if not speakers:                       # nobody spoke; fall back to all
        for name, ref in directory.items():
            speakers.setdefault(ref, name)
    source_type = (spec.get("job") or {}).get("source_type",
                                              "meeting_transcript")
    return directory, speakers, source_type


def assert_outside_repo(path):
    full = os.path.abspath(os.path.expanduser(path))
    if full == REPO or full.startswith(REPO + os.sep):
        raise SystemExit(
            "refusing to write generated windows inside the repository "
            "({0}): they carry the real participant directory. Choose a "
            "path outside it, e.g. ~/task-ledger-synthetic-inbox".format(full))
    return full


def build_meeting(rng, cast, source_type, day, index, turns, seed):
    directory, speakers, _ = cast
    refs = sorted(speakers)
    start = datetime.datetime.combine(day, datetime.time(10, 0))
    # The seed belongs in the id. Two runs on the same day with different
    # seeds produce entirely different conversations, and without this they
    # produced identical source_refs -- which means identical batch_ids,
    # because batch_id hashes (source_ref, window_start, window_end,
    # prompt_version) and none of those see the content. Ingesting a tuning
    # set and a held-out set into one database would have silently dropped
    # the second on ON CONFLICT DO NOTHING and scored the first twice.
    source_ref = "synth-s{0}-{1}-standup-{2}".format(
        seed, day.isoformat(), index)
    mhash = hashlib.sha256(source_ref.encode()).hexdigest()[:8]

    items, golden = [], []
    for n in range(turns):
        speaker = rng.choice(refs)
        text, kind, rule = rng.choice(LINES)
        target = rng.choice([r for r in refs if r != speaker])
        line = text.format(subj=rng.choice(SUBJECTS),
                           target=speakers[target].split()[0])
        rid = "sy{0}-{1:03d}".format(mhash, n + 1)
        items.append({
            "resource_id": rid,
            "author_name": speakers[speaker],
            "author_ref": speaker,
            "timestamp": (start + datetime.timedelta(
                minutes=n * 2)).isoformat() + "Z",
            "text": line,
        })
        if kind != "chatter":
            golden.append({
                "resource_id": rid, "kind": kind, "speaker_ref": speaker,
                "expected_owner_ref": (speaker if rule == "speaker"
                                       else target if rule == "target"
                                       else None),
                "owner_rule": rule, "text": line,
            })

    window = {
        "job": {"source_type": source_type, "source_ref": source_ref,
                "window_start": start.isoformat() + "Z",
                "window_end": (start + datetime.timedelta(
                    minutes=turns * 2)).isoformat() + "Z"},
        "directory": directory,
        "items": items,
    }
    return source_ref, window, {"source_ref": source_ref, "claims": golden}


def main():
    ap = argparse.ArgumentParser(
        description="Synthetic meetings with the real cast.")
    ap.add_argument("--template", required=True,
                    help="a real processed window; supplies the directory "
                         "and format, nothing else")
    ap.add_argument("--out", required=True,
                    help="inbox directory OUTSIDE the repository")
    ap.add_argument("--meetings", type=int, default=6)
    ap.add_argument("--turns", type=int, default=14)
    ap.add_argument("--days", type=int, default=21,
                    help="spread the meetings over this many past days")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    out = assert_outside_repo(args.out)
    cast = load_cast(os.path.expanduser(args.template))
    directory, speakers, source_type = cast
    rng = random.Random(args.seed)

    os.makedirs(out, exist_ok=True)
    gold_dir = os.path.join(out, "golden")
    os.makedirs(gold_dir, exist_ok=True)

    today = datetime.date.today()
    written = 0
    for i in range(args.meetings):
        day = today - datetime.timedelta(
            days=int(args.days * (args.meetings - 1 - i) /
                     max(args.meetings - 1, 1)))
        ref, window, golden = build_meeting(rng, cast, source_type, day, i,
                                            args.turns, args.seed)
        with open(os.path.join(out, ref + ".json"), "w") as f:
            json.dump(window, f, indent=1)
        with open(os.path.join(gold_dir, ref + ".golden.json"), "w") as f:
            json.dump(golden, f, indent=1)
        written += 1

    sys.stderr.write(
        "wrote {0} window(s) to {1}\n  cast: {2} speaker(s) from the "
        "template's directory ({3} entries)\n  golden sidecars in {4}\n"
        "  next: point TL_INBOX at this directory and run a cadence "
        "tick\n".format(written, out, len(speakers), len(directory),
                        gold_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
