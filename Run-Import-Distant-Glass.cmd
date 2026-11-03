@echo off
setlocal EnableExtensions

cd /d "%~dp0"

echo Importing distant-location glass orders...
call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe import_distant_glass.py %*
exit /b %errorlevel%