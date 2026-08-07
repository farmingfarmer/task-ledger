"""Ask the ledger from a terminal.

    python3 ask/run_ask.py --as aad-dev --dsn "$DB_DSN" "what is blocked"
    python3 ask/run_ask.py --as aad-dev --dsn "..." "status of the intake work"
    python3 ask/run_ask.py --as aad-dev --dsn "..." --embed-model nomic-embed-text \
        "where did the redshift extract get to"

--as is trusted local identity, exactly as in review.py and share.py; the
answer is scoped to what that person can see. --embed-model turns on
semantic subject matching through Ollama; without it, matching is lexical
and entirely offline. Neither one writes any part of the answer: an
embedder ranks candidates, and the sentences are assembled from rows.

Python 3.8 compatible.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "reconciler"))

import ask as ask_mod        # noqa: E402
import ask_io_pg             # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Ask the Task Ledger.")
    ap.add_argument("question", nargs="+")
    ap.add_argument("--as", dest="user", required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--embed-model", default=None,
                    help="Ollama embedding model for subject matching; "
                         "omit for offline lexical matching")
    ap.add_argument("--ollama-host", default="http://localhost:11434")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    embed = None
    if args.embed_model:
        import embedders
        embed = embedders.OllamaEmbedder(args.embed_model, args.ollama_host)

    retriever = ask_io_pg.Retriever(args.dsn, args.user)
    ans = ask_mod.answer(" ".join(args.question), retriever, embed=embed)
    if args.json:
        sys.stdout.write(json.dumps(ans, indent=2) + "\n")
    else:
        sys.stdout.write(ask_mod.render_text(ans))
    # Exit 1 when the ledger declined to answer, so a script can tell the
    # difference between "no" and "nothing".
    return 1 if ans.get("refusal") else 0


if __name__ == "__main__":
    sys.exit(main())
