@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
set "REQ_FILE=requirements.txt"
set "REQ_STAMP=.venv\.requirements.sha256"
set "CREATED_VENV=0"

if not exist "%VENV_PY%" (
  echo [BOOTSTRAP] Creating virtual environment in .venv ...
  py -3.13 -m venv .venv
  if errorlevel 1 (
    echo [WARNING] py -3.13 failed, trying py -3 ...
    py -3 -m venv .venv
  )
  if not exist "%VENV_PY%" (
    echo [ERROR] Failed to create virtual environment at %VENV_PY%.
    exit /b 1
  )
  set "CREATED_VENV=1"
  echo [BOOTSTRAP] Virtual environment created successfully.
)

if not exist "%REQ_FILE%" (
  echo [ERROR] Missing %REQ_FILE%. Cannot install dependencies.
  exit /b 1
)

set "REQ_HASH="
for /f "tokens=1" %%H in ('certutil -hashfile "%REQ_FILE%" SHA256 ^| findstr /r /v /c:"hash of file" /c:"CertUtil"') do (
  set "REQ_HASH=%%H"
  goto :hash_done
)
:hash_done

set "SYNC_DEPS=1"
if defined REQ_HASH if exist "%REQ_STAMP%" (
  set /p PREV_HASH=<"%REQ_STAMP%"
  if /i "!PREV_HASH!"=="!REQ_HASH!" if "%CREATED_VENV%"=="0" set "SYNC_DEPS=0"
)

if "%SYNC_DEPS%"=="1" (
  echo [BOOTSTRAP] Installing/updating Python requirements ...
  "%VENV_PY%" -m pip install --disable-pip-version-check -r "%REQ_FILE%"
  if errorlevel 1 (
    echo [ERROR] Failed to install requirements from %REQ_FILE%
    exit /b 1
  )
  echo [BOOTSTRAP] Installing Playwright browsers ...
  "%VENV_PY%" -m playwright install
  if errorlevel 1 (
    echo [ERROR] Failed to install Playwright browsers
    exit /b 1
  )
  if defined REQ_HASH (
    > "%REQ_STAMP%" echo !REQ_HASH!
  )
) else (
  echo [BOOTSTRAP] Requirements unchanged. Skipping dependency install.
)

rem ---------------------------------------------------------------------------
rem  Close/resolve open glass work items for MVAs loaded from GlassClaims sheet.
rem
rem  Usage:
rem    Run-CloseWorkItems.cmd
rem    Run-CloseWorkItems.cmd 180
rem    Run-CloseWorkItems.cmd 180 45
rem    Run-CloseWorkItems.cmd 180 45 1
rem    Run-CloseWorkItems.cmd 180 0 2 59179595,61103092
rem
rem  Exit code:
rem    0 = all MVAs had their glass work item successfully closed
rem    1 = one or more MVAs had no open work item or failed
rem ---------------------------------------------------------------------------

set "TIMEOUT_SECONDS=120"
set "DEBUG_HOLD_SECONDS=60"
set "MAX_ROWS=1"
set "EXPLICIT_MVAS="

if not "%~1"=="" set "TIMEOUT_SECONDS=%~1"
if not "%~2"=="" set "DEBUG_HOLD_SECONDS=%~2"
if not "%~3"=="" set "MAX_ROWS=%~3"
if not "%~4"=="" set "EXPLICIT_MVAS=%~4"

echo Closing work items from GlassClaims sheet (timeout=%TIMEOUT_SECONDS%s, debug_hold=%DEBUG_HOLD_SECONDS%s, max_rows=%MAX_ROWS%)
echo Tip: set 2nd arg to 0 to disable debug hold.

set "GLASS_AGENTIC=1"
set "MVAS_ARG="
if defined EXPLICIT_MVAS set "MVAS_ARG=--mvas %EXPLICIT_MVAS%"
"%VENV_PY%" WorkItems\close_workitem.py --no-pause --timeout-seconds %TIMEOUT_SECONDS% --debug-hold-seconds %DEBUG_HOLD_SECONDS% --max-rows %MAX_ROWS% %MVAS_ARG%

echo.
echo Exit code: %errorlevel%
exit /b %errorlevel%
