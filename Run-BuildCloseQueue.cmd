@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Missing .venv. Run Run-Setup-GlassEnv.cmd first.
  exit /b 1
)

".venv\Scripts\python.exe" "WorkItems\build_close_queue.py" %*
exit /b %errorlevel%