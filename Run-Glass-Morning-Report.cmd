@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo Generating Glass Damage Morning Report...
call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe glass_morning_report.py %*
set "RUN_EXIT=%errorlevel%"

if not "%RUN_EXIT%"=="0" (
  echo [ERROR] Glass morning report was not created. Resolve the error above, then run this command again.
  exit /b %RUN_EXIT%
)

echo Report opened in your default browser.
echo Click "Copy report for Outlook", then paste it into the email body.
exit /b 0