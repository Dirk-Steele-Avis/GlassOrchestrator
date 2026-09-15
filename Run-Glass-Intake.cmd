@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo Running Glass intake workflow...

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe GlassOrchestrator.py
set "RUN_EXIT=%errorlevel%"
if not "%RUN_EXIT%"=="0" (
  echo [ERROR] Glass intake stopped. Resolve the error above, then run this command again.
  exit /b %RUN_EXIT%
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