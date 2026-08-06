#!/usr/bin/env python3
"""Seed a complete, reviewable walkthrough state -- no Ollama required.

    export DB_DSN="..."             # zsh/bash
    set DB_DSN=...                  # cmd.exe
    python3 scripts/demo_offline.py     (python on Windows)

Pushes the canonical standup transcript through the REAL extraction worker
(with a canned model response, so the salvage/guard/attribution machinery
all runs), pools the signals, reconciles OFFLINE (TokenEmbedder, no judge
-- closures hold, exactly as a degraded day would), and writes a review
packet. It then prints the walkthrough commands for THIS platform's shell
(cli/review.py, then the dashboard at 127.0.0.1:8090 as aad-dev).

Idempotent: content-hash ids make re-runs no-ops; supersede keeps at most
one open packet. Safe for scratch or demo databases; it does create tasks
for user aad-dev on accept, so prefer a demo db on shared machines.
"""

import datetime
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
for sub in ("extraction", "reconciler", "policy"):
    sys.path.insert(0, os.path.join(ROOT, sub))

import board_io_pg            # noqa: E402
import embedders              # noqa: E402
import extraction_worker      # noqa: E402
import reconciler as rec_mod  # noqa: E402
import signal_store_pg        # noqa: E402


CANNED = {"signals": [
    {"type": "status_update", "title": "Deploy drift monitor to staging",
     "description": "Alex reports the drift monitor deployed to staging.",
     "owner_candidates": [{"name": "Alex", "basis": "self_reference",
                           "confidence": 0.9}],
     "status_signal": "in_progress", "deadline": None, "blocker": None,
     "confidence": 0.9, "evidence_items": ["1"]},
    {"type": "blocker", "title": "Unblock IAM role for Fargate",
     "description": "Production deploy blocked on the Fargate IAM role.",
     "owner_candidates": [{"name": "Priya", "basis": "inferred",
                           "confidence": 0.7}],
     "status_signal": "blocked", "deadline": None,
     "blocker": {"description": "IAM role stuck with the cloud team",
                 "blocking_party": None},
     "confidence": 0.8, "evidence_items": ["2"]},
    {"type": "completion_claim", "title": "Fix OCR crash in intake",
     "description": "Dev reports the OCR crash fix shipped.",
     "owner_candidates": [{"name": "Dev", "basis": "self_reference",
                           "confidence": 0.9}],
     "status_signal": "completed", "deadline": None, "blocker": None,
     "confidence": 0.9, "evidence_items": ["3"]},
    {"type": "assignment", "title": "Wire up retraining pipeline alerting",
     "description": "Alex assigns Dev to wire up alerting.",
     "owner_candidates": [{"name": "Dev", "basis": "explicit_assignment",
                           "confidence": 0.9}],
     "status_signal": None,
     "deadline": {"raw_text": "end of next week", "precision": "week",
                  "date": "2026-07-31"},
     "blocker": None, "confidence": 0.9, "evidence_items": ["4", "5"]},
    {"type": "proposal", "title": "Rebuild feature store documentation",
     "description": "Priya suggests rebuilding the stale docs.",
     "owner_candidates": [], "status_signal": None, "deadline": None,
     "blocker": None, "confidence": 0.7, "evidence_items": ["6"]},
]}


def hints(user):
    """The walkthrough commands in the shell of the machine running them.
    cmd.exe has no $VAR and no python3 on PATH, and the Windows port's
    Postgres listens on 5433 -- a copy-paste line that fails is worse than
    no line at all."""
    if sys.platform == "win32":
        return ("set DB_DSN=host=localhost port=5433 dbname=taskintel "
                "user=%USERNAME%",
                '  python cli\\review.py --as {0} --dsn "%DB_DSN%"'.format(
                    user),
                "  python integration\\dashboard\\dashboard_api.py")
    return ('export DB_DSN="host=localhost port=5432 dbname=taskintel '
            'user=$(whoami)"',
            '  python3 cli/review.py --as {0} --dsn "$DB_DSN"'.format(user),
            "  python3 integration/dashboard/dashboard_api.py")


def main():
    dsn = os.environ.get("DB_DSN")
    if not dsn:
        print("set DB_DSN first (scripts/doctor.py will tell you the "
              "right shape for this machine):")
        print("  " + hints("aad-dev")[0])
        sys.exit(2)

    spec = json.load(open(os.path.join(ROOT, "extraction",
                                       "sample_transcript.json")))
    worker = extraction_worker.ExtractionWorker(
        fetcher=lambda job: spec["items"],
        llm=lambda messages: json.dumps(CANNED),
        directory=spec["directory"],
        model_name="canned:demo")
    batch, report = worker.run_job(spec["job"])
    fallback = sorted(set(spec["directory"].values()))
    stored = signal_store_pg.write_batch(dsn, batch, fallback)
    print("extraction: {0} signals kept, {1} inserted "
          "(re-runs insert 0 -- idempotent)".format(
              report["kept"], stored["signals_inserted"]))

    user = "aad-dev"
    signals = board_io_pg.load_signals(dsn, user)
    tasks, version = board_io_pg.load_board(dsn, user)
    rec = rec_mod.Reconciler(embed=embedders.TokenEmbedder(),
                             judge_llm=None)
    run_id = "demo-{0}".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    packet, rep = rec.reconcile(user, signals, tasks, version, run_id,
                                all_tasks=board_io_pg.load_all_boards(dsn))
    result = board_io_pg.write_packet(dsn, packet, rep["consumed"],
                                     rep["annotations"], run_id)
    print("reconcile (offline): {0} proposals written, {1} held "
          "(closures wait for a judge -- the designed degradation), "
          "{2} packet(s) superseded".format(
              result["proposals"], len(rep["held"]),
              len(result["superseded"])))
    print()
    print("walkthrough ready for user {0}:".format(user))
    _, review_cmd, dash_cmd = hints(user)
    print(review_cmd)
    print("{0}   ->  http://127.0.0.1:8090  (view as {1})".format(
        dash_cmd, user))


if __name__ == "__main__":
    main()
