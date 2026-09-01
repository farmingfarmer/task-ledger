"""Reconcile one user's signal pool against their board, locally.

Usage:
    python3 run_reconcile.py --user aad-dev \
        --dsn "host=localhost port=5433 dbname=taskintel user=postgres"

    # real embeddings + judge via Ollama:
    python3 run_reconcile.py --user aad-dev --dsn "..." \
        --embed-model nomic-embed-text --judge-model mistral-small3.1

    # persist the packet and consume the signals:
    python3 run_reconcile.py --user aad-dev --dsn "..." --write

Defaults are fully offline: TokenEmbedder for similarity and no judge
(judge-band signals are held rather than guessed at). Output: the validated
ReviewPacket to stdout, the report (consumed/held/annotations/judge calls)
to stderr. Python 3.8 compatible.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.request

import board_io_pg
import embedders
import reconciler


# A judge answer is a short JSON object -- a verdict, a confidence, a
# task id, one sentence. Left uncapped, an 8B model on a laptop CPU will
# happily spend minutes explaining itself first, and the reply is
# discarded past the closing brace anyway. num_predict is the difference
# between a judge call that takes twenty seconds and one that takes five
# minutes, on the same machine and the same model.
JUDGE_NUM_PREDICT = int(os.environ.get("TL_JUDGE_NUM_PREDICT", "256"))

# Per-call budget, not a session budget. 300s was chosen when nothing had
# ever hit it; the first real multi-person run hit it twice, and back
# then a hit meant an unhandled TimeoutError and a lost packet. The
# reconciler now degrades a failed judge to a hold, so a shorter budget
# costs a held signal rather than a person's whole run -- and holding is
# the documented behaviour when there is no judge at all.
JUDGE_TIMEOUT = int(os.environ.get("TL_JUDGE_TIMEOUT", "90"))


def ollama_chat(model, host="http://localhost:11434"):
    def call(messages):
        body = json.dumps({"model": model, "messages": messages,
                           "stream": False,
                           "options": {"temperature": 0.0,
                                       "num_predict": JUDGE_NUM_PREDICT},
                           # Keep the weights resident between calls. A
                           # judge is consulted a handful of times per
                           # person; reloading several GB before each one
                           # is most of the wall clock.
                           "keep_alive": "10m"}).encode("utf-8")
        req = urllib.request.Request(
            host + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=JUDGE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["message"]["content"]
    return call


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--embed-model", default=None,
                    help="Ollama embedding model; omit for offline tokens")
    ap.add_argument("--judge-model", default=None,
                    help="Ollama chat model for the judge; omit to hold "
                         "judge-band signals")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--write", action="store_true",
                    help="persist packet, consume signals, apply aliases")
    args = ap.parse_args()

    embed = (embedders.CachedEmbedder(
                 embedders.OllamaEmbedder(args.embed_model,
                                          args.ollama_host),
                 args.dsn, args.embed_model)
             if args.embed_model else embedders.TokenEmbedder())
    judge = (ollama_chat(args.judge_model, args.ollama_host)
             if args.judge_model else None)

    signals = board_io_pg.load_signals(args.dsn, args.user)
    tasks, version = board_io_pg.load_board(args.dsn, args.user)
    all_tasks = board_io_pg.load_all_boards(args.dsn)
    candidates = board_io_pg.load_candidates(
        args.dsn, args.user,
        window_days=(reconciler.load_policy().get("candidate_promotion")
                     or {}).get("window_days"))
    sys.stderr.write("loaded {0} pooled signals, {1} matchable tasks, "
                     "{2} tasks across all boards, {3} candidate(s), "
                     "board v{4}\n".format(
                         len(signals), len(tasks), len(all_tasks),
                         len(candidates), version))

    run_id = "manual-{0}".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    rec = reconciler.Reconciler(embed=embed, judge_llm=judge)
    packet, report = rec.reconcile(args.user, signals, tasks, version, run_id,
                                   all_tasks=all_tasks,
                                   candidates=candidates)

    sys.stderr.write("report: " + json.dumps(
        {k: v for k, v in report.items() if k != "annotations"}, indent=2)
        + "\n")
    if report["annotations"]:
        sys.stderr.write("annotations: " + json.dumps(report["annotations"])
                         + "\n")
    print(json.dumps(packet, indent=2))

    if args.write:
        result = board_io_pg.write_packet(
            args.dsn, packet, report["consumed"], report["annotations"],
            run_id, report=report)
        sys.stderr.write("db write: " + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
