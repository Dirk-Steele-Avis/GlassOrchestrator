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
rem  Close open glass work items.
rem
rem  Usage:
rem    Run-CloseWorkItems.cmd
rem      Uses the operator-reviewed WorkItems\close_workitem.csv file.
rem
rem    Run-CloseWorkItems.cmd --csv "WorkItems\close_workitem.csv"
rem      Uses a specific reviewed CSV with required mva and Type columns.
rem
rem    Run-CloseWorkItems.cmd --mvas "012345678,087654321"
rem      Closes work items for an explicit comma- or space-separated MVA list.
rem
rem    Run-CloseWorkItems.cmd --max-rows 1
rem      Limits processing to the first reviewed CSV row.
rem
rem  Common switches passed through to close_workitem.py:
rem    --timeout-seconds N       Per-phase timeout (default: 120)
rem    --debug-hold-seconds N    Keep the browser open after a failure
rem    --pause                   Wait for Enter before closing the browser
rem    --no-pause                Deprecated compatibility switch
rem    --help                    Show all available options
rem ---------------------------------------------------------------------------

echo Closing work items...

"%VENV_PY%" WorkItems\close_workitem.py %*

echo.
echo Exit code: %errorlevel%
exit /b %errorlevel%