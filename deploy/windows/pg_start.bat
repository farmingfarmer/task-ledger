@echo off
"%USERPROFILE%\pgsql-portable\pgsql\bin\pg_ctl.exe" -D "%USERPROFILE%\pgdata-taskledger" -o "-p 5433" -l "%USERPROFILE%\pgdata-taskledger\pg.log" start
