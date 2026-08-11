#!/usr/bin/env python3
"""Synthetic Teams transcripts, in the format Word actually hands over.

    python3 testdata/synth_transcripts.py --out ~/task-ledger-demo
    python3 testdata/synth_transcripts.py --out ./demo --ingest

WHY THIS EXISTS, given testdata/synth_meetings.py already makes synthetic
meetings. That one writes inbox windows -- it starts AFTER ingestion, so it
exercises nothing about reading a transcript. This writes the .txt a Teams
transcript becomes when it is saved out of Word, so the demo runs the whole
chain the real file runs: parse, ingest, extract, reconcile, review.

THE ARC IS SCRIPTED, NOT RANDOM. A demo needs the same story every time,
and the story is the point: three meetings across nine days, because a
board has to exist before anything can be completed or blocked on it. From
one meeting you can only ever show work being proposed. The lifecycle --
assigned, accepted, blocked, unblocked, finished -- needs meetings that
refer back to tasks earlier meetings created.

WHAT EACH MEETING IS FOR:
  day 0  assignments, including one to a person who never speaks, and one
         impersonal sentence that must produce NO owner
  day 4  progress against work that now exists: a blocker, a status update,
         a third-party completion claim that SHOULD hold on one source, and
         a negation that must not be read as a completion
  day 9  closure: the third-party claim corroborated by the person herself,
         a blocker resolved, a completion seconded by a second speaker

Every planted claim carries a golden label -- expected type, owner and
attribution basis -- written in the shape testdata/score_extraction.py
already consumes, so attribution can be MEASURED rather than asserted:

    python3 testdata/score_extraction.py --corpus <out> --model mistral-small3.1

The cast is invented. Nobody in this file exists, which is what makes it
committable; testdata/synth_meetings.py covers the other case, where the
real cast matters and therefore never enters the repository.

Python 3.8 compatible, stdlib only.
"""

import argparse
import datetime
import io
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
# Imported rather than reimplemented: these decide the window filenames and
# person refs this file has to predict, and a private copy would drift.
from transcript_ingest import slug, ref_for, canonical   # noqa: E402

SERIES = "Platform Guild Scrums"

# display -> how the transcript labels them ("Last, First")
CAST = {
    "Ada Okonkwo": "Okonkwo, Ada",
    "Rowan Vasquez": "Vasquez, Rowan",
    "Piper Nakamura": "Nakamura, Piper",
    "Marisol Reyes": "Reyes, Marisol",
}

# (title, date, start, end, [(speaker, offset_seconds, text)])
# offsets are seconds from the meeting's start.
MEETINGS = [
    (SERIES, "2026-08-03", "11:45 AM", "12:00 PM", [
        ("Ada Okonkwo", 20, "Hey, good morning everyone. Let's do a quick "
                            "round and keep it short."),
        ("Rowan Vasquez", 48, "Morning. I'm picking up the retry harness "
                              "rebuild this week, I'll have it in staging by "
                              "Thursday."),
        ("Ada Okonkwo", 92, "Good. Piper, can you take the archive migration? "
                            "It needs to land before the permissions audit "
                            "starts."),
        ("Piper Nakamura", 118, "Yeah, I can pick up the archive migration. "
                                "I'll start on it this afternoon."),
        ("Ada Okonkwo", 154, "And Marisol is going to own the schema linter "
                             "rollout, I spoke to her yesterday."),
        ("Ada Okonkwo", 190, "The intake form still needs scoping before "
                             "anyone can start on it."),
        ("Rowan Vasquez", 224, "Sounds good. Anything else? No? Thanks all."),
    ]),
    (SERIES, "2026-08-07", "11:45 AM", "12:00 PM", [
        ("Piper Nakamura", 25, "Morning. The archive migration is stuck -- "
                               "I'm blocked on the storage team for the "
                               "bucket permissions."),
        ("Ada Okonkwo", 70, "How long has that been sitting?"),
        ("Piper Nakamura", 88, "Since Tuesday. I've pinged them twice."),
        ("Rowan Vasquez", 130, "Retry harness rebuild is in staging now, "
                               "still watching it before I call it done."),
        ("Ada Okonkwo", 176, "I think Marisol got the schema linter rollout "
                             "finished last week."),
        ("Ada Okonkwo", 210, "We did not ship the prompt gallery cleanup, "
                             "just to be clear -- that slipped again."),
        ("Rowan Vasquez", 250, "Understood."),
    ]),
    (SERIES, "2026-08-12", "11:45 AM", "12:00 PM", [
        ("Marisol Reyes", 30, "Morning all. I finished the schema linter "
                              "rollout, it went out on Friday."),
        ("Ada Okonkwo", 74, "Great, that matches what I saw."),
        # First person on purpose. An impersonal closure -- "the migration
        # is unblocked", "the harness is in production" -- names nobody, and
        # the extractor is right to return no owner for it. Scoring this set
        # against goldens that expected self_reference was how that got
        # noticed: the labels were wrong, not the model.
        ("Piper Nakamura", 112, "Storage sorted the bucket permissions, so "
                                "I'm unblocked on the archive migration and "
                                "moving again."),
        ("Rowan Vasquez", 158, "I've got the retry harness rebuild in "
                               "production now, so that one is done from my "
                               "side."),
        ("Ada Okonkwo", 192, "Yes, I saw the retry harness go out on "
                             "Tuesday. Nice work."),
        ("Ada Okonkwo", 226, "That's us. Thanks everybody."),
    ]),
    # The final meeting is never decided by the demo builder: its proposals
    # are the review queue. So it is all NEW work -- a queue of completions
    # would mean a board that has already moved, which is the opposite of
    # something still waiting on a person.
    (SERIES, "2026-08-17", "11:45 AM", "12:00 PM", [
        ("Ada Okonkwo", 18, "Morning. A few new things off the back of the "
                            "audit."),
        ("Ada Okonkwo", 55, "Rowan, can you take the retention sweep? The "
                            "auditors want it before the end of the month."),
        ("Rowan Vasquez", 96, "Sure, I'll pick up the retention sweep."),
        ("Ada Okonkwo", 138, "Piper is going to take the sandbox reset "
                             "script, she offered yesterday."),
        ("Marisol Reyes", 180, "I'll start on the changelog builder this "
                               "week."),
        ("Ada Okonkwo", 224, "Good. That's everything."),
    ]),
]

