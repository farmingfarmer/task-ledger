#!/usr/bin/env python3
"""Break one inbox window into smaller ones, for a window the model cannot finish.

    python3 scripts/split_window.py ~/task-ledger-inbox/big-window.json
    python3 scripts/split_window.py <file> --max-items 6 --dry-run

WHY. Extraction is one model call per window, and attention cost climbs
faster than linearly with context -- so a long window is not slightly more
expensive than a short one, it is disproportionately more expensive. On a
CPU-only ThinkPad a 35-utterance window ran past the 1800s socket timeout
and failed, identically, on every retry: half an hour spent each time to
learn nothing, three times, and then quarantine.

Splitting cannot be the ingester's job after the fact, because re-ingesting
a transcript at a smaller --max-items changes every window boundary, which
changes batch_id, which changes signal_id -- and every window already
extracted would be re-extracted and stored a second time under new ids.
This works on the single window that is stuck, and touches nothing else.

SAFE ONLY FOR A WINDOW THAT NEVER SUCCEEDED, for the same reason. If its
signals are already in the store, splitting it will duplicate them: the
pieces carry new source_refs and therefore new batch_ids, and the store has
no way to know the content was seen before. A window still sitting in the
inbox has by definition not been written; one in processed/ has.

The original is moved to <inbox>/split-originals/ rather than deleted --
the same instinct as quarantine/, that the way out of a bad state should
not be to destroy the evidence of it.
"""
import argparse
import json
import os
import shutil
import sys

# One window is one model call, and the size changes what the model
# returns -- see extraction/windowing.py. Imported rather than repeated:
# this default disagreeing with quickscan's is what made a preview
# contradict the pipeline it previews.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "extraction"))
from windowing import DEFAULT_WINDOW_ITEMS          # noqa: E402


def split(spec, max_items):
    """Mirrors transcript_ingest's chunking: a distinct source_ref per piece
    and window bounds taken from the chunk's own first and last utterance,
    so each piece hashes to its own batch and none of them collide."""
    items = spec.get("items") or []
    out = []
    for i in range(0, len(items), max_items):
        chunk = items[i:i + max_items]
        sub = {"job": dict(spec["job"]),
               "directory": spec.get("directory") or {},
               "items": chunk}
        sub["job"]["source_ref"] += "-s{0}".format(i // max_items + 1)
        if chunk[0].get("timestamp"):
            sub["job"]["window_start"] = chunk[0]["timestamp"]
        if chunk[-1].get("timestamp"):
            sub["job"]["window_end"] = chunk[-1]["timestamp"]
        out.append(sub)
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Split a stuck inbox window into smaller windows.")
    ap.add_argument("file")
    ap.add_argument("--max-items", type=int, default=DEFAULT_WINDOW_ITEMS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = os.path.abspath(args.file)
    if not os.path.exists(path):
        print("no such file: {0}".format(path))
        return 2
    inbox = os.path.dirname(path)
    if os.path.basename(inbox) in ("processed", "quarantine"):
        print("refusing: {0} has already been extracted, and splitting it "
              "would store its signals a second time under new ids. Only "
              "split a window still waiting in the inbox.".format(
                  os.path.basename(path)))
        return 2

    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    pieces = split(spec, args.max_items)
    stem = os.path.basename(path)[:-len(".json")]
    print("{0}: {1} utterance(s) -> {2} window(s) of at most {3}".format(
        os.path.basename(path), len(spec.get("items") or []), len(pieces),
        args.max_items))
    for n, sub in enumerate(pieces, 1):
        name = "{0}-s{1}.json".format(stem, n)
        print("  {0:<58} {1} utterance(s)".format(name, len(sub["items"])))
        if args.dry_run:
            continue
        with open(os.path.join(inbox, name), "w", encoding="utf-8") as f:
            json.dump(sub, f, indent=2)
            f.write("\n")
    if args.dry_run:
        print("\nDRY RUN -- nothing written.")
        return 0

    keep = os.path.join(inbox, "split-originals")
    os.makedirs(keep, exist_ok=True)
    shutil.move(path, os.path.join(keep, os.path.basename(path)))
    print("\noriginal moved to split-originals/ so the tick stops retrying "
          "it.\nnext tick extracts {0} smaller window(s) instead of one that "
          "cannot finish.".format(len(pieces)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
