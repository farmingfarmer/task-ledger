#!/usr/bin/env python3
"""Convert Copilot meeting-quote blocks into task-ledger inbox windows. v2.

Hardened against what Copilot actually produced (vs what it was asked
for): headers jammed on one line, [--:--] placeholder timestamps,
several utterances per physical line, doubled-dash dates (2026-07--23),
surname-only speakers vs full-name participants, wrapped participant
lists, and meetings whose only content is a System apology.

    python3 scripts/copilot_ingest.py paste.txt --out ~/task-ledger-inbox
    python3 scripts/copilot_ingest.py paste.txt --out ./preview --dry-run

Deterministic ids -> idempotent re-ingestion. Surname and unambiguous
first-name aliases map to the participant's canonical ref, so a
surname-only speaker line and a first-name signal owner both resolve to
the same person. Speakers named System are dropped; blocks left empty by
that are skipped with a note. Python 3.8, stdlib only.
"""

import argparse
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

HEADER_RE = re.compile(
    r"MEETING:\s*(?P<MEETING>.*?)\s*DATE:\s*(?P<DATE>[\d-]+)\s*"
    r"START:\s*(?P<START>\d{1,2}:\d{2})\s*END:\s*(?P<END>\d{1,2}:\d{2})\s*"
    r"PARTICIPANTS:\s*(?P<PARTICIPANTS>.*)$")
TS_TOKEN = re.compile(r"\[[\d\-.:]{1,10}\]")
UTT_RE = re.compile(r"^\s*(?:\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*)?"
                    r"([A-Z][A-Za-z .'\-]{0,60}?):\s*(.+)$")
GLUE_RE = re.compile(r"\"\s+((?:[A-Z][A-Za-z'\-]+\s)*[A-Z][A-Za-z'\-]+):")


def norm_date(d):
    m = re.match(r"^(\d{4})-(\d{1,2})-+(\d{1,2})$", d.strip())
    if not m:
        raise ValueError("unparseable DATE: " + d)
    return "{0}-{1:02d}-{2:02d}".format(m.group(1), int(m.group(2)),
                                        int(m.group(3)))


def ref_for(display):
    return "aad-" + re.sub(r"[^a-z0-9]+", "-",
                           (display or "unknown").lower()).strip("-")


def slug(text, n=40):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:n] or "meeting"


def split_blocks(text):
    """Split the paste on MEETING: markers; normalize each block into
    (headers dict, list of utterance strings)."""
    chunks = re.split(r"(?=MEETING:)", text)
    blocks = []
    for chunk in chunks:
        if not chunk.strip().startswith("MEETING:"):
            continue
        lines = chunk.splitlines()
        # headers may wrap at any point; accumulate lines until all
        # fields are present or the body starts
        header_text = lines[0].strip()
        body_start = 1
        while "PARTICIPANTS:" not in header_text \
                and body_start < len(lines):
            nxt = lines[body_start].strip()
            if TS_TOKEN.search(nxt) or re.search(r':\s*"', nxt):
                break
            header_text += " " + nxt
            body_start += 1
        hdr = {}
        fields = [("MEETING", r"MEETING:\s*(.*?)\s*(?=DATE:|$)"),
                  ("DATE", r"DATE:\s*([\d-]+)"),
                  ("START", r"START:\s*(\d{1,2}:\d{2})"),
                  ("END", r"END:\s*(\d{1,2}:\d{2})"),
                  ("PARTICIPANTS", r"PARTICIPANTS:\s*(.*)$")]
        for key, pat in fields:
            fm = re.search(pat, header_text)
            if not fm:
                raise ValueError("block header missing {0}: {1}".format(
                    key, header_text[:100]))
            hdr[key] = fm.group(1).strip()
        # participant list may wrap onto following bare-name lines
        while body_start < len(lines):
            nxt = lines[body_start].strip()
            if nxt and ":" not in nxt and not TS_TOKEN.search(nxt) \
                    and re.match(r"^[A-Za-z .'\-;]+$", nxt) \
                    and nxt.upper() != "INTERNAL":
                hdr["PARTICIPANTS"] += "; " + nxt
                body_start += 1
            else:
                break
        body = "\n".join(lines[body_start:])
        # every new utterance starts at a timestamp token; make that a
        # newline, then un-glue `..." Speaker:` runs the tokens missed
        body = TS_TOKEN.sub("\n", body)
        body = GLUE_RE.sub('"\n\\1:', body)
        utts = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.upper() == "INTERNAL":
                continue
            um = UTT_RE.match(line)
            if um:
                utts.append(list(um.groups()))
            elif utts:
                utts[-1][2] += " " + line
        blocks.append((hdr, utts))
    return blocks


