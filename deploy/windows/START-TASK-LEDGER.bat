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
cd /d "%~dp0..\.."

set "DBNAME=taskintel_demo"
set "PGPORT=5433"
set "PGDIR=%USERPROFILE%\pgsql-portable\pgsql"
set "PGDATA=%USERPROFILE%\pgdata-taskledger"
title Task ledger

echo.
echo   Task ledger -- starting up. This window can be minimised,
echo   but closing it stops the app.
echo.

rem ---- Python -------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
  echo   [X] Python was not found.
  echo.
  echo       Install Python 3.10 or newer from python.org, tick
  echo       "Add python.exe to PATH" during setup, then run this
  echo       file again.
  echo.
  pause
  exit /b 1
)

rem ---- Postgres: prefer the portable copy, else anything on PATH
if exist "%PGDIR%\bin\pg_ctl.exe" (
  set "PGBIN=%PGDIR%\bin"
) else (
  for /f "delims=" %%P in ('where pg_ctl 2^>nul') do set "PGBIN=%%~dpP"
)
if not defined PGBIN (
  echo   [X] PostgreSQL was not found.
  echo.
  echo       Ask whoever sent you this kit for the portable
  echo       PostgreSQL folder, unzip it to:
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
  if exist "%~dp0..\..\ledger.sql" (
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f "%~dp0..\..\ledger.sql" >nul 2>&1
  ) else (
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f storage\schema.sql >nul 2>&1
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f access\access_control.sql >nul 2>&1
  )
)

rem ---- Python packages, quietly, only if absent ----------------
python -c "import psycopg2, flask, yaml, jsonschema" >nul 2>&1
if errorlevel 1 (
  echo   installing what Python needs ^(one time, about a minute^)...
  python -m pip install --quiet --user psycopg2-binary flask pyyaml jsonschema >nul 2>&1
)

set "DB_DSN=host=localhost port=%PGPORT% dbname=%DBNAME% user=%USERNAME%"
set "TL_VIEWER_PICKER=1"
set "TL_HANDS_ON=1"

echo.
echo   Ready. Opening http://127.0.0.1:8090 in your browser.
echo   Pick your name when it asks.
echo.
start "" http://127.0.0.1:8090
python integration\dashboard\dashboard_api.py
