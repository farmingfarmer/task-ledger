#!/usr/bin/env python3
"""Convert a Copilot meeting RECAP into a task-ledger inbox window.

Some tenants will not release meeting transcripts. What Copilot will give
you instead looks like this:

    Platform Workstreams (2026-08-04)
    - Robin Vale
      - Slide generation tooling
      - Auto-drafting decks from project charters
    - Sam Okonjo
      - Evaluating generated output quality

Subjects, grouped under the people who raised them. No utterances, no
timestamps, no commitment language.

    python3 scripts/copilot_recap_ingest.py recap.txt --out ./preview --dry-run
    python3 scripts/copilot_recap_ingest.py recap.txt --out ~/task-ledger-inbox

WHAT THIS IS AND IS NOT. The window it writes carries source_type
'meeting_recap', which is a weaker input than a transcript by design and
labelled so throughout: its evidence is 'meeting_insight' rather than
'transcript_utterance', because a bullet points at a paraphrase of several
things somebody said rather than at a sentence; the extraction prompt is
told these are sightings of work rather than claims about it; and a code
guard in the worker deletes any completion, blocker, deadline or
cancellation the model produces anyway, because a summary cannot witness a
state change. Expect task_mentions with 'inferred' owners, and expect them
to need a human -- which is the same gate everything else goes through.

Prefer a transcript, a chat thread or an email thread when you can get one.
Those carry the words people actually chose, which is what attribution and
provenance both rest on.

Deterministic ids -> re-ingesting the same recap is a no-op. Bullets are
taken verbatim; nothing is merged or reworded. Python 3.8, stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from copilot_ingest import build_directory, ref_for, slug  # noqa: E402

# A date in the header, in any of the shapes seen in practice.
DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-+(\d{1,2})")
# Leading bullet glyphs, markdown emphasis, and numbering all stripped.
BULLET_RE = re.compile(r"^(\s*)(?:[-*•◦▪·]|\d+[.)])\s*")
EMPHASIS_RE = re.compile(r"^\**\s*|\s*\**$")
# A person heading: two to four capitalised words, optionally trailing ':'.
NAME_RE = re.compile(
    r"^([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-.]+){1,3})\s*:?\s*$")


def clean(line):
    """Strip bullet glyph and markdown emphasis; return (indent, text)."""
    m = BULLET_RE.match(line)
    indent = len(m.group(1)) if m else len(line) - len(line.lstrip())
    text = BULLET_RE.sub("", line).strip() if m else line.strip()
    text = EMPHASIS_RE.sub("", text)
    text = re.sub(r"^#+\s*", "", text).strip()
    return indent, text


def parse_recap(text, default_date=None):
    """-> (title, date, [(person, [subjects])]).

    Deliberately forgiving about glyphs and indentation and deliberately
    strict about structure: a subject with no person above it is dropped
    rather than guessed at, because attaching work to the wrong person is
    the failure this whole system exists to avoid.
    """
    title, date = None, default_date
    people, current, orphans = [], None, 0

    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent, line = clean(raw)
        if not line:
            continue
        if title is None:
            title = DATE_RE.sub("", line).strip(" ()-—").strip() or "recap"
            m = DATE_RE.search(line)
            if m:
                date = "{0}-{1:02d}-{2:02d}".format(
                    m.group(1), int(m.group(2)), int(m.group(3)))
            continue
        if date is None:
            m = DATE_RE.search(line)
            if m:
                date = "{0}-{1:02d}-{2:02d}".format(
                    m.group(1), int(m.group(2)), int(m.group(3)))
                continue
        nm = NAME_RE.match(line)
        if nm and (current is None or indent <= current[2]):
            current = [nm.group(1).strip(), [], indent]
            people.append(current)
            continue
        if current is None:
            orphans += 1          # a subject before any name: not guessed at
            continue
        current[1].append(line)
    return title or "recap", date, [(p[0], p[1]) for p in people if p[1]], \
        orphans


def to_window(title, date, people):
    names = "; ".join(name for name, _ in people)
    directory = build_directory(names)
    source_ref = "recap-{0}-{1}".format(date, slug(title, 24))
    mhash = hashlib.sha256(source_ref.encode()).hexdigest()[:8]
    items = []
    for name, subjects in people:
        for subject in subjects:
            items.append({
                "resource_id": "rc{0}-{1:03d}".format(mhash, len(items) + 1),
                "author_name": name,
                "author_ref": directory.get(name, ref_for(name)),
                # A recap records no times. The meeting's own date is the
                # most precise honest answer, and pretending to a clock
                # time would put a fiction into the provenance rail.
                "timestamp": date + "T12:00:00Z",
                "text": subject,
            })
    return {
        "job": {"source_type": "meeting_recap",
                "source_ref": source_ref,
                "window_start": date + "T00:00:00Z",
                "window_end": date + "T23:59:00Z"},
        "directory": directory,
        "items": items,
    }, "recap-{0}-{1}.json".format(date, slug(title))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=os.path.expanduser("~/task-ledger-inbox"))
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD, when the recap has no date of its own")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    written = 0
    for path in args.files:
        with open(path, encoding="utf-8-sig") as f:
            title, date, people, orphans = parse_recap(f.read(), args.date)
        if not people:
            print("{0}: no 'person -> subjects' structure found. This "
                  "parser needs each subject indented under the person who "
                  "raised it.".format(os.path.basename(path)))
            continue
        if not date:
            print("{0}: no date in the recap; pass --date YYYY-MM-DD"
                  .format(os.path.basename(path)))
            continue
        window, fname = to_window(title, date, people)
        print("{0}: \"{1}\" {2} -- {3} people, {4} subject(s){5}".format(
            os.path.basename(path), title, date, len(people),
            len(window["items"]),
            ", {0} subject(s) had no person above them and were "
            "dropped".format(orphans) if orphans else ""))
        for name, subjects in people:
            print("    {0}: {1}".format(name, len(subjects)))
        if args.dry_run:
            print(json.dumps(window, indent=1)[:600] + "\n    ...")
            continue
        out = os.path.expanduser(args.out)
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, fname), "w") as f:
            json.dump(window, f, indent=1)
        written += 1
    if written:
        print("\nwrote {0} window(s) to {1}".format(written, args.out))
        print("these are RECAPS: sightings of work, not claims about it. "
              "Extraction will produce task_mentions with inferred owners, "
              "and the worker deletes any state change the model invents.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
