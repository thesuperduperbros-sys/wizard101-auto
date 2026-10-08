@echo off
setlocal EnableExtensions
title wiz101-auto
cd /d "%~dp0"
if not exist state mkdir state
REM Arguments (e.g. "start", "stop", "status", "logs -f") skip the menu and run
REM that command directly after updating/installing. "--updated" is internal.
set "ARGS=%* "
if /i "%~1"=="--updated" set "ARGS=%ARGS:*--updated=%"
set "HAS_ARGS="
if not "%ARGS: =%"=="" set "HAS_ARGS=1"

echo ============================================================
echo   wiz101-auto  (one-click launcher)
echo ============================================================
echo.

REM ---- 0. Self-update from GitHub -------------------------------------------
REM Your config.yaml, state folder and .venv are never touched (they're
REM git-ignored). The whole block is parsed before it runs, so replacing this
REM file mid-update is safe; the new launcher is then restarted.
set "REPO=https://github.com/szatcg/wiz101-auto.git"
REM Only in menu mode: terminal commands (start/stop/status...) leave git alone
REM so local development is never overwritten.
if defined HAS_ARGS goto :noupdate
if /i "%~1"=="--updated" goto :noupdate
where git >nul 2>&1 || goto :noupdate
echo [0/4] Checking for updates...
if not exist .git (
  git init -q
  git remote add origin "%REPO%"
  git fetch -q origin main && (
    git reset -q --hard origin/main
    git branch -q -M main
    git branch -q -u origin/main main
    "%~f0" --updated %*
    exit /b
  ) || (
    echo       could not reach GitHub; using the files already here.
  )
  goto :noupdate
)
git diff --quiet && git diff --cached --quiet || (
  echo       local changes present; skipping the automatic update.
  goto :noupdate
)
git pull -q --ff-only origin main && (
  for /f %%h in ('git rev-parse --short HEAD') do echo       on version %%h
  "%~f0" --updated %*
  exit /b
) || (
  echo       could not update from GitHub; using the files already here.
)
:noupdate

REM ---- 1. Stop any other copy of the bot (old versions, other folders) ----
REM (skipped for commands like "status"/"stop" that manage the running bot)
if defined HAS_ARGS goto :python
echo [1/4] Checking for other running copies of the bot...
REM A bot started from the terminal is stopped cleanly first (unhooks the game).
if exist ".venv\Scripts\python.exe" if exist state\bot.pid (
  ".venv\Scripts\python.exe" -m wiz101_auto stop
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'wiz101[_-]auto' }; foreach ($x in $p) { Write-Host ('      stopping old bot, PID ' + $x.ProcessId); Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }; if ($p) { exit 1 } else { exit 0 }"
if errorlevel 1 (
  echo       Old bot stopped. If the new one says it cannot hook into the game,
  echo       fully close and restart Wizard101, then run this again.
  timeout /t 3 >nul
) else (
  echo       none running.
)

REM ---- 2. Python and Git ----
:python
echo [2/4] Checking Python and Git...
set "PY="
for %%v in (3.14 3.13 3.15 3.16) do (
  if not defined PY (
    py -%%v -c "import sys" >nul 2>&1 && set "PY=py -%%v"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  where py >nul 2>&1 && (
    echo       Installing Python 3.13 with the Python Install Manager...
    py install 3.13
    py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
  )
)
if not defined PY (
  echo.
  echo ERROR: Python 3.13 or newer is not installed.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  goto :fail
)
where git >nul 2>&1
if errorlevel 1 (
  echo.
  echo ERROR: Git is not installed. Get it from https://git-scm.com/download/win
  echo then restart your PC and run this again.
  goto :fail
)
echo       ok.

REM ---- 3. Install / update the bot ----
echo [3/4] Checking the bot install...
set "NEED_INSTALL="
if not exist ".venv\Scripts\python.exe" set "NEED_INSTALL=1"
if not defined NEED_INSTALL (
  ".venv\Scripts\python.exe" -c "import wizwalker, wiz101_auto" >nul 2>&1 || set "NEED_INSTALL=1"
)
if not defined NEED_INSTALL (
  fc /b pyproject.toml ".venv\installed-pyproject.toml" >nul 2>&1 || set "NEED_INSTALL=1"
)
if defined NEED_INSTALL goto :install
echo       up to date.
goto :configured

:install
echo       Installing. The first time takes a few minutes (log: state\setup.txt)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv > state\setup.txt 2>&1 || goto :installfail
)
".venv\Scripts\python.exe" -m pip install --upgrade pip >> state\setup.txt 2>&1
".venv\Scripts\python.exe" -m pip install -e . >> state\setup.txt 2>&1 || goto :installfail
".venv\Scripts\python.exe" -c "import wizwalker, wiz101_auto" >> state\setup.txt 2>&1 || goto :installfail
copy /y pyproject.toml ".venv\installed-pyproject.toml" >nul
echo       installed.

