@echo off
REM Three ledgers, one dashboard. The switch renders only for the ones
REM whose database exists -- create them first (see PROFILES.md) or the
REM extra buttons will point at nothing.
set DB_DSN=host=localhost port=5433 dbname=taskintel user=%USERNAME%
set TL_SYNTH_DSN=host=localhost port=5433 dbname=taskintel_synth user=%USERNAME%
set TL_COPILOT_DSN=host=localhost port=5433 dbname=taskintel_copilot user=%USERNAME%
REM A restored demo ledger has every board event stamped at restore time,
REM so at the 7-day default nothing is ever quiet and that half of the
REM nudge popup renders empty.
set TL_QUIET_DAYS=0
set TL_VIEWER_PICKER=1
cd /d "%~dp0..\..\integration\dashboard"
python dashboard_api.py
