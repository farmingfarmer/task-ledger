@echo off
set "PYTHONUTF8=1"
rem ============================================================
rem  Task ledger -- load this kit's NEWER data, replacing your
rem  local copy. Double-click only when you want fresh meetings.
rem
rem  The normal launcher deliberately KEEPS your local ledger so
rem  your answers survive kit upgrades. The cost is freshness: a
rem  new kit's newer meetings never load while the old copy still
rem  answers. This file is the other side of that choice, made
rem  explicitly: it drops the local copy and lets the launcher
rem  rebuild from this kit's dumps. Answers you recorded on the
rem  OLD copy are erased with it -- that is what "replace" means,
rem  and it says so before doing anything.
rem ============================================================
setlocal
set "PGPORT=5433"
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
set /p OK="  Type YES to continue: "
if /i not "%OK%"=="YES" (
  echo   Nothing done.
  pause
  exit /b 0
)
"%PGBIN%\dropdb.exe" -p %PGPORT% --if-exists taskintel_demo
"%PGBIN%\dropdb.exe" -p %PGPORT% --if-exists taskintel_demo_b
echo   Local copy removed. Starting the app -- it will rebuild from
echo   this kit's data ^(watch the [time] stamps^).
call "%~dp0START-TASK-LEDGER.bat"
