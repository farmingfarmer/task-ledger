@echo off
set "PYTHONUTF8=1"
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

rem ---- nested bundles: unpack once, locally --------------------
rem The Python and Postgres bundles are ~30,000 small files. Shipped
rem loose inside the kit ZIP, Windows Explorer extracts them one at a
rem time while Defender scans each -- no progress bar, up to an hour,
rem and it reads as a frozen laptop; the first teammate to receive a
rem kit reported exactly that. So the kit carries them as INNER zips
rem (the outer ZIP is then a dozen files and extracts instantly), and
rem this launcher unpacks them here, once, with bsdtar -- built into
rem Windows 10+, immune to both slow paths.
if not exist "%KITROOT%python-runtime\python.exe" if exist "%KITROOT%python-runtime.zip" (
  echo   one-time setup: unpacking Python ^(about a minute^)...
  tar -xf "%KITROOT%python-runtime.zip" -C "%KITROOT%" || (
    echo   [X] Could not unpack python-runtime.zip. Details above.
    pause
    exit /b 1
  )
)
if not exist "%KITROOT%postgresin\pg_ctl.exe" if exist "%KITROOT%postgres.zip" (
  echo   one-time setup: unpacking the database ^(about a minute^)...
  tar -xf "%KITROOT%postgres.zip" -C "%KITROOT%" || (
    echo   [X] Could not unpack postgres.zip. Details above.
    pause
    exit /b 1
  )
)

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

rem ---- the ledger: verify it works, rebuild it if it does not ----
rem
rem This used to ask "does the database exist?" and load the dump only
rem when it did not. Two ways that fails, both seen: a database left
rem over from an earlier run keeps an OLDER SCHEMA, and restoring a dump
rem on top of existing tables half-succeeds -- CREATE TABLE errors,
rem COPY fails for any column the old table lacks, and psql still exits
rem 0. Every word of that was going to >nul, so the app started against
rem a database missing columns it needs and the first thing the person
rem saw was a wall of UndefinedColumn tracebacks.
rem
rem So: ask whether the database actually answers the questions the app
rem asks. If it cannot, rebuild it from scratch -- never on top -- and
rem keep the log.
set "SETUPLOG=%KITROOT%setup-log.txt"
set "DBOK=1"
"%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME% -tAc "SELECT author_ref FROM signals LIMIT 1" >nul 2>&1 || set "DBOK="
if defined DBOK "%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME% -tAc "SELECT expires_at FROM v_current_grants LIMIT 1" >nul 2>&1 || set "DBOK="
if defined DBOK for /f "usebackq delims=" %%N in (`"%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME% -tAc "SELECT count(*) FROM signals" 2^>nul`) do if "%%N"=="0" set "DBOK="

if not defined DBOK (
  echo   preparing your copy of the ledger...
  rem Dropped, not reused: a half-built database is the thing being fixed.
  "%PGBIN%\dropdb.exe" -p %PGPORT% --if-exists %DBNAME% >"%SETUPLOG%" 2>&1
  "%PGBIN%\createdb.exe" -p %PGPORT% %DBNAME% >>"%SETUPLOG%" 2>&1
  if exist "%KITROOT%ledger.sql" (
    "%PGBIN%\psql.exe" -q -v ON_ERROR_STOP=1 -p %PGPORT% -d %DBNAME% -f "%KITROOT%ledger.sql" >>"%SETUPLOG%" 2>&1
  ) else (
    "%PGBIN%\psql.exe" -q -v ON_ERROR_STOP=1 -p %PGPORT% -d %DBNAME% -f storage\schema.sql >>"%SETUPLOG%" 2>&1
    "%PGBIN%\psql.exe" -q -v ON_ERROR_STOP=1 -p %PGPORT% -d %DBNAME% -f access\access_control.sql >>"%SETUPLOG%" 2>&1
  )

  rem Prove it before handing over a browser window.
  set "DBOK=1"
  "%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME% -tAc "SELECT author_ref FROM signals LIMIT 1" >nul 2>&1 || set "DBOK="
  if not defined DBOK (
    echo.
    echo   [X] The ledger could not be prepared, so the app would only
    echo       show errors. Nothing here is your fault.
    echo.
    echo       What went wrong is written to:
    echo         %SETUPLOG%
    echo       Send that file to whoever gave you this kit.
    echo.
    pause
    exit /b 1
  )
)

rem ---- Python packages, quietly, only if absent ----------------
"%PY%" -c "import psycopg2, flask, yaml, jsonschema" >nul 2>&1
if errorlevel 1 (
  echo   installing what Python needs ^(one time, about a minute^)...
  "%PY%" -m pip install --quiet --user psycopg2-binary flask pyyaml jsonschema >nul 2>&1
)

