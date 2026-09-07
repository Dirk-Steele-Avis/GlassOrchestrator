@echo off
setlocal EnableExtensions

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File ".\Remove-LogPngFiles.ps1"
if errorlevel 1 (
  echo [ERROR] Failed to remove log PNG files.
  exit /b 1
)

exit /b 0