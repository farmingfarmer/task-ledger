@echo off
set "PYTHONUTF8=1"
setlocal
cd /d "%~dp0extraction" && python test_extraction.py || exit /b 1
cd /d "%~dp0reconciler" && python test_reconciler.py || exit /b 1
if exist "%~dp0reconciler\test_supersede.py" ( cd /d "%~dp0reconciler" && python test_supersede.py || exit /b 1 )
cd /d "%~dp0cli" && python test_review.py || exit /b 1
cd /d "%~dp0cli" && python test_share.py || exit /b 1
cd /d "%~dp0digest" && python test_digest.py || exit /b 1
cd /d "%~dp0ask" && python test_ask.py || exit /b 1
cd /d "%~dp0eval" && python test_eval.py || exit /b 1
cd /d "%~dp0testdata" && python test_synth.py || exit /b 1
cd /d "%~dp0cadence" && python test_cadence.py || exit /b 1
cd /d "%~dp0integration\tests" && python test_dashboard.py || exit /b 1
where node >nul 2>&1 && node "%~dp0integration\dashboard\check_js.js" "%~dp0integration\dashboard\dashboard.html"
cd /d "%~dp0" && set "TL_MAP_OUT=%TEMP%\tl-system-map.html" && python scripts\build_system_map.py || exit /b 1
echo ALL SUITES COMPLETE
endlocal