# Golden labels, indexed (meeting index, utterance index) so the evidence
# hint can be computed from the same ids the ingester will mint.
GOLDEN = {
    (0, 1): {"type": "assignment", "title_core": "retry harness rebuild",
             "owner_ref": "aad-rowan-vasquez", "basis": "self_reference"},
    (0, 2): {"type": "assignment", "title_core": "archive migration",
             "owner_ref": "aad-piper-nakamura",
             "basis": "explicit_assignment"},
    (0, 3): {"type": "assignment", "title_core": "archive migration",
             "owner_ref": "aad-piper-nakamura", "basis": "self_reference"},
    # The case the whole directory question turns on: assigned to somebody
    # who does not speak in this meeting at all.
    (0, 4): {"type": "assignment", "title_core": "schema linter rollout",
             "owner_ref": "aad-marisol-reyes", "basis": "inferred"},
    (1, 0): {"type": "blocker", "title_core": "archive migration",
             "owner_ref": "aad-piper-nakamura", "basis": "self_reference"},
    (1, 3): {"type": "status_update", "title_core": "retry harness rebuild",
             "owner_ref": "aad-rowan-vasquez", "basis": "self_reference"},
    (1, 4): {"type": "completion_claim", "title_core": "schema linter rollout",
             "owner_ref": "aad-marisol-reyes", "basis": "inferred",
             "third_party": True},
    (2, 0): {"type": "completion_claim", "title_core": "schema linter rollout",
             "owner_ref": "aad-marisol-reyes", "basis": "self_reference"},
    (2, 2): {"type": "blocker_resolved", "title_core": "archive migration",
             "owner_ref": "aad-piper-nakamura", "basis": "self_reference"},
    (2, 3): {"type": "completion_claim", "title_core": "retry harness rebuild",
             "owner_ref": "aad-rowan-vasquez", "basis": "self_reference"},
    (3, 1): {"type": "assignment", "title_core": "retention sweep",
             "owner_ref": "aad-rowan-vasquez",
             "basis": "explicit_assignment"},
    (3, 2): {"type": "assignment", "title_core": "retention sweep",
             "owner_ref": "aad-rowan-vasquez", "basis": "self_reference"},
    # Assigned to somebody who is present but silent on this one: the owner
    # is the person named, not the person reporting it.
    (3, 3): {"type": "assignment", "title_core": "sandbox reset script",
             "owner_ref": "aad-piper-nakamura", "basis": "inferred"},
    (3, 4): {"type": "assignment", "title_core": "changelog builder",
             "owner_ref": "aad-marisol-reyes", "basis": "self_reference"},
}

# Sentences that must not be read as more than they say. `acceptable` lists
# the readings that ARE fair -- noticing the work is fine, asserting a state
# change is the trap. Scored apart from recall: reading one of these is a
# fabrication, not a miss.
TRAPS = {
    (0, 5): {"trap": "assignment_from_impersonal",
             "title_core": "intake form scoping",
             "acceptable": ["proposal", "task_mention", "status_update"],
             "why": "names nobody, so an owner may be noticed but never "
                    "invented"},
    (1, 5): {"trap": "completion_from_negation",
             "title_core": "prompt gallery cleanup",
             "acceptable": ["status_update", "task_mention", "proposal"],
             "why": "'we did not ship' is a slip, not a completion"},
}


def long_offset(sec):
    """'0 minutes 20 seconds', '1 hour 2 minutes 3 seconds' -- and the
    singular forms, because Word writes '1 minute 1 second'."""
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append("{0} hour{1}".format(h, "" if h == 1 else "s"))
    parts.append("{0} minute{1}".format(m, "" if m == 1 else "s"))
    parts.append("{0} second{1}".format(s, "" if s == 1 else "s"))
    return " ".join(parts)


