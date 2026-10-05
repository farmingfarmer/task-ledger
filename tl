#!/bin/zsh
# tl -- one entrypoint for the daily loop.
#
# Two problems this solves, both of them workflow rather than code:
#
#   1. Every command was a paragraph. The reconciler line alone is four
#      flags and two model names, retyped from DEVELOPMENT.md each session.
#      Here it is `./tl reconcile`.
#   2. Failure landed last. An unset DB_DSN or a stopped Postgres was
#      discovered by the command that needed it, which was usually the
#      slow one at the end. Every DB-touching subcommand below now runs
#      doctor first (~0.3s) and refuses to start if it fails, so a broken
#      environment costs a third of a second instead of a full run.
#
# The profile is two env vars (PROFILES.md): DB_DSN and TL_INBOX. This
# sets them when they are unset, so "export the vars" stops being a
# prerequisite you can forget. `--synth` overrides them authoritatively,
# which is the only safe way to mean it -- an inherited real DB_DSN must
# not win over an explicit request for the synthetic ledger.
#
#   ./tl                    what state everything is in, and what is next
#   ./tl reconcile          signals -> proposals (writes a packet)
#   ./tl review             the human decision loop
#   ./tl ask "what is blocked"
#   ./tl --synth review     the same loop against the synthetic ledger
#   ./tl test               the suites, forced onto a scratch database
#
# Overridable: TL_USER, TL_EMBED_MODEL, TL_JUDGE_MODEL, TL_SCRATCH_DSN.

set -e
cd "$(dirname "$0")"

ME="$(whoami)"
PROFILE="real"

while [ $# -gt 0 ]; do
  case "$1" in
    --synth|--synthetic) PROFILE="synth"; shift ;;
    --real)              PROFILE="real";  shift ;;
    *) break ;;
  esac
done

# --synth assigns rather than defaults: an exported real DSN in the shell
# must not survive an explicit request for the synthetic board.
if [ "$PROFILE" = "synth" ]; then
  DB_DSN="${TL_SYNTH_DSN:-host=localhost port=5432 dbname=taskintel_synth user=$ME}"
  TL_INBOX="$HOME/task-ledger-synthetic-inbox"
else
  DB_DSN="${DB_DSN:-host=localhost port=5432 dbname=taskintel user=$ME}"
  TL_INBOX="${TL_INBOX:-$HOME/task-ledger-inbox}"
fi
# The dashboard's in-app profile switch reads this one; harmless elsewhere.
TL_SYNTH_DSN="${TL_SYNTH_DSN:-host=localhost port=5432 dbname=taskintel_synth user=$ME}"
export DB_DSN TL_INBOX TL_SYNTH_DSN

# Same courtesy as tl.bat: subcommands that shell out to psql should not
# fail over a variable this script can work out. PATH first here, since a
# unix box normally has the client tools on it.
if [ -z "${PGBIN:-}" ]; then
  if command -v psql >/dev/null 2>&1; then
    PGBIN="$(dirname "$(command -v psql)")"
  elif [ -x "$HOME/pgsql-portable/pgsql/bin/psql" ]; then
    PGBIN="$HOME/pgsql-portable/pgsql/bin"
  fi
  [ -n "${PGBIN:-}" ] && export PGBIN
fi

USER_ID="${TL_USER:-aad-dev}"
EMBED_MODEL="${TL_EMBED_MODEL:-nomic-embed-text}"
JUDGE_MODEL="${TL_JUDGE_MODEL:-mistral-small3.1}"

dbname_of() { echo "$1" | sed -n 's/.*dbname=\([^ ]*\).*/\1/p'; }
DBNAME="$(dbname_of "$DB_DSN")"

# Fail before the work, not during it.
preflight() {
  local out
  if ! out="$(python3 scripts/doctor.py 2>&1)"; then
    echo "$out"
    echo ""
    echo "tl: doctor failed -- '$1' was not run. Fix the above and retry."
    exit 1
  fi
}

banner() { echo "tl: profile=$PROFILE db=$DBNAME as=$USER_ID"; }

