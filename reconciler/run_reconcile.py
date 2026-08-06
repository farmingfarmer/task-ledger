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
import sys
import urllib.request

import board_io_pg
import embedders
import reconciler


def ollama_chat(model, host="http://localhost:11434"):
    def call(messages):
        body = json.dumps({"model": model, "messages": messages,
                           "stream": False,
                           "options": {"temperature": 0.0}}).encode("utf-8")
        req = urllib.request.Request(
            host + "/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
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

    embed = (embedders.OllamaEmbedder(args.embed_model, args.ollama_host)
             if args.embed_model else embedders.TokenEmbedder())
    judge = (ollama_chat(args.judge_model, args.ollama_host)
             if args.judge_model else None)

    signals = board_io_pg.load_signals(args.dsn, args.user)
    tasks, version = board_io_pg.load_board(args.dsn, args.user)
    all_tasks = board_io_pg.load_all_boards(args.dsn)
    sys.stderr.write("loaded {0} pooled signals, {1} matchable tasks, "
                     "{2} tasks across all boards, board v{3}\n".format(
                         len(signals), len(tasks), len(all_tasks), version))

    run_id = "manual-{0}".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    rec = reconciler.Reconciler(embed=embed, judge_llm=judge)
    packet, report = rec.reconcile(args.user, signals, tasks, version, run_id,
                                   all_tasks=all_tasks)

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
            run_id)
        sys.stderr.write("db write: " + json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
