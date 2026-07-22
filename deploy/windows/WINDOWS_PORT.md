# Windows Port (No-Admin ThinkPad)

Ports the full local system to a locked-down Windows machine using only
per-user, no-admin installs: portable Postgres (the pattern already proven
on this machine), a user-level Python, and Task Scheduler for cadence.
Everything in the repo is already cross-platform Python; the port is
environment, not code -- with one deliberate exception (notifications)
and one decision to make (where inference runs).

## Phase A -- stack without inference (do this first)

Validates storage, access, reconciler-offline, review CLI, and dashboard
on the ThinkPad using signals already produced elsewhere or offline mode.
No Ollama required.

```bat
:: from the repo checkout (git clone or zip copy to %USERPROFILE%\dev\task-ledger)
cd %USERPROFILE%\dev\task-ledger\deploy\windows
setup_windows.bat
```

`setup_windows.bat` does, idempotently: init a portable-PG data dir
(`%USERPROFILE%\pgdata-taskledger`, port 5433) if absent; start it; create
db `taskintel`; load `storage\schema.sql`, `storage\validate_storage.sql`,
`access\access_control.sql`, `access\validate_access.sql`; pip-install
`psycopg2-binary jsonschema pyyaml flask` for the current user; run the
offline test suites. Expected: all storage/access assertions, 18 extraction
checks (pg section live), 21+ reconciler checks live, review CLI list mode.

Dashboard: `run_dashboard.bat`, then browse **http://127.0.0.1:8090**
explicitly -- Windows browsers upgrade bare hostnames to https and produce
"Bad request version" against Flask (seen previously on this machine).

## Phase B -- inference: the decision

Extraction and judging need a model. Two viable shapes; pick per context:

**B1. Ollama portable on the ThinkPad (self-contained, slower).**
Ollama ships a no-admin zip (`ollama-windows-amd64.zip`); run
`ollama.exe serve` from a user dir. CPU-only on a ThinkPad means
mistral-small3.1 (24B) is impractical; the realistic models are
llama3.1:8b or qwen2.5:7b -- *different models than the ones the eval
numbers were measured on*. The scorer ports with the repo, so the honest
move is: generate the corpora on the ThinkPad (`testdata\synthesize.py`,
same seeds) and re-score with the small model before trusting it. Expect
lower attribution; the gates absorb it, but measure, don't assume.

**B2. Mac as inference server (fast, network-dependent).**
On the Mac: `OLLAMA_HOST=0.0.0.0 ollama serve`, then point the ThinkPad
at `http://<mac-ip>:11434` via `OLLAMA_HOST_URL` / `--ollama-host`.
Right answer on a home network. **On the hospital network, do not do this
without IT's blessing** -- cross-device inference traffic on a clinical
network is exactly the kind of thing the eventual approval conversation
should cover, not preempt. Same caution applies to corpus content: keep
synthetic corpora synthetic on the work machine; no real meeting text
enters this system until the system is approved to hold it.

Recommendation: Phase A now, B1 with re-scoring for self-contained demos,
B2 only at home. The demo for IT works fine in Phase A + pre-produced
signals: the review-to-provenance story doesn't need live inference.

## Cadence on Windows

`cadence_tick.py` runs unmodified (its notifier logs instead of popping on
non-macOS). Task Scheduler replaces launchd -- per-user tasks need no
admin:

```bat
schtasks /create /tn "TaskLedgerCadence" /sc weekly ^
  /d MON,TUE,WED,THU,FRI /st 16:45 ^
  /tr "cmd /c cd /d %USERPROFILE%\dev\task-ledger && set DB_DSN=host=localhost port=5433 dbname=taskintel user=%USERNAME%&& set TL_USER=aad-dev&& python cadence\cadence_tick.py >> %USERPROFILE%\task-ledger-cadence.log 2>&1"
```

`schtasks /run /tn "TaskLedgerCadence"` is the run-now;
`/delete /tn "TaskLedgerCadence" /f` uninstalls. Local time, so 16:45
tracks the machine's timezone the same way launchd does. For a visible
popup instead of log-only, `windows_toast.ps1` (included) shows a toast
via the Windows Runtime from the user session; wire it by appending
`&& powershell -ExecutionPolicy Bypass -File deploy\windows\windows_toast.ps1`
to the task command -- but treat it as optional polish; the log is the
contract.

## Postgres service management

Portable PG has no service; `pg_start.bat` / `pg_stop.bat` wrap pg_ctl,
and `setup_windows.bat` starts it if down. If the machine reboots, run
`pg_start.bat` (or add it to the scheduled task command ahead of the
tick, which the generated command in setup's output does).

## What is deliberately NOT ported

launchd plists, macOS notifications, Homebrew paths -- all isolated in
`cadence/install_cadence.sh`, which is macOS-only by design. The Python
tick is the shared core.
