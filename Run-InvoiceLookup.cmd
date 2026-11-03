@echo off
setlocal EnableExtensions
cd /d "%~dp0"

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe -m invoice_workflow.lookup_cli %*
exit /b %errorlevel%