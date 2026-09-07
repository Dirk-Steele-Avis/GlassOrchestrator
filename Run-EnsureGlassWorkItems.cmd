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
    echo [INFO] Try deleting .venv folder and running this script again.
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
  if defined REQ_HASH (
    > "%REQ_STAMP%" echo !REQ_HASH!
  )
) else (
  echo [BOOTSTRAP] Requirements unchanged. Skipping dependency install.
)

rem No arguments: process valid MVAs from today's spreadsheet rows.
rem Manual modes:
rem   Run-EnsureGlassWorkItems.cmd --mva 058524185
rem   Run-EnsureGlassWorkItems.cmd --mva 058524185 --dry-run
rem   Run-EnsureGlassWorkItems.cmd --csv WorkItems\create_workitem.csv

if /i "%~1"=="--csv" goto :run_csv

echo Ensuring Glass complaints and work items from today's spreadsheet...
"%VENV_PY%" ".\create_compass_complaints.py" %*
goto :complete

:run_csv
if "%~2"=="" (
  echo [ERROR] --csv requires a CSV path.
  echo Usage: Run-EnsureGlassWorkItems.cmd --csv path\to\file.csv
  exit /b 2
)
if not "%~3"=="" (
  echo [ERROR] CSV mode accepts only --csv and a path.
  exit /b 2
)
if not exist "%~2" (
  echo [ERROR] CSV file not found: %~2
  exit /b 1
)

echo Ensuring Glass complaints and work items from CSV: %~2
set "GLASS_AGENTIC=1"
"%VENV_PY%" WorkItems\create_workitem.py --csv "%~2" --backend playwright

:complete
set "RUN_EXIT=%errorlevel%"
echo.
echo Exit code: %RUN_EXIT%
exit /b %RUN_EXIT%