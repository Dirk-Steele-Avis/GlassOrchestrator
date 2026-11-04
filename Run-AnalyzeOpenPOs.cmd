@echo off
setlocal EnableExtensions
cd /d "%~dp0"

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe -m invoice_workflow.open_po_analysis %*
exit /b %errorlevel%
