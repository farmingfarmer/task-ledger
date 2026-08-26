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
from windowing import DEFAULT_WINDOW_ITEMS               # noqa: E402
from textio import read_text                             # noqa: E402


def windows_from_txt(paths, max_items):
    """Meetings -> in-memory windows, the same shapes the inbox holds.
    Nothing is written; the ingester's parser is reused so quickscan can
    never disagree with what a real ingest would have produced."""
    lines, unparsed = [], []
    for path in paths:
        lines.extend(read_text(path, verbose=True)
                     .replace("\r\n", "\n").split("\n"))
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


def cluster(items, floor):
    """Greedy nearest-cluster, the same shape _sight_candidate uses.

    A mention is compared against the clusters already formed and joins the
    best one above the floor, or starts its own. First-member vector rather
    than a centroid, because that is what the register does -- a candidate
    carries the embedding of the title it was first heard under, and this
    exists to predict the register, not to out-cluster it.
    """
    clusters = []
    for s, vec in items:
        best, bi = 0.0, None
        for i, c in enumerate(clusters):
            sim = embedders.cosine(vec, c["vec"])
            if sim > best:
                best, bi = sim, i
        if bi is not None and best >= floor:
            clusters[bi]["members"].append(s)
        else:
            clusters.append({"vec": vec, "title": s["title"], "members": [s]})
    return clusters


def report_new_work(new_items, embed):
    """What the register would actually hold, and what would reach a queue.

    Two corrections to a flat list of new titles, both of which change the
    number a lot:

    ONE PROJECT IS NOT FOUR CANDIDATES. "Synth kit project", "Synth kit
    system" and "Synthkit project" are one piece of work said three ways.
    The register merges them at same_candidate_floor; a flat list counts
    them separately and overstates how much distinct work was found.

    A CANDIDATE BELONGS TO A POOL, NOT TO THE CORPUS. Sightings accumulate
    per pool owner, so the same project raised by three different people is
    three candidates with one sighting each, not one with three -- and none
    of them promotes on persistence. Clustering globally would report work
    as corroborated that the pipeline will never treat that way. Routing
    here is pool_targets, the same function the store uses.
    """
    import signal_store_pg
    import yaml as _yaml
    with open(os.path.join(ROOT, "policy", "matching_policy.yaml")) as f:
        pol = _yaml.safe_load(f) or {}
    floor = (pol.get("candidate_promotion") or {}).get(
        "same_candidate_floor", 0.62)
    rule = pol.get("candidate_promotion") or {}
    need_s = rule.get("min_sightings", 3)
    need_p = rule.get("min_distinct_speakers", 2)
    mode = rule.get("mode", "all")

    by_user = {}
    for s, vec in new_items:
        targets, _how = signal_store_pg.pool_targets(s, [], False)
        for u in (targets or ["(unrouted -- reaches nobody)"]):
            by_user.setdefault(u, []).append((s, vec))
    if not by_user:
        return

    print("NEW WORK, clustered as the register would hold it")
    print("bar: {0} sighting(s) {1} {2} speaker(s), per pool\n".format(
        need_s, "or" if mode == "any" else "and", need_p))

    grand = [0, 0]
    for user in sorted(by_user):
        clusters = cluster(by_user[user], floor)
        promote, wait = [], []
        for c in clusters:
            n = len(c["members"])
            speakers = set(m.get("author_ref") for m in c["members"]
                           if m.get("author_ref"))
            ok_s, ok_p = n >= need_s, len(speakers) >= need_p
            enough = (ok_s or ok_p) if mode == "any" else (ok_s and ok_p)
            (promote if enough else wait).append((c, n, len(speakers)))
        grand[0] += len(promote)
        grand[1] += len(clusters)
        print("{0}  --  {1} distinct from {2} mention(s), {3} would "
              "promote".format(user, len(clusters), len(by_user[user]),
                               len(promote)))
        for label, group in (("  WOULD PROMOTE", promote),
                             ("  would wait", wait)):
            if not group:
                continue
            print(label)
            for c, n, nsp in group:
                print("    {0:<52} {1} mention(s), {2} speaker(s)".format(
                    c["title"][:52], n, nsp))
                others = [m["title"] for m in c["members"][1:]
                          if m["title"] != c["title"]]
                if others:
                    print("      also heard as: {0}".format(
                        "; ".join(others[:4])))
        print("")
    print("{0} distinct piece(s) of new work; {1} would reach a review "
          "queue.\n".format(grand[1], grand[0]))


def main():
    ap = argparse.ArgumentParser(
        description="Extract work from a transcript and check what is new. "
                    "Read-only.")
    ap.add_argument("files", nargs="+", help=".txt transcript or window .json")
    ap.add_argument("--max-items", type=int,
                    default=DEFAULT_WINDOW_ITEMS,
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
    seen, rows, new_items = set(), [], []
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
            if verdict == "NEW":
                new_items.append((s, svec if known else embed([s["title"]])[0]))

    print("")
    report_new_work(new_items, embed)

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
