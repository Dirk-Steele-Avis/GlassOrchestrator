@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo [ERROR] Missing %VENV_PY%.
  echo [INFO] Run Run-FieldPOFillNextAction.cmd once to bootstrap the virtual environment.
  exit /b 1
)

echo Running FieldPO NextAction rollback (latest backup)...
"%VENV_PY%" ".\FieldPOFillNextAction.py" --rollback-latest

echo.
echo Exit code: %errorlevel%
exit /b %errorlevel%
