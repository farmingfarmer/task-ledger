@echo off
rem ============================================================
rem  Build the portable Python runtime that kits carry.
rem
rem  Run this ONCE, on a Windows machine with internet access.
rem  It produces %USERPROFILE%\task-ledger-runtime -- a complete
rem  Python with the four packages this system needs, owned by
rem  nobody, installed nowhere, and copyable. Kits built with
rem  "tl kit --with-runtime" include it, so a teammate needs
rem  nothing on their machine at all.
rem
rem  Python's "embeddable" distribution is a zip, not an
rem  installer: no admin rights, no PATH changes, no registry,
rem  and no interference with any Python already on the machine.
rem  It ships without pip and with site-packages disabled, so
rem  this enables both -- that is what the _pth edit below is.
rem
rem  Re-runnable: delete the folder to start clean.
rem ============================================================
setlocal EnableDelayedExpansion

rem 3.12 on purpose: psycopg2-binary publishes wheels for it, which
rem newer versions do not always have on release day, and a source
rem build needs a C compiler nobody wants to install.
set "PYVER=3.12.7"
set "DEST=%USERPROFILE%\task-ledger-runtime"
set "TMPD=%TEMP%\tl-runtime-build"

echo.
echo   Building a portable Python %PYVER% at:
echo     %DEST%
echo.

if exist "%DEST%\python.exe" (
  echo   Already built. Delete the folder to rebuild.
  echo.
  pause
  exit /b 0
)

if exist "%TMPD%" rmdir /s /q "%TMPD%"
mkdir "%TMPD%" 2>nul
mkdir "%DEST%" 2>nul

echo   [1/4] downloading Python...
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-embed-amd64.zip' -OutFile '%TMPD%\py.zip'"
if errorlevel 1 goto :netfail

echo   [2/4] unpacking...
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop'; Expand-Archive -Path '%TMPD%\py.zip' -DestinationPath '%DEST%' -Force"
if errorlevel 1 goto :fail

rem The embeddable build ships with site-packages switched off, which
rem means pip cannot install anything anywhere it will later be found.
rem Uncommenting "import site" in the ._pth is the documented way to
rem turn it back on. The filename carries the version, so glob it.
echo   [3/4] enabling packages...
powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop'; Get-ChildItem '%DEST%\python*._pth' | ForEach-Object { (Get-Content $_.FullName) -replace '^#\s*import site','import site' | Set-Content $_.FullName }"
if errorlevel 1 goto :fail

powershell -NoProfile -Command ^
  "$ErrorActionPreference='Stop'; Invoke-WebRequest -UseBasicParsing -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%TMPD%\get-pip.py'"
if errorlevel 1 goto :netfail
"%DEST%\python.exe" "%TMPD%\get-pip.py" --no-warn-script-location >nul 2>&1
if errorlevel 1 goto :fail

echo   [4/4] installing what the app needs...
"%DEST%\python.exe" -m pip install --no-warn-script-location ^
    psycopg2-binary flask pyyaml jsonschema >nul 2>&1
if errorlevel 1 goto :fail

rem Prove it before claiming success: a runtime that cannot import the
rem four packages is worse than none, because the kit will look right
rem and fail on a teammate's machine instead of on this one.
"%DEST%\python.exe" -c "import psycopg2, flask, yaml, jsonschema; print('   verified:', psycopg2.__name__, flask.__name__, yaml.__name__, jsonschema.__name__)"
if errorlevel 1 goto :fail

rmdir /s /q "%TMPD%" 2>nul
echo.
echo   Done. Kits built with:
echo       tl.bat kit --apply --with-runtime --out ^<folder^>
echo   now carry this runtime, and teammates need nothing installed.
echo.
pause
exit /b 0

:netfail
echo.
echo   [X] Could not download. Check the connection, or whether a
echo       proxy blocks python.org / bootstrap.pypa.io.
echo.
pause
exit /b 1

:fail
echo.
echo   [X] Build failed. Delete %DEST% and try again.
echo.
pause
exit /b 1
