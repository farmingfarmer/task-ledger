@echo off
set DB_DSN=host=localhost port=5433 dbname=taskintel user=%USERNAME%
cd /d "%~dp0..\..\integration\dashboard"
python dashboard_api.py
