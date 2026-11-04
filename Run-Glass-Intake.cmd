@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo Running Glass intake workflow...

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe glass_intake.py
set "RUN_EXIT=%errorlevel%"
echo.
echo Exit code: %RUN_EXIT%
exit /b %RUN_EXIT%