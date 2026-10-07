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
  "%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -o "-p %PGPORT%" -l "%PGDATA%\pg.log" start >nul 2>&1
  for /l %%i in (1,1,10) do (
    "%PGBIN%\pg_isready.exe" -p %PGPORT% >nul 2>&1
    if not errorlevel 1 goto :srvup
    ping -n 2 127.0.0.1 >nul
  )
  echo   [X] The server would not start. Details: %PGDATA%\pg.log
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
