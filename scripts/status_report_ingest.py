#!/usr/bin/env python3
"""A person's status report -> their own claims, through the same gates.

    python3 scripts/status_report_ingest.py report.txt --for jason-tjia
    python3 scripts/status_report_ingest.py report.txt --for jason-tjia --dry-run

The pilot's first reviewer scored the extraction clean and then named
the gap: "it missed a lot of tasks that I was actually assigned -- I've
gathered these tasks from my status report." Meetings are not where all
work lives; a weekly status report is a person stating their own
assignments in their own words, which is the strongest attribution this
system ever sees. This reads the format people actually write:

    Week of 07/13:
    - AI Sandbox Guide & Communication
    - Drafting Practice Questions for RAI Taskforce Training

    Week of 08/24:
    - Smart Utilization Project Charter

Each bullet becomes an assignment claim: authored by the person, owned
by the person (self_reference -- they wrote it about themselves),
evidence pointing at the report line with its content hash. One batch
per week, deterministic ids, so re-ingesting an updated report is an
upsert-shaped no-op for the weeks that did not change. NOTHING lands on
the board from here: like every other source, these pool, reconcile,
and arrive as review cards the person answers.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "extraction"))

WEEK_RE = re.compile(
    r"^\s*week\s+of\s+(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s*:?\s*$",
    re.IGNORECASE)
BULLET_RE = re.compile(r"^\s*[-*•●]\s+(.+?)\s*$")


def _sha(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def ref_for(display):
    r = re.sub(r"[^a-z0-9]+", "-", (display or "").lower()).strip("-")
    return r if r.startswith("aad-") else "aad-" + r


def _week_date(m, today=None):
    """'Week of 07/13' -> a date. Yearless dates take the year that puts
    them in the PAST: a status report describes work done, and '12/29'
    read in January means last December, not eleven months from now."""
    today = today or datetime.date.today()
    month, day = int(m.group(1)), int(m.group(2))
    if m.group(3):
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return datetime.date(year, month, day)
    d = datetime.date(today.year, month, day)
    if d > today + datetime.timedelta(days=7):
        d = datetime.date(today.year - 1, month, day)
    return d


def parse_report(text, today=None):
    """-> [(week_date, [task line, ...]), ...] in file order."""
    weeks, current = [], None
    for line in (text or "").splitlines():
        wm = WEEK_RE.match(line)
        if wm:
            current = (_week_date(wm, today), [])
            weeks.append(current)
            continue
        bm = BULLET_RE.match(line)
        if bm and current is not None:
            current[1].append(bm.group(1))
        elif current is not None and line.strip():
            # a non-bullet line under a week continues the previous
            # bullet -- people wrap long task names
            if current[1]:
                current[1][-1] += " " + line.strip()
    return [(d, items) for d, items in weeks if items]


def build_batches(weeks, person_ref, source_label):
    batches = []
    for week, items in weeks:
        source_ref = "statusreport-{0}-{1}".format(
            person_ref.replace("aad-", ""), week.isoformat())
        batch_id = _sha(source_ref, "statusreport-v1")[:32]
        signals = []
        for i, item in enumerate(items):
            signals.append({
                "signal_id": _sha(batch_id, item)[:32],
                "type": "assignment",
                "title": item[:120],
                "description": ("From {0}'s status report, week of {1}."
                                .format(person_ref, week.isoformat())),
                "status_signal": "in_progress",
                "confidence": 0.9,
                # They wrote it, about themselves: author and owner are
                # the same person, and self_reference is literally true
                # here in a way no meeting claim can match.
                "author_ref": person_ref,
                "owner_candidates": [{"person_ref": person_ref,
                                      "confidence": 0.9,
                                      "basis": "self_reference"}],
                "evidence": [{
                    "source_type": "document_span",
                    "resource_id": "{0}#item-{1}".format(source_ref, i + 1),
                    "span": None,
                    "content_hash": _sha(item),
                }],
            })
        batches.append({
            "batch_id": batch_id,
            "extracted_at": datetime.datetime.now().replace(
                microsecond=0).isoformat(),
            "extractor": {"model": "human:status-report",
                          "prompt_version": "statusreport-v1"},
            "window": {"source_type": "document",
                       "source_ref": source_ref,
                       "time_start": week.isoformat() + "T00:00:00",
                       "time_end": (week + datetime.timedelta(days=7)
                                    ).isoformat() + "T00:00:00"},
            "signals": signals,
        })
    return batches


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--for", dest="person", required=True,
                    help="whose report this is -- author, owner and pool "
                         "target, all three")
    ap.add_argument("--dsn", default=os.environ.get("DB_DSN"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from textio import read_text
    person = ref_for(args.person)
    total = 0
    for path in args.files:
        weeks = parse_report(read_text(path, verbose=True))
        if not weeks:
            print("{0}: no 'Week of MM/DD:' sections with tasks found"
                  .format(os.path.basename(path)))
            continue
        batches = build_batches(weeks, person, os.path.basename(path))
        try:
            import jsonschema
            with open(os.path.join(ROOT, "contracts",
                                   "extraction_schema.json")) as f:
                schema = json.load(f)
            for b in batches:
                jsonschema.validate(b, schema)
        except ImportError:
            print("  (jsonschema not installed here -- contract "
                  "validation skipped; the store will still refuse "
                  "malformed rows)")
        for b in batches:
            n = len(b["signals"])
            total += n
            print("{0}: {1} task(s)".format(b["window"]["source_ref"], n))
            for s in b["signals"]:
                print("    - " + s["title"])
        if args.dry_run:
            continue
        if not args.dsn:
            print("no --dsn and DB_DSN unset; stopping before any write")
            return 2
        import signal_store_pg
        for b in batches:
            out = signal_store_pg.write_batch(args.dsn, b, [person])
            print("  -> {0}".format(out))
    if args.dry_run:
        print("total: {0} task(s) -- DRY RUN, nothing written".format(total))
    else:
        print("total: {0} task(s) pooled for {1}; they become review "
              "cards at the next tick".format(total, person))
    return 0


if __name__ == "__main__":
    sys.exit(main())