rem A database that exists but holds nothing is the failure that shipped
rem once already: the launcher skips loading when the database is there,
rem so an empty one from an earlier run is never filled, and the app
rem opens with no meetings in it. Check the content, not the existence.
for /f "usebackq delims=" %%N in (`"%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME% -tAc "SELECT count(*) FROM signals" 2^>nul`) do set "SIGCOUNT=%%N"
if "%SIGCOUNT%"=="0" (
  if exist "%KITROOT%ledger.sql" (
    echo   the ledger looked empty -- loading it now...
    "%PGBIN%\psql.exe" -q -p %PGPORT% -d %DBNAME% -f "%KITROOT%ledger.sql" >nul 2>&1
  ) else (
    echo.
    echo   [!] This copy of the ledger contains no meetings.
    echo       Nothing is broken, but there is nothing to review either.
    echo       Ask whoever sent you this kit to rebuild it.
    echo.
  )
)

set "DB_DSN=host=localhost port=%PGPORT% dbname=%DBNAME% user=%USERNAME%"
set "TL_VIEWER_PICKER=1"
set "TL_HANDS_ON=1"
set "TL_SURVEY_DIR=%KITROOT%my-answers"
if exist "%KITROOT%kit-config.json" set "TL_KIT_CONFIG=%KITROOT%kit-config.json"

rem ---- the second ledger, if this kit carries one ----
rem
rem Same meetings, different extractor. Prepared exactly like the first
rem and by the same rules: never restored on top of an existing database,
rem proven with the query the app asks before it is offered.
rem
rem If it is not here, or will not build, the app runs single-ledger and
rem nothing about the session changes. A comparison that half-works is
rem worse than no comparison, because the person doing it cannot tell
rem which half they are looking at.
if exist "%KITROOT%ledger-b.sql" (
  set "DB_B_OK=1"
  "%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME%_b -tAc "SELECT author_ref FROM signals LIMIT 1" >nul 2>&1 || set "DB_B_OK="
  if defined DB_B_OK for /f "usebackq delims=" %%N in (`"%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME%_b -tAc "SELECT count(*) FROM signals" 2^>nul`) do if "%%N"=="0" set "DB_B_OK="
  if not defined DB_B_OK (
    echo   preparing the second ledger...
    "%PGBIN%\dropdb.exe" -p %PGPORT% --if-exists %DBNAME%_b >>"%SETUPLOG%" 2>&1
    "%PGBIN%\createdb.exe" -p %PGPORT% %DBNAME%_b >>"%SETUPLOG%" 2>&1
    "%PGBIN%\psql.exe" -q -v ON_ERROR_STOP=1 -p %PGPORT% -d %DBNAME%_b -f "%KITROOT%ledger-b.sql" >>"%SETUPLOG%" 2>&1
    set "DB_B_OK=1"
    "%PGBIN%\psql.exe" -p %PGPORT% -d %DBNAME%_b -tAc "SELECT author_ref FROM signals LIMIT 1" >nul 2>&1 || set "DB_B_OK="
  )
  if defined DB_B_OK (
    set "TL_COPILOT_DSN=host=localhost port=%PGPORT% dbname=%DBNAME%_b user=%USERNAME%"
    set "TL_BLIND=1"
    rem The salt shipped with the kit, so every machine assigns the same
    rem person the same arm and every survey file means the same thing
    rem by "Ledger A". Without the file the shipped default applies,
    rem which is still consistent across kits -- just unchecked.
    if exist "%KITROOT%blind-salt.txt" (
      set /p TL_BLIND_SALT=<"%KITROOT%blind-salt.txt"
    )
  ) else (
    echo   [!] The second ledger would not prepare; running with one.
    echo       Details: %SETUPLOG%
  )
)

rem Only one Task Ledger can serve this computer at a time. If another
rem is already listening, starting this one FAILS -- but the browser
rem window would have opened first and been answered by the OLD server,
rem so five different kits opened in a row all show the first kit's
rem greeting, and the only clue is a bind error scrolling past in a
rem console nobody reads. Check first, refuse in words, open nothing.
netstat -ano | findstr /C:":8090 " | findstr "LISTENING" >nul && (
  echo.
  echo   [!] A Task Ledger is already open on this computer.
  echo       Close its black window ^(the one from the kit you opened
  echo       first^), then double-click this file again.
  echo.
  pause
  exit /b 1
)

echo.
echo   Ready. Opening http://127.0.0.1:8090 in your browser.
echo.
start "" http://127.0.0.1:8090
"%PY%" integration\dashboard\dashboard_api.py
