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
  if defined REQ_HASH (
    > "%REQ_STAMP%" echo !REQ_HASH!
  )
) else (
  echo [BOOTSTRAP] Requirements unchanged. Skipping dependency install.
)

echo Running GlassWorkItems (Phase 7) with venv Python...
"%VENV_PY%" ".\GlassWorkItems.py"

echo.
echo Exit code: %errorlevel%
exit /b %errorlevel%
