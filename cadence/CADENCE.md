# Cadence: The System Runs Itself

`cadence/` makes the local loop autonomous: a launchd agent fires **every
weekday at 4:45 PM local time** (launchd follows the Mac's clock, so
PST/PDT transitions are automatic), runs one tick, and raises a macOS
notification only when proposals actually await review. Files:
`cadence_tick.py` (the tick), `install_cadence.sh` (plist render +
bootstrap + status/run-now/uninstall).

## One tick

1. **Inbox.** Every `*.json` in `~/task-ledger-inbox` (the `run_local`
   transcript format: `job` with `window_start`/`window_end`, `directory`,
   `items` with `resource_id`/`author_name`/`text`) is extracted via Ollama
   and written to the signal store, then archived to `processed/`. Ollama
   unreachable -> files stay put for the next tick; extraction needs a
   model and loses nothing by waiting. This folder is the pre-Mnemo input
   path: drop a transcript, the system ingests it at 4:45.
2. **Reconcile.** Ollama up: real embeddings + judge. Down: offline
   TokenEmbedder, no judge -- gates still run, judge-band and closures
   hold. Either way `write_packet` supersedes any open packet (undecided
   signals rejoin the pool) and empty packets insert nothing.
3. **Notify.** Any undecided proposals for the user -> one notification
   pointing at the review CLI. Zero -> zero noise. A down Ollama or an
   empty day exits 0: degraded is not failure.

Logs: `~/Library/Logs/task-ledger/cadence.log`, timestamped.

## Install

Edit the CONFIG block at the top of `install_cadence.sh` if the defaults
(user `aad-dev`, Homebrew Postgres DSN, `~/task-ledger-inbox`, 16:45) need
changing, then:

```zsh
cd ~/dev/task-ledger/cadence
chmod +x install_cadence.sh
./install_cadence.sh install
./install_cadence.sh status
```

`run-now` kicks a tick immediately (the honest smoke test); `uninstall`
removes it. Reinstall after any CONFIG edit.

Two launchd realities worth knowing: agents do not source your shell, so
the plist pins an absolute python3 and a PATH containing `/opt/homebrew/bin`
(psql, ollama); and if the Mac is asleep at 16:45, launchd runs the tick on
wake rather than skipping the day.

## Validation

Seven live checks in the sandbox (launchd itself can't run there; the tick
logic can): degraded tick leaves the inbox intact, falls back offline, and
exits 0; a scripted-LLM tick extracts one signal from a dropped transcript,
archives the file, and the no-match closure is HELD rather than guessed;
the held signal persists into the next tick's pool; empty re-tick is
silent and clean. The installer is syntax-checked and the rendered plist
XML-validated (five weekday entries at 16:45, env and logging present) --
`plutil -lint` runs for real at install time on the Mac.

## The loop this closes

Weekday afternoons: transcript lands in the inbox (today by hand, soon via
the Mnemo bridge) -> 4:45 tick extracts, reconciles, supersedes stale work
-> notification if and only if something needs you -> two minutes in
`review.py` -> decisions feed the reject-reason histograms the calibration
sweep is waiting on. The system now improves by being used, on a schedule,
without being remembered.
