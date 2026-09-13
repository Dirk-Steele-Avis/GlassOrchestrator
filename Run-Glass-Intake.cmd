@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo Running Glass intake workflow...

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe GlassOrchestrator.py
if errorlevel 1 (
  echo [ERROR] GlassOrchestrator step failed.
  exit /b %errorlevel%
)

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe FieldPOFillNextAction.py
if errorlevel 1 (
  echo [WARNING] FieldPO next-action step failed; continuing to EnsureGlassWorkItems.
)

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe create_compass_complaints.py %*
set "RUN_EXIT=%errorlevel%"
echo.
echo Exit code: %RUN_EXIT%
exit /b %RUN_EXIT%