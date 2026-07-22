"""Score the extraction stage against a synthesized corpus.

Runs the REAL extraction worker (real prompts, real salvage and guards)
over every window in a corpus directory, matches extracted signals to the
golden labels, and reports:

  recall           golden signals recovered (by type where labeled strictly)
  precision        extracted signals that correspond to something real
                   (golden signal, acceptable trap reading, or duplicate
                   sighting) vs fabrications
  attribution      owner_ref + basis correct where golden specifies them
  vague deadlines  vague phrases carried as vague/null, not invented dates
  trap hits        affirmative extractions from negations (the bad ones)
  distractor FPs   signals extracted from chatter

Matching is lenient on titles (token overlap on title_core) and strict on
what matters downstream: type family, attribution, dates.

Usage:
  python3 score_extraction.py --corpus corpus/ --model mistral-small3.1
  python3 score_extraction.py --corpus corpus/ --limit 4   # subset
Requires Ollama unless --llm-fake (testing) is used. Python 3.8.
"""

import argparse
import glob
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "extraction"))

import extraction_worker  # noqa: E402

STOP = {"the", "a", "an", "for", "of", "to", "in", "on", "and", "fix"}


def toks(s):
    return {w for w in "".join(c if c.isalnum() or c == " " else " "
                               for c in (s or "").lower()).split()
            if w not in STOP}


def overlap(a, b):
    ta, tb = toks(a), toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def ollama_llm(model, host):
    def call(messages):
        body = json.dumps({"model": model, "messages": messages,
                           "stream": False,
                           "options": {"temperature": 0.0}}).encode()
        req = urllib.request.Request(host + "/api/chat", data=body,
                                     headers={"Content-Type":
                                              "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())["message"]["content"]
    return call


TYPE_FAMILY = {
    "status_update": {"status_update", "task_mention"},
    "assignment": {"assignment"},
    "completion_claim": {"completion_claim", "status_update"},
    "blocker": {"blocker", "status_update"},
    "proposal": {"proposal", "task_mention"},
}


def match_type(golden, extracted_type):
    if "type_any" in golden:
        return extracted_type in golden["type_any"]
    fam = TYPE_FAMILY.get(golden["type"], {golden["type"]})
    return extracted_type in fam


def score_window(golden, signals):
    res = {"golden": len(golden["signals"]), "recovered": 0,
           "attr_checked": 0, "attr_ok": 0,
           "vague_checked": 0, "vague_ok": 0,
           "trap_hits": 0, "trap_details": [],
           "fabrications": 0, "extracted": len(signals)}
    used = set()
    for g in golden["signals"]:
        best, best_i = 0.0, None
        for i, s in enumerate(signals):
            if i in used:
                continue
            ov = overlap(g["title_core"], s["title"] + " " +
                         (s.get("description") or ""))
            if ov >= 0.5 and match_type(g, s["type"]) and ov > best:
                best, best_i = ov, i
        if best_i is None:
            continue
        used.add(best_i)
        res["recovered"] += 1
        s = signals[best_i]
        if g.get("owner_ref") and not g.get("mention_only"):
            res["attr_checked"] += 1
            cands = {c["person_ref"]: c["basis"]
                     for c in s.get("owner_candidates") or []}
            basis_ok = (g.get("basis") is None
                        or cands.get(g["owner_ref"]) == g["basis"]
                        or (g.get("third_party")
                            and g["owner_ref"] in cands))
            if g["owner_ref"] in cands and basis_ok:
                res["attr_ok"] += 1
        if g.get("deadline_precision") == "vague":
            res["vague_checked"] += 1
            dl = s.get("deadline")
            if dl is None or dl.get("date") is None \
               or dl.get("precision") == "vague":
                res["vague_ok"] += 1

    for trap in golden["traps"]:
        for i, s in enumerate(signals):
            if i in used:
                continue
            if overlap(trap["title_core"], s["title"]) >= 0.5 \
               and s["type"] not in trap["acceptable"]:
                res["trap_hits"] += 1
                res["trap_details"].append(
                    {"trap": trap["trap"], "extracted_as": s["type"],
                     "title": s["title"]})
                used.add(i)
                break

    # anything unmatched and not near ANY golden/trap title = fabrication
    all_titles = ([g["title_core"] for g in golden["signals"]] +
                  [t["title_core"] for t in golden["traps"]])
    for i, s in enumerate(signals):
        if i in used:
            continue
        if any(overlap(t, s["title"]) >= 0.5 for t in all_titles):
            continue   # duplicate sighting or family-mismatch: not counted
        res["fabrications"] += 1
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument("--model", default="mistral-small3.1")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--llm-fake", default=None,
                    help="path to a JSON of canned responses (testing)")
    args = ap.parse_args()

    with open(os.path.join(args.corpus, "golden.json")) as f:
        golden_all = json.load(f)
    files = sorted(glob.glob(os.path.join(args.corpus, "window-*.json")))
    if args.limit:
        files = files[:args.limit]

    if args.llm_fake:
        canned = json.load(open(args.llm_fake))
        counter = {"i": -1}

        def llm(messages):
            counter["i"] += 1
            return json.dumps(canned[min(counter["i"], len(canned) - 1)])
    else:
        llm = ollama_llm(args.model, args.ollama_host)

    totals = {}
    for path in files:
        fname = os.path.basename(path)
        with open(path) as f:
            spec = json.load(f)
        worker = extraction_worker.ExtractionWorker(
            fetcher=lambda job, items=spec["items"]: items,
            llm=llm, directory=spec["directory"],
            model_name="ollama:" + args.model)
        batch, report = worker.run_job(spec["job"])
        r = score_window(golden_all[fname], batch["signals"])
        r["dropped"] = len(report["dropped"])
        totals[fname] = r
        print("{0}: {1}/{2} recovered, {3} trap hits, {4} fabrications, "
              "{5} extracted".format(fname, r["recovered"], r["golden"],
                                     r["trap_hits"], r["fabrications"],
                                     r["extracted"]))
        for td in r["trap_details"]:
            print("    TRAP: {0} -> extracted as {1} (\"{2}\")".format(
                td["trap"], td["extracted_as"], td["title"]))

    def s(key):
        return sum(t[key] for t in totals.values())

    print("\n================ corpus report ================")
    print("windows scored:      {0}".format(len(totals)))
    g, rcv = s("golden"), s("recovered")
    print("recall:              {0}/{1} = {2:.0%}".format(rcv, g,
                                                          rcv / max(g, 1)))
    ac, ao = s("attr_checked"), s("attr_ok")
    print("attribution:         {0}/{1} = {2:.0%}".format(ao, ac,
                                                          ao / max(ac, 1)))
    vc, vo = s("vague_checked"), s("vague_ok")
    print("vague-date safety:   {0}/{1} = {2:.0%}".format(vo, vc,
                                                          vo / max(vc, 1)))
    print("negation trap hits:  {0}".format(s("trap_hits")))
    print("fabrications:        {0}".format(s("fabrications")))
    print("salvage drops:       {0}".format(s("dropped")))
    with open(os.path.join(args.corpus, "score_report.json"), "w") as f:
        json.dump(totals, f, indent=1)
    print("per-window detail -> {0}/score_report.json".format(args.corpus))


if __name__ == "__main__":
    main()
