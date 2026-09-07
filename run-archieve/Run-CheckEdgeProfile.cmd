@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [ERROR] Missing %VENV_PY%.
  echo Run Run-Setup-GlassEnv.cmd first.
  exit /b 1
)

set "PROFILE_DIR=Profile 1"
if not "%~1"=="" set "PROFILE_DIR=%~1"

echo [PROFILE CHECK] Using profile directory: %PROFILE_DIR%
"%VENV_PY%" -m playwright_prototype.profile_launch_check --edge-profile-directory "%PROFILE_DIR%"

echo.
echo Exit code: %errorlevel%
exit /b %errorlevel%
