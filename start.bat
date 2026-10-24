@echo off
rem ===========================================================================
rem  DMV Regional Transportation Assistant - double-click to start.
rem
rem  First run: creates a private Python environment, installs the app, creates
rem  your settings file, and downloads transit data (5-10 minutes).
rem  After that: starts the assistant and opens it in your web browser.
rem
rem  Options:  start.bat --no-browser     start without opening a browser
rem ===========================================================================
setlocal EnableExtensions
title DMV Regional Transportation Assistant
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto :have_venv

rem ---- First run: find Python 3.11+ and create the environment -------------
call :find_python
if errorlevel 1 goto :fail
echo Setting up for the first time. This takes a few minutes...
%PY% -m venv .venv
if errorlevel 1 goto :fail

:have_venv
rem ---- (Re)install whenever pyproject.toml is newer than the last install ---
"%VENV_PY%" -c "import os,sys; s='.venv/.installed'; sys.exit(0 if os.path.exists(s) and os.path.getmtime(s) >= os.path.getmtime('pyproject.toml') else 1)"
if not errorlevel 1 goto :installed
echo Installing the assistant's components...
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check -q -e .
if errorlevel 1 goto :fail
type nul > ".venv\.installed"
:installed

rem ---- Settings file ----------------------------------------------------------
if exist ".env" goto :launch
copy /y ".env.example" ".env" >nul
echo.
echo  A settings file called .env has been created and will open in Notepad.
echo  Paste in your ANTHROPIC_API_KEY and WMATA_API_KEY, save, close Notepad,
echo  and then double-click start.bat again.
start "" notepad ".env"
goto :done

:launch
"%VENV_PY%" -m transit_mcp.cli launch %*
if errorlevel 1 goto :fail
goto :done

rem ---- helpers --------------------------------------------------------------
:find_python
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto :try_python
set "PY=py -3"
exit /b 0
:try_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto :no_python
set "PY=python"
exit /b 0
:no_python
echo.
echo  Python 3.11 or newer is needed but was not found on this computer.
echo  Install it from https://www.python.org/downloads/
echo  During setup, tick the box "Add python.exe to PATH".
echo  Then double-click start.bat again.
exit /b 1

:fail
echo.
echo  The assistant could not start. Please read the messages above.
echo  For a full check of your setup, double-click check-setup.bat.

:done
echo.
pause
endlocal