:configured
if not exist config.yaml (
  copy configs\couch_potato.yaml config.yaml >nul
  echo       created config.yaml from the Couch Potato farm preset.
)
set "BOT=.venv\Scripts\python.exe -m wiz101_auto"
if defined HAS_ARGS (
  %BOT% %ARGS%
  exit /b
)

REM ---- 4. Menu ----
echo [4/4] Ready. Log into Wizard101 and stand in the world with your wizard.
echo.
echo   [1] Run the bot                      (starts automatically in 8 seconds)
echo   [2] Health check      - saves state\doctor.txt
echo   [3] Show deck plan    - saves state\deck.txt (changes nothing)
echo   [4] Rebuild my deck   - saves state\deck_apply.txt
echo   [5] What the bot sees - saves state\inspect.txt
echo   [6] Record this zone  - saves state\explore_*.txt
echo.
echo   While the bot runs: Ctrl+Shift+Q = stop, Ctrl+Shift+P = pause/resume
echo.
choice /c 123456 /t 8 /d 1 /n /m "Choose 1-6: "
set "CHOICE=%errorlevel%"
echo.
if "%CHOICE%"=="1" goto :run
if "%CHOICE%"=="2" goto :doctor
if "%CHOICE%"=="3" goto :deck
if "%CHOICE%"=="4" goto :deckapply
if "%CHOICE%"=="5" goto :inspect
if "%CHOICE%"=="6" goto :explore
goto :done

:run
%BOT% run -c config.yaml
goto :done

:deck
%BOT% deck -c config.yaml > state\deck.txt 2>&1
type state\deck.txt
echo.
echo Saved to %~dp0state\deck.txt
goto :done

:deckapply
%BOT% deck -c config.yaml --apply > state\deck_apply.txt 2>&1
type state\deck_apply.txt
echo.
echo Saved to %~dp0state\deck_apply.txt
goto :done

:inspect
%BOT% inspect > state\inspect.txt 2>&1
type state\inspect.txt
echo.
echo Saved to %~dp0state\inspect.txt
goto :done

:explore
%BOT% explore
goto :done

:doctor
(
  echo ===== wiz101-auto health check =====
  date /t
  time /t
  ver
  echo Python: %PY%
  ".venv\Scripts\python.exe" --version
  git --version
  echo.
  echo --- game ---
  tasklist /FI "IMAGENAME eq WizardGraphicalClient.exe"
  echo.
  echo --- config ---
  ".venv\Scripts\python.exe" -c "from wiz101_auto.config import load_config; load_config('config.yaml'); print('config OK')"
  echo.
  echo --- bot view ---
  %BOT% inspect
  echo.
  echo --- last log lines ---
  if exist wiz101-auto.log powershell -NoProfile -Command "Get-Content wiz101-auto.log -Tail 80"
) > state\doctor.txt 2>&1
type state\doctor.txt
echo.
echo Saved to %~dp0state\doctor.txt
goto :done

:installfail
echo.
echo ERROR: installing the bot failed. Last lines of state\setup.txt:
powershell -NoProfile -Command "Get-Content 'state\setup.txt' -Tail 25"
goto :fail

:fail
echo.
echo Send state\setup.txt (if it exists) and a screenshot of this window to Claude.
pause
exit /b 1

:done
echo.
pause
exit /b 0
