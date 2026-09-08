@echo off
setlocal EnableExtensions

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File ".\Set-GlassPassword.ps1"
if errorlevel 1 (
  echo [ERROR] Failed to set GLASS_LOGIN_PASSWORD.
  exit /b 1
)

echo [INFO] Password setup complete.
exit /b 0
