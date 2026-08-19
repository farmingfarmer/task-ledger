#!/usr/bin/env python3
"""Convert Teams transcripts exported through Word into inbox windows.

    python3 scripts/transcript_ingest.py transcripts_raw.txt --dry-run
    python3 scripts/transcript_ingest.py transcripts_raw.txt --out ~/task-ledger-inbox

WHY A SECOND INGESTER. copilot_ingest.py reads what Copilot was ASKED to
produce -- 'MEETING:/DATE:/PARTICIPANTS:' headers and '[HH:MM] Name: text'
lines. A Teams transcript saved out of Word is a different artifact
entirely: no colons, no participant list, speaker and offset repeated on
their own lines, and the offset given twice in two notations. Bending the
Copilot regexes to cover both would make neither legible.

THE SHAPE THIS PARSES. One meeting looks like:

    <meeting title>
    Weekday, Month D, YYYY H:MM AM - H:MM PM
    AI-generated content may be incorrect

    Last, First
    0 minutes 20 seconds0:20
    Last, First 0 minutes 20 seconds
    the utterance text, sometimes wrapped over several lines
    Last, First 1 minute 4 seconds
    the same speaker's next utterance

The line carrying BOTH a name and a long-form offset is the only reliable
delimiter -- it opens the first utterance of a block and every continuation
inside it -- so that is what this splits on. Everything else in a block is
either the text, a restatement of what the delimiter already said, or
export furniture. Several meetings may sit in one file; each date header
starts a new one.

WHAT IS DISCARDED, and why none of it is content: the bare speaker line
and the doubled offset line ('0 minutes 20 seconds0:20') repeat the
delimiter; 'AI-generated content may be incorrect' and the arrow-key
navigation blurb are Word furniture; '[?]' glyphs are dropped images;
one-to-three-letter lines are the initials Teams renders beside an avatar;
'started transcription' is an event, not speech. --show-unparsed prints
anything this could not place, so a format drift surfaces as a number
rather than as silently missing speech.

IDENTITY, which decides whether any of this is useful. 'Last, First'
becomes 'First Last' becomes aad-first-last, the ref convention already in
the ledger. But the owners worth catching are mostly people who did NOT
speak -- the one handed the work, the one somebody else reported on -- and
the worker drops any owner candidate whose name it cannot resolve. So the
directory is deliberately not the speaker list: it is everyone in the file,
carried into every window, plus unambiguous first-name and surname aliases,
because a room says a first name where the transcript prints 'Last, First'.

--directory takes a JSON object of display name -> ref and does two jobs:
it pins someone ALREADY on the board under a different ref, so the same
human does not arrive twice and fork the board, and it ADDS people who
never speak in these meetings but are handed work in them. That file names
real people: keep it out of this repository.

Timestamps follow copilot_ingest: the meeting's own clock, stamped Z. It
is a local wall clock either way; nothing downstream compares across
meetings, and changing one ingester's convention alone would be worse.

Deterministic ids -> re-ingesting a file is an upsert-shaped no-op.
Python 3.8 compatible, stdlib only.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

# One window is one model call, and the size changes what the model
# returns -- see extraction/windowing.py. Imported rather than repeated:
# this default disagreeing with quickscan's is what made a preview
# contradict the pipeline it previews.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "extraction"))
from windowing import DEFAULT_WINDOW_ITEMS          # noqa: E402

MONTHS = ("January February March April May June July August September "
          "October November December").split()

DATE_RE = re.compile(
    r"^\s*(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s*"
    r"(?P<month>" + "|".join(MONTHS) + r")\s+(?P<day>\d{1,2}),\s*"
    r"(?P<year>\d{4})\s+"
    r"(?P<start>\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\s*[-–—]\s*"
    r"(?P<end>\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\s*$")

# "0 minutes 48 seconds", "1 minute 1 second", "1 hour 2 minutes 3 seconds"
OFFSET = (r"(?:(?P<h>\d+)\s+hours?\s+)?(?P<m>\d+)\s+minutes?\s+"
          r"(?P<s>\d+)\s+seconds?")
# The delimiter: a name AND an offset on one line.
SPEAKER_AT = re.compile(r"^\s*(?P<name>\S.*?)\s+" + OFFSET + r"\s*$")
# The doubled offset line on its own: "0 minutes 48 seconds0:48"
BARE_OFFSET = re.compile(r"^\s*" + OFFSET + r"\s*(?:\d{1,2}:\d{2}(?::\d{2})?)?\s*$")
INITIALS = re.compile(r"^\s*[A-Z]{1,3}\s*$")
GLYPH = re.compile(r"^[\s☐❑?�\[\]()]*$")

FURNITURE = (
    "ai-generated content may be incorrect",
    "transcript. use arrow keys to navigate between transcript entries.",
    # Word wraps the navigation blurb, so its tail arrives as its own line.
    "navigate the media to the time of the entry",
    "started transcription",
    "stopped transcription",
)


def is_furniture(line):
    low = line.strip().lower()
    if not low:
        return True
    for f in FURNITURE:
        if low.startswith(f) or f.startswith(low) and len(low) > 12:
            return True
    return False


def canonical(display):
    """'Last, First M' -> 'First M Last'. Anything else is left alone."""
    d = " ".join(display.split())
    if "," in d:
        last, _, first = d.partition(",")
        first, last = first.strip(), last.strip()
        if first and last:
            return "{0} {1}".format(first, last)
    return d


def ref_for(display):
    return "aad-" + re.sub(r"[^a-z0-9]+", "-",
                           (display or "unknown").lower()).strip("-")


def with_aliases(roster):
    """Add surname and first-name keys pointing at the same ref.

    People are named in half a dozen forms and almost never the full one:
    the transcript labels a speaker 'Last, First' while the room calls them
    by a first name. An owner whose name does not resolve is dropped by the
    worker, so an alias missing here is an owner silently lost. Ambiguous
    aliases -- two people sharing a first name -- are left out rather than
    guessed, which is the same trade the Copilot ingester makes.
    """
    out = dict(roster)
    firsts, lasts = {}, {}
    for name, ref in roster.items():
        parts = name.split()
        if not parts:
            continue
        firsts.setdefault(parts[0], set()).add(ref)
        lasts.setdefault(parts[-1], set()).add(ref)
    for alias_map in (firsts, lasts):
        for alias, refs in alias_map.items():
            if len(refs) == 1 and alias not in out:
                out[alias] = sorted(refs)[0]
    return out


def slug(text, n=40):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "meeting").lower()).strip("-")
    return (s[:n] or "meeting").strip("-")


def parse_clock(day, clock):
    """'August 3, 2026' + '11:45 AM' -> datetime."""
    c = clock.replace(".", "").upper().replace(" ", "")
    return datetime.datetime.strptime(day + " " + c, "%B %d, %Y %I:%M%p")


def looks_like_name(text):
    """A delimiter's name half must not itself be prose.

    Guards the one ambiguity in the format: a sentence ending in a
    duration ('... it ran for 3 minutes 20 seconds') would otherwise open
    a new utterance attributed to the sentence.
    """
    t = text.strip().rstrip(":")
    if not t or len(t) > 60:
        return False
    if t[0].islower():
        return False
    if re.search(r"[.!?,;]$", t):
        return False
    return len(t.split()) <= 6


def split_meetings(lines):
    """[(title, date_match, [body lines])] -- a date header opens a meeting.

    The title sits ABOVE its date line, so a meeting's body has to stop
    short of the next meeting's title -- otherwise that title lands in the
    last speaker's mouth as a trailing sentence.
    """
    heads = []
    for i, ln in enumerate(lines):
        m = DATE_RE.match(ln)
        if not m:
            continue
        t_idx, title = i, ""
        for back in range(i - 1, max(-1, i - 6), -1):
            cand = lines[back].strip()
            if cand and not is_furniture(cand) and not GLYPH.match(cand):
                t_idx, title = back, cand
                break
        heads.append((i, t_idx, title, m))

    out = []
    for k, head in enumerate(heads):
        d_idx, _, title, m = head
        stop = heads[k + 1][1] if k + 1 < len(heads) else len(lines)
        out.append((title, m, lines[d_idx + 1:stop]))
    return out


def parse_meeting(title, dm, body, unparsed):
    day = "{0} {1}, {2}".format(dm.group("month"), dm.group("day"),
                                dm.group("year"))
    started = parse_clock(day, dm.group("start"))
    ended = parse_clock(day, dm.group("end"))
    date = started.strftime("%Y-%m-%d")

    utts, cur = [], None

    def flush():
        if cur and cur["text"]:
            text = " ".join(" ".join(cur["text"]).split())
            if text:
                utts.append({"name": cur["name"], "off": cur["off"],
                             "text": text})

    j = 0
    while j < len(body):
        line = body[j].strip()
        j += 1
        if not line or is_furniture(line) or GLYPH.match(line) \
                or INITIALS.match(line) or BARE_OFFSET.match(line):
            continue
        m = SPEAKER_AT.match(line)
        if m and looks_like_name(m.group("name")):
            flush()
            off = (int(m.group("h") or 0) * 3600
                   + int(m.group("m")) * 60 + int(m.group("s")))
            cur = {"name": canonical(m.group("name")), "off": off, "text": []}
            continue
        # A block opens with the speaker's name ALONE, and the next thing
        # is always its offset. Without that lookahead the name reads as
        # the tail of whatever the previous speaker said.
        if looks_like_name(line):
            nxt = None
            for k in range(j, min(len(body), j + 3)):
                cand = body[k].strip()
                if cand and not GLYPH.match(cand) and not is_furniture(cand):
                    nxt = cand
                    break
            if nxt is not None and (BARE_OFFSET.match(nxt)
                                    or SPEAKER_AT.match(nxt)):
                continue
        if cur is None:
            # Before the first delimiter there is nothing to attach to.
            if not looks_like_name(line):
                unparsed.append(line)
            continue
        if canonical(line) == cur["name"]:
            continue
        cur["text"].append(line)
    flush()

    if not utts:
        return None

    mhash = hashlib.sha256(
        ("teams|" + date + "|" + title).encode("utf-8")).hexdigest()[:8]
    directory, items = {}, []
    for n, u in enumerate(utts, 1):
        directory.setdefault(u["name"], ref_for(u["name"]))
        at = started + datetime.timedelta(seconds=u["off"])
        items.append({
            "resource_id": "tx{0}-{1:03d}".format(mhash, n),
            "author_name": u["name"],
            "author_ref": directory[u["name"]],
            "timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "text": u["text"]})

    window = {
        "job": {"source_type": "meeting_transcript",
                "source_ref": "teams-{0}-{1}".format(date, slug(title, 24)),
                "window_start": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "window_end": ended.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "directory": directory,
        "items": items}
    return window, "teams-{0}-{1}.json".format(date, slug(title))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=os.path.expanduser(
        "~/task-ledger-inbox"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--directory", help="JSON object: display name -> ref. "
                                        "Pins people already on the board "
                                        "under another ref, and adds people "
                                        "who never speak but are given work")
    ap.add_argument("--max-items", type=int, default=DEFAULT_WINDOW_ITEMS,
                    help="split a meeting into sub-windows of at most this "
                         "many utterances (0 = never split)")
    ap.add_argument("--skip-processed", action="store_true",
                    help="do not re-write windows already in "
                         "<out>/processed/ -- append new meetings to a "
                         "transcript and ingest only what is new")
    ap.add_argument("--show-unparsed", action="store_true",
                    help="print lines this could not place; they are real "
                         "speech, so this prints to the terminal only")
    args = ap.parse_args()

    override = {}
    if args.directory:
        with open(args.directory, encoding="utf-8") as f:
            override = json.load(f)

    lines, unparsed = [], []
    for path in args.files:
        with open(path, encoding="utf-8-sig") as f:
            lines.extend(f.read().replace("\r\n", "\n").split("\n"))

    meetings = split_meetings(lines)
    if not meetings:
        print("no meeting date header found -- expected a line like "
              "'Monday, August 3, 2026 11:45 AM - 12:00 PM'")
        return 2

    built = []
    for title, dm, body in meetings:
        parsed = parse_meeting(title, dm, body, unparsed)
        if not parsed:
            print("SKIP {0} {1} -- no utterances".format(
                dm.group("month"), dm.group("day")))
            continue
        built.append(parsed)

    # The directory is what an owner's NAME is resolved through, and the
    # owners worth catching are mostly people who did not speak -- the one
    # handed the work, the one somebody reported on. A directory built from
    # speakers alone silently drops exactly those. So: everyone in the file,
    # in every window, plus --directory for people who appear in neither.
    roster = {}
    for window, _ in built:
        for name in window["directory"]:
            roster.setdefault(name, ref_for(name))
    for name, ref in override.items():
        roster[canonical(name)] = ref
    resolved = with_aliases(roster)

    for window, _ in built:
        window["directory"] = dict(resolved)
        for it in window["items"]:
            it["author_ref"] = roster[it["author_name"]]

    expanded = []
    for window, fname in built:
        items = window["items"]
        if args.max_items and len(items) > args.max_items:
            for pi in range(0, (len(items) + args.max_items - 1)
                            // args.max_items):
                chunk = items[pi * args.max_items:(pi + 1) * args.max_items]
                sub = {"job": dict(window["job"]),
                       "directory": window["directory"], "items": chunk}
                sub["job"]["source_ref"] += "-p{0}".format(pi + 1)
                sub["job"]["window_start"] = chunk[0]["timestamp"]
                sub["job"]["window_end"] = chunk[-1]["timestamp"]
                expanded.append((sub, fname.replace(
                    ".json", "-p{0}.json".format(pi + 1))))
        else:
            expanded.append((window, fname))

    if not args.dry_run:
        try:
            os.makedirs(args.out)
        except OSError:
            pass

    # Re-ingesting a whole transcript to pick up one appended meeting is
    # not wrong -- content-hash ids make the old windows a no-op at the
    # store -- but it is not free either: every window still goes through
    # the model first, and 65 of them against a local 8B is most of an
    # hour to learn nothing. So a window already sitting in processed/ can
    # be left alone. Counted and reported, never silently dropped: "I
    # ingested the file" and "I ingested part of the file" must not look
    # the same on the way past.
    processed_dir = os.path.join(args.out, "processed")
    total = skipped = 0
    for window, fname in expanded:
        if args.skip_processed and os.path.exists(
                os.path.join(processed_dir, fname)):
            skipped += 1
            continue
        total += len(window["items"])
        refs = sorted(set(i["author_ref"] for i in window["items"]))
        print("{0}{1}  {2} utterance(s), {3} speaker(s): {4}".format(
            "[dry-run] " if args.dry_run else "", fname,
            len(window["items"]), len(refs), ", ".join(refs)))
        if not args.dry_run:
            with open(os.path.join(args.out, fname), "w",
                      encoding="utf-8") as f:
                json.dump(window, f, indent=2)
                f.write("\n")

    print("{0} window(s), {1} utterance(s){2}{3}".format(
        len(expanded) - skipped, total,
        "" if args.dry_run else " -> " + args.out,
        "" if not skipped else
        "  ({0} already processed, skipped)".format(skipped)))
    if unparsed:
        print("{0} line(s) could not be placed"
              "{1}".format(len(unparsed),
                           "" if args.show_unparsed
                           else " (--show-unparsed to see them)"))
        if args.show_unparsed:
            for u in unparsed:
                print("  ? " + u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