usage() {
  echo "tl -- one entrypoint for the daily loop."
  echo ""
  echo "  ./tl [--synth] <subcommand> [args]"
  echo ""
  echo "Sets DB_DSN and TL_INBOX when unset, so a fresh terminal needs no"
  echo "setup step. --synth targets the synthetic ledger. Every DB-touching"
  echo "subcommand runs doctor first and refuses to start if it fails."
  echo ""
  echo "subcommands:"
  echo "  up | doctor     preflight and current pipeline state (default)"
  echo "  env             print the exports, to paste into a bare shell"
  echo "  ollama          find and start the local model server"
  echo "  tick            the whole cadence: pool, reconcile everyone, deliver"
  echo "  reconcile       signals + board -> ReviewPacket (writes)"
  echo "  review          accept / edit / reject / defer the packet"
  echo "  digest          the week, addressed to a person"
  echo "  ask <question>  answers over confirmed state, with receipts"
  echo "  share <args>    grant / revoke / list access"
  echo "  dashboard       serve http://127.0.0.1:8090 (--blind --hands-on --picker)"
  echo "  extract <file>  one transcript -> typed signals (writes)"
  echo "  ingest <file>   a Copilot paste -> the inbox"
  echo "  transcripts <f> a Teams transcript (Word .txt) -> the inbox"
  echo "  quickscan <f>   what a transcript names + what is new (read-only)"
  echo "  brief [meeting] before the standup: what this room said + what became of it"
  echo "  recall <words>  who said that, when -- quoted, with receipts"
  echo "  handover <who>  everything one person carries, with history"
  echo "  kit             package the system + a ledger copy for a teammate"
  echo "  statusreport <f> --for <who>  a weekly status report -> their own claims"
  echo "  copilot <file>  build a Copilot-claims ledger beside yours, to compare"
  echo "  demo-set        generate the synthetic transcript demo set"
  echo "  demo-tx <src>   build the demo board from transcripts (writes)"
  echo "  score <dir>     extraction vs golden labels, measured"
  echo "  demo            seed a reviewable state, no Ollama needed"
  echo "  demo-check      drive the real app and assert the demo holds up"
  echo "  test            every suite, forced onto a scratch database"
  echo "  prompt          the Copilot prompt, ready to paste (--with <file>)"
  echo "  blind <people>  who sees which ledger under TL_BLIND=1 (--balance)"
  echo "  map             rebuild docs/system_map.html"
}

