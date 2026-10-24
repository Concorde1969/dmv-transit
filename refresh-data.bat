@echo off
rem ===========================================================================
rem  Download the latest alerts, schedules, fares and rider information for every
rem  agency. Safe to run any time (the assistant can stay open). Takes a few
rem  minutes.
rem
rem  Options:  refresh-data.bat --only dash,vre    refresh just those agencies
rem ===========================================================================
setlocal EnableExtensions
title Refreshing transit data
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto :run
echo.
echo  The assistant isn't set up yet. Double-click start.bat first.
goto :done

:run
echo Refreshing transit data for every agency. This takes a few minutes...
echo.
"%VENV_PY%" -m transit_mcp.cli ingest all %*
if errorlevel 1 goto :fail
echo.
echo  Done.
goto :done

:fail
echo.
echo  The refresh did not finish. Please read the messages above.

:done
echo.
pause
endlocal
