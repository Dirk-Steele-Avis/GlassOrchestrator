@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File ".\Setup-GlassEnv.ps1" %*
set "CODE=%errorlevel%"

echo.
echo Exit code: %CODE%
exit /b %CODE%
