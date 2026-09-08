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
rem  Verify that open work items exist for MVAs listed in a CSV.
rem
rem  Usage:
rem    Run-VerifyWorkItems.cmd                             -- uses sample_mvas.csv, type=GLASS
rem    Run-VerifyWorkItems.cmd "data\my_mvas.csv"          -- custom CSV, type=GLASS
rem    Run-VerifyWorkItems.cmd "data\my_mvas.csv" GLASS    -- explicit type keyword
rem    Run-VerifyWorkItems.cmd "data\my_mvas.csv" "Glass Replacement"
rem    Run-VerifyWorkItems.cmd "data\my_mvas.csv" "Glass Repair"
rem
rem  Exit code:
rem    0 = all MVAs have the specified open work item
rem    1 = one or more MVAs missing the work item or failed
rem ---------------------------------------------------------------------------

set "CSV_PATH=WorkItems\create_workitem.csv"
set "TYPE_FILTER=GLASS"

if not "%~1"=="" set "CSV_PATH=%~1"
if not "%~2"=="" set "TYPE_FILTER=%~2"

if not exist "%CSV_PATH%" (
  echo [ERROR] CSV file not found: %CSV_PATH%
  echo Usage: Run-VerifyWorkItems.cmd [csv_path] [type_filter]
  exit /b 1
)

echo Verifying work items in: %CSV_PATH%
echo Type filter: %TYPE_FILTER%

echo [PRE-LAUNCH] Terminating any running Edge instances...
taskkill /F /IM msedge.exe /T 2>nul
timeout /t 2 /nobreak >nul

set "GLASS_AGENTIC=1"
"%VENV_PY%" WorkItems\verify_workitem.py --csv "%CSV_PATH%" --type "%TYPE_FILTER%" --no-pause

echo.
echo Exit code: %errorlevel%
exit /b %errorlevel%
