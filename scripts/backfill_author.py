"""Fill signals.author_ref from the transcripts that produced the signals.

    DB_DSN=... python3 scripts/backfill_author.py [--apply]

Dry run by default: prints what it would fill and stops.

WHY THIS EXISTS, given that re-extraction is supposed to cover it.
migrate_author_ref.sql added the column and named re-extraction as the way
to populate it, on the grounds that content-hash ids make a replay
"upsert-shaped". They are not. signal_id deliberately excludes author_ref,
so a replay produces identical ids, and write_batch inserts ON CONFLICT
(signal_id) DO NOTHING and skips on rowcount 0 -- the freshly extracted
author is discarded. Re-extracting a window fills nothing.

Authorship was never the model's to guess, either: extraction_worker takes
it from the window item, not the LLM. So this does the same computation the
extractor would, minus the model -- primary evidence resource_id, back to
the utterance, out with its author_ref -- and needs no Ollama at all.

Requires storage/migrate_author_backfill.sql: signals is append-only and
the unmigrated guard refuses the UPDATE. doctor reports whether it is in
place. Windows that are no longer on disk keep author_ref NULL, and those
candidates promote on persistence alone.
"""
import glob
import json
import os
import sys

import psycopg2

APPLY = "--apply" in sys.argv


def utterance_authors(inbox):
    """resource_id -> author_ref, straight from the windows on disk."""
    authors = {}
    files = sorted(glob.glob(os.path.join(inbox, "*.json")))
    for path in files:
        with open(path) as f:
            doc = json.load(f)
        directory = doc.get("directory") or {}
        for item in doc.get("items") or []:
            rid = item.get("resource_id")
            ref = (item.get("author_ref")
                   or directory.get(item.get("author_name")))
            if rid and ref:
                authors[rid] = ref
    return authors, files


def main():
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("DB_DSN is unset. Run through ./tl, or export it first.")
        return 2
    inbox = os.path.join(
        os.path.expanduser(os.environ.get("TL_INBOX", "~/task-ledger-inbox")),
        "processed")
    if not os.path.isdir(inbox):
        print("no processed inbox at {0} -- nothing to read authors "
              "from.".format(inbox))
        return 2

    authors, files = utterance_authors(inbox)
    print("windows read: {0}   utterances with an author: {1}".format(
        len(files), len(authors)))

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""
                SELECT s.signal_id, s.signal_type, e.resource_id
                FROM signals s
                JOIN evidence_refs e
                  ON e.signal_id = s.signal_id AND e.ordinal = 1
                WHERE s.author_ref IS NULL
                ORDER BY s.signal_id""")
            rows = cur.fetchall()

            hits, misses, by_type = [], 0, {}
            for signal_id, stype, rid in rows:
                ref = authors.get(rid)
                if ref:
                    hits.append((signal_id, ref))
                    by_type[stype] = by_type.get(stype, 0) + 1
                else:
                    misses += 1

            print("signals missing author_ref: {0}".format(len(rows)))
            print("resolvable from these windows: {0}".format(len(hits)))
            for stype, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
                print("   {0:<18} {1}".format(stype, n))
            if misses:
                print("not resolvable (window no longer on disk): "
                      "{0}".format(misses))

            if not APPLY:
                print("\nDRY RUN -- nothing written. Re-run with --apply.")
                return 0

            for signal_id, ref in hits:
                cur.execute(
                    "UPDATE signals SET author_ref=%s WHERE signal_id=%s",
                    (ref, signal_id))
            # Sightings copied the null when they were written, and
            # v_task_candidates counts DISTINCT speaker_ref -- so breadth
            # stays dead until these catch up too. State events carry no
            # signal_id and are correctly left alone.
            cur.execute("""
                UPDATE task_candidate_events c
                SET speaker_ref = s.author_ref
                FROM signals s
                WHERE c.signal_id = s.signal_id
                  AND c.speaker_ref IS NULL
                  AND s.author_ref IS NOT NULL""")
            print("\nfilled author_ref on {0} signal(s), speaker_ref on {1} "
                  "sighting(s).".format(len(hits), cur.rowcount))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
