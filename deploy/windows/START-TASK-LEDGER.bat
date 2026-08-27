@echo off
rem ============================================================
rem  Task ledger -- double-click to start.
rem
rem  Everything the kit's README used to ask a colleague to do by
rem  hand: find Postgres, create the database, load the ledger,
rem  install what Python needs, start the server, open the browser.
rem  A person evaluating this should be answering questions about
rem  their own meetings within a minute of double-clicking, not
rem  reading setup instructions.
rem
rem  Safe to run repeatedly: it creates and loads only what is
rem  missing, and never touches an existing database.
rem ============================================================
setlocal EnableDelayedExpansion

rem This file runs from two places and must work in both: inside a kit,
rem where the code sits in .\app and the ledger beside it, and inside a
rem repo checkout, where it lives in deploy\windows. Detect rather than
rem assume -- a launcher that only works from one of them is a launcher
rem that works on the builder's machine.
set "KITROOT=%~dp0"
if exist "%~dp0app\cadence\cadence_tick.py" (
  set "APPDIR=%~dp0app"
) else if exist "%~dp0..\..\cadence\cadence_tick.py" (
  set "APPDIR=%~dp0..\.."
  set "KITROOT=%~dp0..\..\"
) else (
  echo   [X] Cannot find the application files next to this launcher.
  echo       Keep this file in the folder it arrived in.
  pause
  exit /b 1
)
cd /d "%APPDIR%"

set "DBNAME=taskintel_demo"
set "PGPORT=5433"
set "PGDIR=%USERPROFILE%\pgsql-portable\pgsql"
set "PGDATA=%USERPROFILE%\pgdata-taskledger"
title Task ledger

echo.
echo   Task ledger -- starting up. This window can be minimised,
echo   but closing it stops the app.
echo.

rem ---- Python: the bundled runtime first ----------------------
rem A kit built with --with-runtime carries its own Python, so nothing
rem needs installing and nothing already on the machine is disturbed.
rem The bundled copy wins when present: a teammate's own Python may be
rem any version, with or without the packages, and debugging that on
rem someone else's laptop is exactly the errand this exists to avoid.
set "PY="
if exist "%KITROOT%python-runtime\python.exe" set "PY=%KITROOT%python-runtime\python.exe"
if not defined PY if exist "%USERPROFILE%\task-ledger-runtime\python.exe" set "PY=%USERPROFILE%\task-ledger-runtime\python.exe"
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo   [X] No Python found, and this kit did not come with one.
  echo.
  echo       Either ask for a kit built with --with-runtime, or
  echo       install Python 3.10+ from python.org, ticking
  echo       "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

rem ---- Postgres: prefer the portable copy, else anything on PATH
if exist "%KITROOT%postgres\bin\pg_ctl.exe" (
  set "PGBIN=%KITROOT%postgres\bin"
) else if exist "%PGDIR%\bin\pg_ctl.exe" (
  set "PGBIN=%PGDIR%\bin"
) else (
  for /f "delims=" %%P in ('where pg_ctl 2^>nul') do set "PGBIN=%%~dpP"
)
if not defined PGBIN (
  echo   [X] PostgreSQL was not found.
  echo.
  echo       Ask for a kit built with --with-postgres, which
  echo       carries its own copy, or unzip a portable PostgreSQL to:
  echo         %USERPROFILE%\pgsql-portable
  echo       then run this file again.
  echo.
  pause
  exit /b 1
)

rem ---- start the server if it is not already answering ---------
"%PGBIN%\pg_isready.exe" -p %PGPORT% >nul 2>&1
if errorlevel 1 (
  echo   starting the database...
  if not exist "%PGDATA%" (
    "%PGBIN%\initdb.exe" -D "%PGDATA%" -U %USERNAME% -E UTF8 >nul 2>&1
  )
  "%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -o "-p %PGPORT%" -l "%PGDATA%\pg.log" start >nul 2>&1
  rem give it a moment to come up before anything asks it a question
  for /l %%i in (1,1,10) do (
    "%PGBIN%\pg_isready.exe" -p %PGPORT% >nul 2>&1
    if not errorlevel 1 goto :pgup
    ping -n 2 127.0.0.1 >nul
  )
  echo   [X] The database would not start. Details:
  echo       %PGDATA%\pg.log
  pause
  exit /b 1
)
:pgup

rem ---- create + load the ledger, once --------------------------
"%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME% -c "SELECT 1" >nul 2>&1
if errorlevel 1 (
  echo   preparing your copy of the ledger...
  "%PGBIN%\createdb.exe" -p %PGPORT% %DBNAME% >nul 2>&1
  if exist "%KITROOT%ledger.sql" (
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f "%KITROOT%ledger.sql" >nul 2>&1
  ) else (
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f storage\schema.sql >nul 2>&1
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f access\access_control.sql >nul 2>&1
  )
)

rem ---- Python packages, quietly, only if absent ----------------
"%PY%" -c "import psycopg2, flask, yaml, jsonschema" >nul 2>&1
if errorlevel 1 (
  echo   installing what Python needs ^(one time, about a minute^)...
  "%PY%" -m pip install --quiet --user psycopg2-binary flask pyyaml jsonschema >nul 2>&1
)

set "DB_DSN=host=localhost port=%PGPORT% dbname=%DBNAME% user=%USERNAME%"
set "TL_VIEWER_PICKER=1"
set "TL_HANDS_ON=1"

echo.
echo   Ready. Opening http://127.0.0.1:8090 in your browser.
echo   Pick your name when it asks.
echo.
start "" http://127.0.0.1:8090
"%PY%" integration\dashboard\dashboard_api.py
