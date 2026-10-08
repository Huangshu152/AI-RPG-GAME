@echo off
REM ---------------------------------------------------------------
REM  AI RPG Engine V0.1 launcher
REM  This file must stay ASCII-only: cmd.exe mis-parses batch files
REM  that contain multi-byte (UTF-8) characters, however harmless
REM  they look. Chinese documentation lives in README.md instead.
REM ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

echo ==========================================
echo   AI RPG Engine V0.1
echo ==========================================
echo.

REM An existing working .venv is enough on its own - no system Python needed.
if exist ".venv\Scripts\python.exe" goto checkdeps

REM Otherwise find a Python that really runs, by actually executing it:
REM a path on its own proves nothing. python.exe under WindowsApps may be the
REM Microsoft Store stub, the Python Install Manager alias, or a real install,
REM and C:\Windows\py.exe (old launcher) may point at a deleted interpreter.
REM Compare ERRORLEVEL as text: a failed launcher can report a negative HRESULT
REM (0x80070003), and "if not errorlevel 1" wrongly treats negatives as success.
set "PYEXE="
python -c "import sys" >nul 2>nul
if "%ERRORLEVEL%"=="0" set "PYEXE=python"
if defined PYEXE goto makevenv
py -3 -c "import sys" >nul 2>nul
if "%ERRORLEVEL%"=="0" set "PYEXE=py -3"
if defined PYEXE goto makevenv
py -c "import sys" >nul 2>nul
if "%ERRORLEVEL%"=="0" set "PYEXE=py"
if defined PYEXE goto makevenv
goto nopython

:makevenv
echo Using Python: %PYEXE%
echo [1/3] Creating virtual environment .venv ...
%PYEXE% -m venv .venv
if errorlevel 1 goto venvfail
goto installdeps

:checkdeps
if not exist ".venv\.deps_ok" goto installdeps
goto run

:installdeps
echo [2/3] Installing dependencies (first run needs internet) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto depfail
echo ok> ".venv\.deps_ok"

:run
echo [3/3] Server: http://127.0.0.1:8000
echo       The browser opens automatically. Press Ctrl+C to stop.
echo.
start "AI RPG Engine" /min cmd /c "ping -n 3 127.0.0.1 >nul && start http://127.0.0.1:8000"
".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
goto end

:nopython
echo [ERROR] No working Python found, and there is no usable .venv either.
echo.
echo   1) Not installed: install Python 3.10 or newer from
echo      https://www.python.org/downloads/windows/
echo      and tick "Add python.exe to PATH" during setup.
echo.
echo   2) Installed but broken: the py launcher may point at an
echo      interpreter that was moved or deleted. Reinstall Python,
echo      then open a NEW terminal window.
echo.
echo   Check with:  python --version
echo   Chinese instructions: see README.md
goto end

:venvfail
echo [ERROR] Could not create the virtual environment.
echo         If .venv exists but is damaged, delete it and rerun.
goto end

:depfail
echo [ERROR] Could not install dependencies.
echo         The first run needs internet access to fetch fastapi and uvicorn.
echo         If .venv is half-installed, delete it and rerun.
goto end

:end
echo.
pause