def build_directory(participants):
    """Full names -> refs, plus surname and unambiguous first-name
    aliases pointing at the same ref."""
    directory = {}
    firsts, lasts = {}, {}
    for name in participants.split(";"):
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            continue
        directory[name] = ref_for(name)
        parts = name.split(" ")
        firsts.setdefault(parts[0], []).append(directory[name])
        lasts.setdefault(parts[-1], []).append(directory[name])
    for alias_map in (firsts, lasts):
        for alias, refs in alias_map.items():
            if len(set(refs)) == 1 and alias not in directory:
                directory[alias] = refs[0]
    return directory


def to_window(hdr, utts):
    date = norm_date(hdr["DATE"])
    directory = build_directory(hdr["PARTICIPANTS"])
    mhash = hashlib.sha256((hdr["MEETING"] + date +
                            hdr["START"]).encode()).hexdigest()[:8]
    items, last_time, skipped_system = [], hdr["START"], 0
    for ts, speaker, text in utts:
        speaker = speaker.strip()
        if speaker.lower() == "system":
            skipped_system += 1
            continue
        text = text.strip().strip('"').strip()
        if not text:
            continue
        if speaker not in directory:
            directory[speaker] = ref_for(speaker)
        t = (ts or last_time)
        last_time = t
        if len(t) == 4:
            t = "0" + t
        items.append({
            "resource_id": "cp{0}-{1:03d}".format(mhash, len(items) + 1),
            "author_name": speaker,
            "author_ref": directory[speaker],
            "timestamp": "{0}T{1}{2}Z".format(
                date, t, ":00" if len(t) == 5 else ""),
            "text": text,
        })
    window = {
        "job": {"source_type": "meeting_transcript",
                "source_ref": "copilot-{0}-{1}".format(
                    date, slug(hdr["MEETING"], 24)),
                "window_start": "{0}T{1}:00Z".format(date, hdr["START"]),
                "window_end": "{0}T{1}:00Z".format(date, hdr["END"])},
        "directory": directory,
        "items": items,
    }
    return window, "copilot-{0}-{1}.json".format(date,
                                                 slug(hdr["MEETING"])), \
        skipped_system


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", default=os.path.expanduser(
        "~/task-ledger-inbox"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-items", type=int, default=DEFAULT_WINDOW_ITEMS,
                    help="split meetings into sub-windows of at most "
                         "this many utterances (0 = never split)")
    args = ap.parse_args()

    blocks = []
    for path in args.files:
        with open(path, encoding="utf-8-sig") as f:
            blocks.extend(split_blocks(f.read()))
    if not blocks:
        print("no MEETING: blocks found")
        return 2

    total = 0
    expanded = []
    for hdr, utts in blocks:
        window, fname, skipped = to_window(hdr, utts)
        items = window["items"]
        if args.max_items and len(items) > args.max_items:
            n = args.max_items
            parts = [items[i:i + n] for i in range(0, len(items), n)]
            for pi, chunk in enumerate(parts, 1):
                sub = {"job": dict(window["job"]),
                       "directory": window["directory"],
                       "items": chunk}
                sub["job"]["source_ref"] += "-p{0}".format(pi)
                sub["job"]["window_start"] = chunk[0]["timestamp"]
                sub["job"]["window_end"] = chunk[-1]["timestamp"]
                expanded.append((sub, fname.replace(
                    ".json", "-p{0}.json".format(pi)), 0))
        else:
            expanded.append((window, fname, skipped))
    for window, fname, skipped in expanded:
        if not window["items"]:
            print("SKIP {0} -- no usable utterances"
                  "{1}".format(fname, " (System-only block)"
                               if skipped else ""))
            continue
        total += len(window["items"])
        speakers = sorted(set(i["author_ref"] for i in window["items"]))
        print("{0}: {1} utterances, {2} speakers".format(
            fname, len(window["items"]), len(speakers)))
        for s in speakers:
            print("      " + s)
        if not args.dry_run:
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, fname), "w") as f:
                json.dump(window, f, indent=1)
    print("total: {0} utterances{1}".format(
        total, " -- DRY RUN, nothing written" if args.dry_run
        else " -> " + args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
