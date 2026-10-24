@echo off
rem ===========================================================================
rem  Check settings, data, agency definitions and the trip planner, and say in
rem  plain words what (if anything) needs fixing.
rem ===========================================================================
setlocal EnableExtensions
title Checking setup
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto :run
echo.
echo  The assistant isn't set up yet. Double-click start.bat first.
goto :done

:run
"%VENV_PY%" -m transit_mcp.cli doctor

:done
echo.
pause
endlocal
