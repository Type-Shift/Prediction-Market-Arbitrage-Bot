@echo off
rem ===========================================================================
rem  Double-click this to start everything: the dashboard, the scanner behind
rem  it, and the paper-trading bot, then open the page in a browser.
rem
rem  Leave this window open -- it is the server. The scanner's progress prints
rem  here as it runs, and closing the window (or Ctrl-C) stops it.
rem
rem  Anything you type after the filename is passed through to app.py, so
rem  `run.bat --port 9000` and `run.bat --offline fixtures` both work.
rem ===========================================================================

setlocal
title Divergence scanner
rem Run from the folder this file lives in, so double-clicking works no matter
rem what the current directory happens to be.
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  Find a Python. The py launcher ships with the python.org installer and is
rem  the more reliable of the two; a Microsoft Store install usually only has
rem  `python` on PATH. Try both before giving up.
rem ---------------------------------------------------------------------------
set "PY="
py -3 -c "" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "" >nul 2>nul && set "PY=python"
)
if not defined PY goto :no_python

rem The project is 3.10+ (it uses `X | None` annotations, which are a syntax
rem error before that). Check now, so the failure is a sentence rather than a
rem traceback from halfway through an import.
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python

if not exist "app.py" goto :wrong_folder

echo.
echo   Starting the dashboard, scanner and paper-trading bot.
echo   A browser tab will open once the server is up.
echo.
echo   Keep this window open. Ctrl-C stops the server.
echo.

rem ---------------------------------------------------------------------------
rem  Model review (optional). With an Anthropic API key, clicking Run scan has
rem  Opus review each scan's results. Supply the key in the ANTHROPIC_API_KEY
rem  environment variable, or drop it in a file named anthropic_key.txt beside
rem  this script -- that file is gitignored, so it is never committed.
rem ---------------------------------------------------------------------------
if not defined ANTHROPIC_API_KEY if exist "anthropic_key.txt" set /p ANTHROPIC_API_KEY=<anthropic_key.txt
if defined ANTHROPIC_API_KEY echo   Model review is ON - Opus reviews each scan.
if not defined ANTHROPIC_API_KEY echo   Model review is off - set ANTHROPIC_API_KEY or add anthropic_key.txt to enable it.
echo.

rem ---------------------------------------------------------------------------
rem  Remote scanning. The prediction venues are blocked on some networks (a
rem  school or office filter), so by default each scan runs on GitHub's runners
rem  -- which are not behind the filter -- and the results are pulled back. This
rem  needs the GitHub CLI (gh), installed and logged in. Set ARB_REMOTE_SCAN=0
rem  before running to scan on this machine instead.
rem ---------------------------------------------------------------------------
if not defined ARB_REMOTE_SCAN set "ARB_REMOTE_SCAN=1"
where gh >nul 2>nul
if errorlevel 1 (
  echo   Remote scan wants the GitHub CLI 'gh', not found on PATH - scans will
  echo   run on this machine. Install gh from https://cli.github.com/ to bypass
  echo   a network that blocks the venues.
) else (
  if not "%ARB_REMOTE_SCAN%"=="0" echo   Scans run on GitHub Actions to bypass a blocked network.
)
echo.

rem --open waits until the port is actually bound before launching the browser,
rem so the tab never lands on a page that is not being served yet.
%PY% app.py --open %*
set "EXITCODE=%ERRORLEVEL%"

rem A clean Ctrl-C is not a failure worth pausing over; anything else is, or the
rem error scrolls past as the window closes.
if not "%EXITCODE%"=="0" goto :crashed
goto :end

rem ---------------------------------------------------------------------------

:no_python
echo.
echo   Could not find Python on this machine.
echo.
echo   Install Python 3.10 or newer from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" in the installer.
echo.
goto :hold

:old_python
echo.
echo   This Python is too old. The scanner needs 3.10 or newer.
echo.
%PY% -c "import sys; print('   found:', sys.version.split()[0], 'at', sys.executable)"
echo.
echo   Install a newer one from https://www.python.org/downloads/
echo.
goto :hold

:wrong_folder
echo.
echo   app.py is not in this folder:
echo     %CD%
echo.
echo   Keep run.bat in the same folder as app.py and scanner.py.
echo.
goto :hold

:crashed
echo.
echo   The server stopped with error code %EXITCODE%. The reason should be
echo   just above this line.
echo.
goto :hold

:hold
pause

:end
endlocal
