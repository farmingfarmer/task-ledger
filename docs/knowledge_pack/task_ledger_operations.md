---
id: task_ledger_operations
display_name: Task Ledger Operations
aliases: [task ledger commands, task ledger how to run]
type: documentation
status: active
owner: Alex
related: [task_ledger]
---

# Task Ledger Operations

Repository: github.com/farmingfarmer/task-ledger (private), local at
~/dev/task-ledger on the Mac (source of truth) and
C:\Users\NE1002766\dev\task-ledger on the work ThinkPad. Mac database:
Homebrew PostgreSQL 16, port 5432, database taskintel, DSN
host=localhost port=5432 dbname=taskintel user=$(whoami). Windows:
portable PostgreSQL 18.4 at %USERPROFILE%\pgsql-portable, port 5433,
data dir %USERPROFILE%\pgdata-taskledger.

Daily rituals. Full test suite: ./run_tests.sh from the repo root (runs
every suite plus the drift-refusing atlas build). Preflight any machine:
python3 scripts/doctor.py (checks python, deps, files, database, schema,
pool detail, postgres version; Ollama absence is a warning, not a
failure). Seed a demo without Ollama: python3 scripts/demo_offline.py
then review with python3 cli/review.py --as aad-dev and view with the
dashboard at http://127.0.0.1:8090 (the explicit http matters on
Windows). Rebuild the atlas after any code change: python3
scripts/build_system_map.py; it fails loudly on map or narrative drift.
Rebuild the presenter companion: python3 scripts/build_presenter.py.
Cadence: cadence/install_cadence.sh install puts a launchd agent at
weekdays 16:45; run-now kicks a tick; logs at
~/Library/Logs/task-ledger/cadence.log. Windows cadence uses schtasks
per deploy/windows/WINDOWS_PORT.md.

Evaluation: python3 testdata/synthesize.py --seed 7 --days 3 --out
corpus then python3 testdata/score_extraction.py --corpus corpus --model
mistral-small3.1. Costs: zero marginal inference cost; everything runs
on local Ollama models (mistral-small3.1, nomic-embed-text). Tenant
integration lives in integration/tenant/ with TENANT_SETUP.md covering
tenant acquisition (M365 dev program eligibility is restricted;
Business Standard trial is the reliable path), Entra app registration,
Mail.Read application and Chat.Read delegated permissions, the webhook
host plus tunnel, and the Teams protected-API request form that must be
filed early because approval takes weeks.