def short_offset(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return "{0}:{1:02d}:{2:02d}".format(h, m, s)
    return "{0}:{1:02d}".format(m, s)


def render(meetings, wrap_at=78):
    """The transcript as Word leaves it, artefacts and all."""
    out = []
    for mi, (title, date, start, end, utts) in enumerate(meetings):
        d = datetime.datetime.strptime(date, "%Y-%m-%d")
        out.append(title)
        out.append("{0}, {1} {2}, {3} {4} - {5}".format(
            d.strftime("%A"), d.strftime("%B"), d.day, d.year, start, end))
        # The second meeting carries the navigation blurb and the dropped
        # image glyphs, so the parser meets them at least once.
        if mi == 1:
            out.append("")
            out.append("Transcript. Use arrow keys to navigate between "
                       "transcript entries. Select an entry to")
            out.append("navigate the media to the time of the entry.")
            out.append("")
            out.extend(["?", "?", "?"])
        out.append("AI-generated content may be incorrect")
        out.append("")

        last = None
        for ui, (who, sec, text) in enumerate(utts):
            label = CAST[who]
            if label != last:
                # A new block: the name alone, then the doubled offset line.
                out.append(label)
                out.append(long_offset(sec) + short_offset(sec))
                last = label
            out.append("{0} {1}".format(label, long_offset(sec)))
            for line in wrapped(text, wrap_at):
                out.append(line)
            # The avatar initials Teams sometimes leaves behind.
            if (mi, ui) == (0, 0):
                out.append("MA")
            out.append("")
    return "\n".join(out) + "\n"


def wrapped(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


def window_name(title, date):
    """Exactly what transcript_ingest will call this window."""
    return "teams-{0}-{1}.json".format(date, slug(title))


def resource_ids(meetings):
    """Predict the ingester's ids, so a golden label can point at one.

    The ingester numbers utterances per meeting from 1 and hashes the
    meeting identity into the prefix; both are reproduced here rather than
    guessed, and window_name/ref_for come from the ingester itself.
    """
    import hashlib
    out = {}
    for mi, (title, date, _s, _e, utts) in enumerate(meetings):
        mhash = hashlib.sha256(
            ("teams|" + date + "|" + title).encode("utf-8")).hexdigest()[:8]
        for ui in range(len(utts)):
            out[(mi, ui)] = "tx{0}-{1:03d}".format(mhash, ui + 1)
    return out


def build_golden(meetings):
    ids = resource_ids(meetings)
    golden = {}
    for mi, (title, date, _s, _e, _u) in enumerate(meetings):
        golden[window_name(title, date)] = {"signals": [], "traps": []}
    for (mi, ui), label in sorted(GOLDEN.items()):
        title, date = meetings[mi][0], meetings[mi][1]
        entry = dict(label)
        entry["evidence_hint"] = ids[(mi, ui)]
        golden[window_name(title, date)]["signals"].append(entry)
    for (mi, ui), trap in sorted(TRAPS.items()):
        title, date = meetings[mi][0], meetings[mi][1]
        entry = dict(trap)
        entry["evidence_hint"] = ids[(mi, ui)]
        golden[window_name(title, date)]["traps"].append(entry)
    return golden


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default="demo_transcripts")
    ap.add_argument("--ingest", action="store_true",
                    help="also run transcript_ingest into --out, leaving a "
                         "corpus score_extraction.py can read directly")
    args = ap.parse_args()

    try:
        os.makedirs(args.out)
    except OSError:
        pass

    txt_path = os.path.join(args.out, "transcripts.txt")
    with io.open(txt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(MEETINGS))

    golden = build_golden(MEETINGS)
    with io.open(os.path.join(args.out, "golden.json"), "w",
                 encoding="utf-8") as f:
        f.write(json.dumps(golden, indent=1, sort_keys=True))
        f.write("\n")

    people = sorted(set(ref_for(canonical(n)) for n in CAST))
    print("{0}  ({1} meetings, {2} utterances, {3} people)".format(
        txt_path, len(MEETINGS), sum(len(m[4]) for m in MEETINGS),
        len(people)))
    print("golden.json  ({0} labelled claim(s), {1} trap(s))".format(
        sum(len(g["signals"]) for g in golden.values()),
        sum(len(g["traps"]) for g in golden.values())))
    for ref in people:
        print("  " + ref)

    if args.ingest:
        import transcript_ingest as ti
        argv = sys.argv
        sys.argv = ["transcript_ingest", txt_path, "--out", args.out]
        try:
            ti.main()
        finally:
            sys.argv = argv
    else:
        print("\nnext:")
        print("  python3 scripts/transcript_ingest.py {0} --out {1}".format(
            txt_path, args.out))
        print("  python3 testdata/score_extraction.py --corpus {0} "
              "--model mistral-small3.1".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
