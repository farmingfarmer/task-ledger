@echo off
setlocal
:: Task Ledger no-admin Windows setup. Idempotent: safe to rerun.
:: Assumes portable Postgres binaries at %USERPROFILE%\pgsql-portable\pgsql
:: (the layout already in use on this machine) and python on PATH.

set PGBIN=%USERPROFILE%\pgsql-portable\pgsql\bin
set PGDATA=%USERPROFILE%\pgdata-taskledger
set PGPORT=5433
set REPO=%~dp0..\..
set DSN=host=localhost port=%PGPORT% dbname=taskintel user=%USERNAME%

if not exist "%PGBIN%\pg_ctl.exe" (
  echo [setup] portable postgres not found at %PGBIN%
  echo         unzip the PG portable build there first, then rerun.
  exit /b 1
)

if not exist "%PGDATA%\PG_VERSION" (
  echo [setup] initializing data dir %PGDATA%
  "%PGBIN%\initdb.exe" -D "%PGDATA%" -U %USERNAME% -E UTF8 || exit /b 1
)

"%PGBIN%\pg_ctl.exe" -D "%PGDATA%" status >nul 2>&1
if errorlevel 1 (
  echo [setup] starting postgres on port %PGPORT%
  "%PGBIN%\pg_ctl.exe" -D "%PGDATA%" -o "-p %PGPORT%" -l "%PGDATA%\pg.log" start || exit /b 1
  timeout /t 2 /nobreak >nul
)

"%PGBIN%\psql.exe" -p %PGPORT% -U %USERNAME% -d taskintel -c "SELECT 1" >nul 2>&1
if errorlevel 1 (
  echo [setup] creating database taskintel
  "%PGBIN%\createdb.exe" -p %PGPORT% -U %USERNAME% taskintel || exit /b 1
  echo [setup] loading schema + access control
  "%PGBIN%\psql.exe" -p %PGPORT% -U %USERNAME% -d taskintel -v ON_ERROR_STOP=1 -f "%REPO%\storage\schema.sql" || exit /b 1
  "%PGBIN%\psql.exe" -p %PGPORT% -U %USERNAME% -d taskintel -v ON_ERROR_STOP=1 -f "%REPO%\storage\validate_storage.sql" || exit /b 1
  "%PGBIN%\psql.exe" -p %PGPORT% -U %USERNAME% -d taskintel -v ON_ERROR_STOP=1 -f "%REPO%\access\access_control.sql" || exit /b 1
  "%PGBIN%\psql.exe" -p %PGPORT% -U %USERNAME% -d taskintel -v ON_ERROR_STOP=1 -f "%REPO%\access\validate_access.sql" || exit /b 1
) else (
  echo [setup] database taskintel already present
)

echo [setup] python deps (user scope)
python -m pip install --user -q psycopg2-binary jsonschema pyyaml flask || exit /b 1

echo [setup] offline suites
set DB_DSN=%DSN%
cd /d "%REPO%\extraction" && python test_extraction.py || exit /b 1
cd /d "%REPO%\reconciler" && python test_reconciler.py || exit /b 1
cd /d "%REPO%\cli"        && python review.py --as aad-dev --dsn "%DSN%" --list

echo.
echo [setup] done. dashboard: run_dashboard.bat  -^>  http://127.0.0.1:8090
echo [setup] cadence: see WINDOWS_PORT.md for the schtasks command
endlocal
