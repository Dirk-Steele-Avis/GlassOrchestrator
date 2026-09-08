@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo [ERROR] Missing project virtual environment: %VENV_PY%
  echo [INFO] Run Run-Setup-GlassEnv.cmd first.
  exit /b 1
)

rem Default: populate only, do not submit, and leave source emails unread.
rem Production: ProcessLocalMarket_PMs.cmd --submit
"%VENV_PY%" ProcessLocalMarket_PMs.py %*
exit /b %errorlevel%