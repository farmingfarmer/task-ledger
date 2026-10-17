@echo off
set "PYTHONUTF8=1"
rem ============================================================
rem  Task ledger -- load this kit's NEWER data, replacing your
rem  local copy. Double-click only when you want fresh meetings.
rem
rem  v2, after the first teammate to run v1 met a silent cursor:
rem  every step now says what it is doing WITH a timestamp, the
rem  database server is started if it is not up (you cannot drop
rem  a database nobody is serving), and lingering connections are
rem  closed first (a database still being read refuses to drop,
rem  and v1 would have shown that error only if you knew to wait
rem  for it). If this window ever looks frozen: click it once and
rem  press Enter -- clicking a console pauses it, a Windows
rem  behavior nothing here can disable from inside.
rem ============================================================
setlocal
set "PGPORT=5433"
set "PGDATA=%USERPROFILE%\pgdata-taskledger"
rem On a FRESH extract the database tools are still inside
rem postgres.zip -- the normal launcher unpacks them on first run,
rem and the first teammate to follow "extract, then refresh" met a
rem refusal for exactly that reason. Unpack here too; order of the
rem two launchers stops mattering.
if not exist "%~dp0postgres\bin\dropdb.exe" if not exist "%~dp0postgres\pgsql\bin\dropdb.exe" if exist "%~dp0postgres.zip" (
  echo   [%TIME%] one-time setup: unpacking the database tools.
  echo   File names scroll past while it works -- that is normal.
  tar -xvf "%~dp0postgres.zip" -C "%~dp0." || (
    echo   [X] Could not unpack postgres.zip. Details above.
    pause
    exit /b 1
  )
)
if exist "%~dp0postgres\bin\dropdb.exe" (
  set "PGBIN=%~dp0postgres\bin"
) else if exist "%~dp0postgres\pgsql\bin\dropdb.exe" (
  set "PGBIN=%~dp0postgres\pgsql\bin"
) else if exist "%USERPROFILE%\pgsql-portable\pgsql\bin\dropdb.exe" (
  set "PGBIN=%USERPROFILE%\pgsql-portable\pgsql\bin"
) else (
  echo   [X] Could not find the kit's database tools next to this file.
  pause
  exit /b 1
)
echo.
echo   This replaces your local ledger copy with this kit's newer one.
echo   Everything you answered on the OLD copy is erased with it.
echo   Your feedback files in my-answers are NOT touched.
echo.
set /p OK="  Type YES and press Enter to continue: "
if /i not "%OK%"=="YES" (
  echo   Nothing done.
  pause
  exit /b 0
)
echo   [%TIME%] checking the database server...
"%PGBIN%\pg_isready.exe" -p %PGPORT% >nul 2>&1
if errorlevel 1 (
  echo   [%TIME%] server not running -- starting it first; a database
  echo            nobody is serving cannot be dropped
  rem Started DETACHED via Start-Process: a Postgres launched from
rem this console dies when the console closes -- 0xC000013A in
rem the first teammate's log, mid-checkpoint, because the
rem instructions said "close the black window" and on Windows
rem that killed the database with it. Hidden own console now:
rem closing THIS window stops only the app, never the data.
rem The port rides the PGPORT environment variable, which
rem postgres reads natively -- no -o quoting gymnastics.
powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath '%PGBIN%\pg_ctl.exe' -ArgumentList '-D','%PGDATA%','-l','%PGDATA%\pg.log','start'" >nul 2>&1
  rem up to ~60s: after an unclean stop the server replays its
  rem write-ahead log before answering; that is recovery, not
  rem a hang -- say so instead of giving up at ten seconds.
  for /l %%i in (1,1,30) do (
    if %%i==10 echo   [%TIME%] still starting -- recovering after an unclean stop is normal, give it a minute
    "%PGBIN%\pg_isready.exe" -p %PGPORT% >nul 2>&1
    if not errorlevel 1 goto :srvup
    ping -n 2 127.0.0.1 >nul
  )
  echo   [X] The server would not start. Details: %PGDATA%\pg.log
  echo   ---- why, from the last lines of pg.log ----
  powershell -NoProfile -Command "Get-Content -Tail 25 '%PGDATA%\pg.log'"
  echo   -------------------------------------------
  echo   Photograph this window -- it is the complete report.
  pause
  exit /b 1
)
:srvup
echo   [%TIME%] closing any sessions still reading the old copy...
"%PGBIN%\psql.exe" -p %PGPORT% -d postgres -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('taskintel_demo','taskintel_demo_b')" >nul 2>&1
echo   [%TIME%] removing the old copy...
"%PGBIN%\dropdb.exe" -p %PGPORT% --if-exists taskintel_demo
"%PGBIN%\dropdb.exe" -p %PGPORT% --if-exists taskintel_demo_b
echo   [%TIME%] old copy removed. Starting the app -- it rebuilds from
echo            this kit's data; watch the timestamps.
call "%~dp0START-TASK-LEDGER.bat"
rem The launcher titles the console "Task ledger" for its own window;
rem when the refresher ran in a caller's terminal, that name outlives
rem the run and confuses anything that identifies windows by title
rem (it cost an evening of remote-hands debugging). Give the name back.
title Command Prompt