CMD="${1:-up}"
[ $# -gt 0 ] && shift

case "$CMD" in

  help|-h|--help) usage ;;

  env)
    echo "export DB_DSN=\"$DB_DSN\""
    echo "export TL_INBOX=\"$TL_INBOX\""
    echo "export TL_SYNTH_DSN=\"$TL_SYNTH_DSN\""
    ;;

  up|doctor)
    banner
    python3 scripts/doctor.py
    ;;

  reconcile)
    banner; preflight reconcile
    python3 reconciler/run_reconcile.py --user "$USER_ID" --dsn "$DB_DSN" \
      --embed-model "$EMBED_MODEL" --judge-model "$JUDGE_MODEL" --write "$@"
    ;;

  review)
    banner; preflight review
    python3 cli/review.py --as "$USER_ID" --dsn "$DB_DSN" "$@"
    ;;

  digest)
    banner; preflight digest
    python3 digest/run_digest.py --as "$USER_ID" --dsn "$DB_DSN" "$@"
    ;;

  ask)
    if [ $# -eq 0 ]; then
      echo "tl: ask needs a question, e.g. ./tl ask \"what is blocked\"" >&2
      exit 2
    fi
    banner; preflight ask
    python3 ask/run_ask.py --as "$USER_ID" --dsn "$DB_DSN" "$@"
    ;;

  share)
    banner; preflight share
    python3 cli/share.py --as "$USER_ID" --dsn "$DB_DSN" "${@:-list}"
    ;;

  dashboard)
    # --blind, because the alternative was an environment variable that
    # had to be set in THIS shell, before this command, every time -- and
    # forgetting it produces a session that runs perfectly and answers
    # nothing, since the arms were labelled all along. A setting that
    # decides whether an afternoon of five people's time is usable does
    # not belong in invisible shell state.
    for a in "$@"; do
      [ "$a" = "--blind" ] && export TL_BLIND=1
      [ "$a" = "--hands-on" ] && export TL_HANDS_ON=1
      [ "$a" = "--picker" ] && export TL_VIEWER_PICKER=1
    done
    banner; preflight dashboard
    echo "tl: http://127.0.0.1:8090  (ctrl-c to stop)"
    python3 integration/dashboard/dashboard_api.py "$@"
    ;;

  extract)
    if [ $# -eq 0 ]; then
      echo "tl: extract needs a transcript, e.g." >&2
      echo "    ./tl extract extraction/sample_transcript.json" >&2
      exit 2
    fi
    banner; preflight extract
    TRANSCRIPT="$1"; shift
    python3 extraction/run_local.py --transcript "$TRANSCRIPT" \
      --model "$JUDGE_MODEL" --write-db "$@"
    ;;

  ingest)
    if [ $# -eq 0 ]; then
      echo "tl: ingest needs a file, e.g. ./tl ingest paste.txt" >&2
      exit 2
    fi
    banner
    python3 scripts/copilot_ingest.py "$1" --out "$TL_INBOX" "${@:2}"
    ;;

  statusreport)
    if [ -z "$1" ]; then
      echo "tl: statusreport needs a file and a person, e.g." >&2
      echo "    ./tl statusreport report.txt --for jason-tjia" >&2
      exit 2
    fi
    python3 scripts/status_report_ingest.py "$@" --dsn "$DB_DSN"
    ;;

  copilot)
    if [ $# -eq 0 ]; then
      echo "tl: copilot needs the claims file, e.g." >&2
      echo "    ./tl copilot ~/copilot_claims_raw.txt" >&2
      exit 2
    fi
    banner
    python3 scripts/copilot_ledger.py "$@"
    ;;

  kit)
    banner
    python3 scripts/team_kit.py "$@"
    ;;

  brief)
    banner
    python3 scripts/brief.py "$@"
    ;;

  recall)
    if [ $# -eq 0 ]; then
      echo "tl: recall needs words, e.g. ./tl recall synthetic data" >&2
      exit 2
    fi
    banner
    python3 scripts/recall.py "$@"
    ;;

  handover)
    if [ $# -eq 0 ]; then
      echo "tl: handover needs a person, e.g. ./tl handover hari" >&2
      exit 2
    fi
    banner
    python3 scripts/handover.py "$@"
    ;;

  quickscan)
    # No preflight gate. It degrades to extraction-only without a database
    # and writes nothing either way, so refusing to start on a doctor
    # failure would withhold the one command that still works when the
    # ledger does not.
    if [ $# -eq 0 ]; then
      echo "tl: quickscan needs a transcript, e.g." >&2
      echo "    ./tl quickscan ~/transcripts_raw.txt" >&2
      exit 2
    fi
    banner
    python3 scripts/quickscan.py "$@"
    ;;

  transcripts)
    if [ $# -eq 0 ]; then
      echo "tl: transcripts needs a .txt, e.g." >&2
      echo "    ./tl transcripts ~/transcripts_raw.txt" >&2
      exit 2
    fi
    banner
    TX="$1"; shift
    python3 scripts/transcript_ingest.py "$TX" --out "$TL_INBOX" "$@"
    ;;

  demo-set)
    python3 testdata/synth_transcripts.py --ingest "$@"
    ;;

  demo-tx)
    # Its own database on purpose: this seeds decisions in other people's
    # names, which is defensible on invented work and on nothing else.
    if [ $# -eq 0 ]; then
      echo "tl: demo-tx needs a transcripts .txt or a window directory" >&2
      exit 2
    fi
    SRC="$1"; shift
    DEMO_DSN="${TL_DEMO_DSN:-host=localhost port=5432 dbname=taskintel_txdemo user=$ME}"
    echo "tl: demo board -> $(dbname_of "$DEMO_DSN")"
    python3 scripts/demo_transcript_build.py "$SRC" --dsn "$DEMO_DSN" "$@"
    ;;

  score)
    if [ $# -eq 0 ]; then
      echo "tl: score needs a corpus directory (with golden.json)" >&2
      exit 2
    fi
    CORPUS="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; shift
    ( cd testdata && python3 score_extraction.py --corpus "$CORPUS" \
        --model "$JUDGE_MODEL" "$@" )
    ;;

  demo)
    banner; preflight demo
    python3 scripts/demo_offline.py "$@"
    ;;

  demo-check)
    banner
    python3 scripts/demo_preflight.py "$@"
    ;;

  test)
    # run_tests.sh seeds tasks and accepts proposals through
    # apply_task_event -- it mutates whatever board it is pointed at.
    # Forced onto scratch here so a shell left exported at the real
    # ledger cannot turn a test run into a write against real data.
    DB_DSN="${TL_SCRATCH_DSN:-host=localhost port=5432 dbname=taskintel_scratch user=$ME}"
    if [ "$(dbname_of "$DB_DSN")" = "taskintel" ]; then
      echo "tl: refusing to run the suites against taskintel." >&2
      echo "    TL_SCRATCH_DSN must name a scratch database." >&2
      exit 1
    fi
    export DB_DSN
    echo "tl: suites against $(dbname_of "$DB_DSN")"
    ./run_tests.sh "$@"
    ;;

  ollama)
    command -v ollama >/dev/null || {
      echo "tl: ollama not on PATH -- brew install ollama, or say" >&2
      echo "    where it lives: export OLLAMA_EXE=/path/to/ollama" >&2
      exit 1
    }
    echo "tl: starting ollama serve (leave it running)"
    "${OLLAMA_EXE:-ollama}" serve
    ;;

  tick)
    banner; preflight tick
    python3 cadence/cadence_tick.py "$@"
    ;;

  prompt)
    python3 scripts/show_prompt.py "$@"
    ;;

  blind)
    python3 scripts/blind_map.py "$@"
    ;;

  map)
    python3 scripts/build_system_map.py "$@"
    ;;

  *)
    echo "tl: unknown subcommand '$CMD'" >&2
    echo "" >&2
    usage >&2
    exit 2
    ;;
esac
