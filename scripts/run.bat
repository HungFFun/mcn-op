@echo off
REM mekoo-op task runner (Windows).
REM
REM Usage:  scripts\run.bat ^<task^> [extra args...]
REM Tasks:  mcn-report | leave-watch | daily-recap | payout | check | schedule
REM
REM Examples:
REM   scripts\run.bat daily-recap
REM   scripts\run.bat mcn-report --no-send
REM   scripts\run.bat check --live
setlocal enabledelayedexpansion
cd /d "%~dp0.."

REM Force UTF-8 so emoji / Vietnamese in task output don't crash the cp1252 console.
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if "%PYTHON%"=="" set PYTHON=python

set TASK=%1
shift

set ARGS=
:collect
if "%1"=="" goto run
set ARGS=!ARGS! %1
shift
goto collect

:run
if /i "%TASK%"=="mcn-report"  ( %PYTHON% -m tasks.mcn_daily_report !ARGS! & goto :eof )
if /i "%TASK%"=="leave-watch" ( %PYTHON% -m tasks.mcn_leave_watch !ARGS! & goto :eof )
if /i "%TASK%"=="daily-recap" ( %PYTHON% -m tasks.daily_recap !ARGS! & goto :eof )
if /i "%TASK%"=="payout"      ( %PYTHON% -m payout.run --market both !ARGS! & goto :eof )
if /i "%TASK%"=="check"       ( %PYTHON% -m common.check !ARGS! & goto :eof )
if /i "%TASK%"=="schedule"    ( %PYTHON% -m scheduler !ARGS! & goto :eof )
if /i "%TASK%"=="webui"       ( %PYTHON% -m webui !ARGS! & goto :eof )
if /i "%TASK%"=="bot"         ( %PYTHON% -m bot !ARGS! & goto :eof )

echo Usage: scripts\run.bat {mcn-report^|leave-watch^|daily-recap^|payout^|check^|schedule^|webui^|bot} [args]
exit /b 2
