#!/usr/bin/env python3
"""Read a transcript and say what work it names -- and which of it is new.

    DB_DSN=... python3 scripts/quickscan.py transcripts_raw.txt
    DB_DSN=... python3 scripts/quickscan.py ~/task-ledger-inbox/*.json

STRICTLY READ-ONLY. No signals stored, no pool events, no packets, no
proposals, no candidate sightings, nothing moved to processed/, no retry
counter touched. Run it as often as you like on the same file.

WHY IT EXISTS. The full tick answers "what should each of eleven people be
asked to confirm this week", and to do that honestly it extracts, pools,
matches, judges, aggregates, gates and writes a reviewable packet per
person. That is the product. It is also an hour of local inference and
eight stages that can each legitimately produce nothing, which makes it a
poor way to answer the much smaller question people actually ask first:

    "I talked about my projects in this meeting -- did it notice?"

That question needs extraction and a novelty check, and nothing else. So
this is those two steps and a printed list. When it says NEW, the full
pipeline would register a candidate; when it says a title is already
tracked or already standing, it would not.

Windows default to 8 utterances rather than the ingester's 35: one model
call per window and attention cost climbing faster than linearly with
context means small windows are dramatically cheaper on CPU, and a line
every couple of minutes beats a blank terminal for half an hour.
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for _sub in ("extraction", "reconciler", "policy", "cadence", "scripts"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import embedders                                        # noqa: E402
import extraction_worker                                # noqa: E402
import cadence_tick                                     # noqa: E402
import transcript_ingest as ti                          # noqa: E402


def windows_from_txt(paths, max_items):
    """Meetings -> in-memory windows, the same shapes the inbox holds.
    Nothing is written; the ingester's parser is reused so quickscan can
    never disagree with what a real ingest would have produced."""
    lines, unparsed = [], []
    for path in paths:
        with open(path, encoding="utf-8-sig") as f:
            lines.extend(f.read().replace("\r\n", "\n").split("\n"))
    built = []
    for title, dm, body in ti.split_meetings(lines):
        parsed = ti.parse_meeting(title, dm, body, unparsed)
        if parsed:
            built.append(parsed[0])
    roster = {}
    for w in built:
        for name in w["directory"]:
            roster.setdefault(name, ti.ref_for(name))
    resolved = ti.with_aliases(roster)
    out = []
    for w in built:
        w["directory"] = dict(resolved)
        for it in w["items"]:
            it["author_ref"] = roster[it["author_name"]]
        items = w["items"]
        for i in range(0, len(items), max_items):
            chunk = items[i:i + max_items]
            sub = {"job": dict(w["job"]), "directory": w["directory"],
                   "items": chunk}
            sub["job"]["source_ref"] += "-q{0}".format(i // max_items + 1)
            sub["job"]["window_start"] = chunk[0]["timestamp"]
            sub["job"]["window_end"] = chunk[-1]["timestamp"]
            out.append(sub)
    return out, unparsed


def load_known(dsn):
    """Task titles on every board, and candidate titles already standing.
    Read-only; a database that cannot answer costs the novelty check, not
    the scan."""
    import psycopg2
    tasks, cands = [], []
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
    except Exception as exc:                            # noqa: BLE001
        print("  (no database: {0!r} -- extraction only, novelty "
              "unchecked)".format(exc))
        return None, None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT title, owner_ref FROM tasks_current "
                        "WHERE status NOT IN ('completed','cancelled')")
            tasks = [(r[0], r[1]) for r in cur.fetchall()]
            try:
                cur.execute("SELECT title, user_ref, sightings "
                            "FROM v_task_candidates WHERE state = 'sighted'")
                cands = [(r[0], r[1], r[2]) for r in cur.fetchall()]
            except Exception:                           # noqa: BLE001
                conn.rollback()
    finally:
        conn.close()
    return tasks, cands


def main():
    ap = argparse.ArgumentParser(
        description="Extract work from a transcript and check what is new. "
                    "Read-only.")
    ap.add_argument("files", nargs="+", help=".txt transcript or window .json")
    ap.add_argument("--max-items", type=int, default=8,
                    help="utterances per model call (default 8; smaller is "
                         "much faster on CPU)")
    ap.add_argument("--model", default=os.environ.get(
        "TL_EXTRACT_MODEL", "mistral-small3.1"))
    ap.add_argument("--embed-model", default=os.environ.get(
        "TL_EMBED_MODEL", "nomic-embed-text"))
    ap.add_argument("--ollama-host", default=os.environ.get(
        "OLLAMA_HOST_URL", "http://localhost:11434"))
    args = ap.parse_args()

    paths = []
    for f in args.files:
        paths.extend(sorted(glob.glob(f)) or [f])
    txt = [p for p in paths if p.lower().endswith(".txt")]
    jsn = [p for p in paths if p.lower().endswith(".json")]

    windows, unparsed = ([], [])
    if txt:
        windows, unparsed = windows_from_txt(txt, args.max_items)
    for p in jsn:
        with open(p, encoding="utf-8") as f:
            windows.append(json.load(f))
    if not windows:
        print("nothing to scan")
        return 2

    if not cadence_tick.ollama_up(args.ollama_host):
        print("ollama unreachable at {0} -- extraction needs it".format(
            args.ollama_host))
        return 1

    dsn = os.environ.get("DB_DSN")
    tasks, cands = (None, None)
    if dsn:
        tasks, cands = load_known(dsn)
    else:
        print("  (DB_DSN unset -- extraction only, novelty unchecked)")

    print("{0} window(s), {1} utterance(s), model {2}\n".format(
        len(windows), sum(len(w["items"]) for w in windows), args.model))
    if unparsed:
        print("  {0} line(s) could not be placed\n".format(len(unparsed)))

    embed = embedders.OllamaEmbedder(args.embed_model, args.ollama_host)
    known = []
    if tasks is not None:
        known = ([("on a board", t, o) for t, o in tasks]
                 + [("already standing ({0} sighting(s))".format(n), t, u)
                    for t, u, n in cands])
    known_vecs = embed([k[1] for k in known]) if known else []

    # The same three bands the reconciler uses, reported rather than
    # collapsed. Calling everything above judge_floor "known" would be a
    # lie of exactly the kind this tool exists to stop: on the first run
    # "migrate readmission model to Fargate" scored 0.64 against "ship
    # drift monitoring for readmission model" -- same model, different
    # work, and an embedding cannot tell them apart. The pipeline does not
    # pretend it can either; it pays for a judge call in that band. So the
    # middle band is reported as undecided, which is what it is.
    import yaml as _yaml
    with open(os.path.join(ROOT, "policy", "matching_policy.yaml")) as _f:
        _th = (_yaml.safe_load(_f) or {}).get("thresholds") or {}
    floor = _th.get("judge_floor", 0.62)
    auto = _th.get("auto_link", 0.90)
    seen, rows = set(), []
    for wi, w in enumerate(windows, 1):
        worker = extraction_worker.ExtractionWorker(
            fetcher=lambda job, items=w["items"]: items,
            llm=cadence_tick.ollama_chat(args.model, args.ollama_host),
            directory=w["directory"],
            model_name="ollama:{0}".format(args.model))
        batch, _report = worker.run_job(w["job"])
        sigs = batch["signals"]
        print("[{0}/{1}] {2}: {3} signal(s)".format(
            wi, len(windows), w["job"]["source_ref"], len(sigs)))
        for s in sigs:
            key = s["title"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            verdict, note = "NEW", ""
            if known:
                svec = embed([s["title"]])[0]
                best, bi = 0.0, None
                for i, kv in enumerate(known_vecs):
                    sim = embedders.cosine(svec, kv)
                    if sim > best:
                        best, bi = sim, i
                if bi is not None and best >= auto:
                    verdict = "TRACKED"
                    note = "{0}: \"{1}\" [{2}] sim {3:.2f}".format(
                        known[bi][0], known[bi][1], known[bi][2], best)
                elif bi is not None and best >= floor:
                    verdict = "UNSURE"
                    note = "closest {0}: \"{1}\" [{2}] sim {3:.2f} -- the "
                    note = note.format(known[bi][0], known[bi][1],
                                       known[bi][2], best)
                    note += "tick would ask the judge"
            rows.append((verdict, s["type"], s["title"],
                         s.get("author_ref"), note))

    print("")
    for verdict in ("NEW", "UNSURE", "TRACKED"):
        group = [r for r in rows if r[0] == verdict]
        if not group:
            continue
        print("{0} ({1})".format(verdict, len(group)))
        for _v, stype, title, author, note in group:
            print("  {0:<17} {1}".format(stype, title))
            print("  {0:<17} said by {1}{2}".format(
                "", author or "unattributed", "  |  " + note if note else ""))
        print("")
    print("{0} distinct title(s): {1} new, {2} needing a judge call, "
          "{3} already tracked.".format(
              len(rows), sum(1 for r in rows if r[0] == "NEW"),
              sum(1 for r in rows if r[0] == "UNSURE"),
              sum(1 for r in rows if r[0] == "TRACKED")))
    print("NEW is what the tick would register as a candidate; two of those "
          "and it becomes a proposal.")
    if tasks is None:
        print("Novelty was NOT checked -- no database.")
    print("Nothing was written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
