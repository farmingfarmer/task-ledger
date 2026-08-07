@echo off
setlocal
cd /d "%~dp0extraction" && python test_extraction.py || exit /b 1
cd /d "%~dp0reconciler" && python test_reconciler.py || exit /b 1
if exist "%~dp0reconciler\test_supersede.py" ( cd /d "%~dp0reconciler" && python test_supersede.py || exit /b 1 )
cd /d "%~dp0cli" && python test_review.py || exit /b 1
cd /d "%~dp0cli" && python test_share.py || exit /b 1
cd /d "%~dp0digest" && python test_digest.py || exit /b 1
cd /d "%~dp0ask" && python test_ask.py || exit /b 1
cd /d "%~dp0eval" && python test_eval.py || exit /b 1
cd /d "%~dp0integration\tests" && python test_dashboard.py || exit /b 1
cd /d "%~dp0" && python scripts\build_system_map.py || exit /b 1
echo ALL SUITES COMPLETE
endlocal
